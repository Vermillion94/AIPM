"""Scheduler — main work loop that ties everything together."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ..config import Settings
from ..db.models import (
    Complexity,
    Decision,
    DecisionStatus,
    RunStatus,
    TaskStatus,
)
from ..db.store import Store
from ..github.actions import GitHubActions
from ..github.sync import GitHubSync
from .budget import BudgetTracker
from .qa import QARunner
from .router import TaskRouter
from .worker import Worker
from .worktree import WorktreeManager

if TYPE_CHECKING:
    from ..db.models import Task, WorkRun
    from ..integrations.telegram_bot import TelegramNotifier

logger = logging.getLogger("aipm.core.scheduler")


class Scheduler:
    """Main work loop — sync issues, route tasks, dispatch workers, run QA."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.github_sync = GitHubSync(settings, store)
        self.router = TaskRouter(settings, store)
        self.budget = BudgetTracker(settings, store)
        self.worker = Worker(settings, store)
        self.qa = QARunner(settings, store)
        self.worktree_mgr = WorktreeManager(settings.worktrees_path)
        self.github_actions = GitHubActions(settings)
        self._running = False

        # Pause event: set = running, cleared = paused
        self.pause_event = asyncio.Event()
        self.pause_event.set()  # starts running

        # Optional Telegram notifier (wired by daemon)
        self._telegram: Optional[TelegramNotifier] = None
        self._budget_warning_sent = False

    def set_telegram(self, telegram: TelegramNotifier) -> None:
        """Set the Telegram notifier for sending notifications."""
        self._telegram = telegram

    async def run_once(self) -> dict:
        """Run a single cycle: sync → pick task → execute → QA → publish.

        Returns a summary dict of what happened.
        """
        summary = {"synced": {}, "worked": None, "qa": None}

        # 1. Sync issues
        logger.info("Syncing issues from GitHub...")
        try:
            sync_results = await self.github_sync.sync_all()
            summary["synced"] = sync_results
        except Exception as e:
            logger.error(f"Sync failed: {e}")

        # 2. Check budget
        budget_status = await self.budget.check_budget()
        if not budget_status.can_work:
            logger.info(f"Budget exhausted: {budget_status.reason}")
            summary["budget_exhausted"] = True
            await self._notify_budget_warning(budget_status.daily_spent, budget_status.daily_limit)
            return summary

        # 3. Check worker slots
        active_runs = await self.store.count_active_runs()
        if active_runs >= self.settings.aipm.max_concurrent_workers:
            logger.info(f"All worker slots busy ({active_runs} active)")
            summary["workers_busy"] = True
            return summary

        # 4. Pick next task
        task = await self.store.get_next_task()
        if not task:
            logger.info("No tasks in backlog")
            summary["no_tasks"] = True
            return summary

        # 5. Classify and route
        complexity, model = await self.router.classify_and_route(task)
        logger.info(f"Routing task {task.id} → {model} (complexity: {complexity.value})")

        # 5b. Complex task gate: request human approval before first execution
        if complexity == Complexity.COMPLEX:
            failed_count = await self.store.count_failed_runs(task.id)
            if failed_count == 0:
                # First attempt on a complex task — request approval
                await self._request_decision(
                    task,
                    question=f"Complex task detected (model: {model}). Approve execution?",
                    options=["Approve", "Skip", "Assign to human"],
                )
                summary["decision_requested"] = True
                return summary

        # 6. Create worktree
        project = await self.store.get_project(task.project_id)
        if not project:
            logger.error(f"Project not found: {task.project_id}")
            return summary

        repo_url = f"https://github.com/{project.owner}/{project.repo}"
        branch_name = f"aipm/issue-{task.github_number}"
        worktree_path = await self.worktree_mgr.create(
            repo_url=repo_url,
            branch_name=branch_name,
            base_branch=project.default_branch,
            task_id=task.id,
        )

        if not worktree_path:
            logger.error(f"Failed to create worktree for {task.id}")
            await self.store.update_task_status(task.id, TaskStatus.FAILED)
            return summary

        # 7. Execute work
        execution_mode = self.budget.select_execution_mode(model)
        run = await self.worker.execute(
            task=task,
            worktree_path=worktree_path,
            model=model,
            execution_mode=execution_mode,
        )
        summary["worked"] = {"task_id": task.id, "model": model, "run_id": run.id}

        # 8. Run QA (if work succeeded)
        if run.status == RunStatus.SUCCEEDED:
            qa_result = await self.qa.run_checks(worktree_path, run, None)
            summary["qa"] = {
                "passed": qa_result.passed,
                "issues": qa_result.issues,
            }

            if qa_result.passed:
                # Publish results: push branch → create PR → comment on issue
                await self._publish_results(task, run, project, worktree_path, branch_name)
                logger.info(f"Task {task.id} completed, passed QA, and published!")
                # Notify success
                await self._notify_success(task, run)
            else:
                await self._handle_failure(task, run)
                logger.warning(f"Task {task.id} failed QA: {qa_result.issues}")
        else:
            await self._handle_failure(task, run)

        return summary

    async def start(self) -> None:
        """Start the continuous scheduling loop."""
        self._running = True
        logger.info("Scheduler started")

        sync_interval = self.settings.aipm.sync_interval_seconds
        work_interval = self.settings.aipm.work_interval_seconds

        last_sync = 0.0

        while self._running:
            try:
                # Check pause state
                if not self.pause_event.is_set():
                    logger.info("Scheduler paused, waiting for resume...")
                    await self.pause_event.wait()
                    logger.info("Scheduler resumed")

                import time
                now = time.time()

                # Sync on interval
                if now - last_sync >= sync_interval:
                    logger.info("Running sync cycle...")
                    try:
                        await self.github_sync.sync_all()
                    except Exception as e:
                        logger.error(f"Sync failed: {e}")
                    last_sync = now

                # Try to dispatch work
                budget_status = await self.budget.check_budget()
                if budget_status.can_work:
                    # Reset budget warning flag when budget is available again
                    self._budget_warning_sent = False

                    active_runs = await self.store.count_active_runs()
                    if active_runs < self.settings.aipm.max_concurrent_workers:
                        task = await self.store.get_next_task()
                        if task:
                            # Run work in background
                            asyncio.create_task(self._work_on_task(task))
                else:
                    await self._notify_budget_warning(
                        budget_status.daily_spent, budget_status.daily_limit
                    )

                await asyncio.sleep(work_interval)

            except asyncio.CancelledError:
                logger.info("Scheduler cancelled")
                break
            except Exception as e:
                logger.exception(f"Scheduler error: {e}")
                await asyncio.sleep(30)

        logger.info("Scheduler stopped")

    async def stop(self) -> None:
        """Stop the scheduling loop."""
        self._running = False
        # Unblock the pause event so the loop can exit
        self.pause_event.set()
        await self.github_sync.close()
        await self.github_actions.close()

    async def _work_on_task(self, task: Task) -> None:
        """Background task to work on a single issue."""
        try:
            complexity, model = await self.router.classify_and_route(task)
            project = await self.store.get_project(task.project_id)
            if not project:
                return

            # Complex task gate for background work
            if complexity == Complexity.COMPLEX:
                failed_count = await self.store.count_failed_runs(task.id)
                if failed_count == 0:
                    await self._request_decision(
                        task,
                        question=f"Complex task detected (model: {model}). Approve execution?",
                        options=["Approve", "Skip", "Assign to human"],
                    )
                    return

            repo_url = f"https://github.com/{project.owner}/{project.repo}"
            branch_name = f"aipm/issue-{task.github_number}"
            worktree_path = await self.worktree_mgr.create(
                repo_url=repo_url,
                branch_name=branch_name,
                base_branch=project.default_branch,
                task_id=task.id,
            )

            if not worktree_path:
                await self.store.update_task_status(task.id, TaskStatus.FAILED)
                return

            execution_mode = self.budget.select_execution_mode(model)
            run = await self.worker.execute(
                task=task,
                worktree_path=worktree_path,
                model=model,
                execution_mode=execution_mode,
            )

            if run.status == RunStatus.SUCCEEDED:
                qa_result = await self.qa.run_checks(
                    worktree_path, run, None
                )
                if qa_result.passed:
                    await self._publish_results(task, run, project, worktree_path, branch_name)
                    await self._notify_success(task, run)
                else:
                    await self._handle_failure(task, run)
            else:
                await self._handle_failure(task, run)

        except Exception as e:
            logger.exception(f"Background work failed for {task.id}: {e}")
            await self.store.update_task_status(task.id, TaskStatus.FAILED)

    # --- PR Creation (Gap 3) ---

    async def _publish_results(
        self,
        task: Task,
        run: WorkRun,
        project,
        worktree_path: Path,
        branch_name: str,
    ) -> None:
        """After QA passes: push branch → create PR → comment on issue → update run."""
        try:
            # Push branch
            pushed = await self.github_actions.push_branch(
                str(worktree_path), branch_name
            )
            if not pushed:
                logger.error(f"Failed to push branch {branch_name} for task {task.id}")
                await self.store.update_task_status(task.id, TaskStatus.IN_REVIEW)
                return

            # Create PR
            pr_title = f"[AIPM] Fix #{task.github_number}: {task.title}"
            pr_body = (
                f"Automated fix for #{task.github_number}\n\n"
                f"**Issue:** {task.title}\n"
                f"**Model:** {run.model_used}\n"
                f"**Duration:** {run.duration_seconds}s\n\n"
                f"---\n"
                f"*Generated by AIPM*"
            )

            pr_data = await self.github_actions.create_pull_request(
                owner=project.owner,
                repo=project.repo,
                title=pr_title,
                body=pr_body,
                head_branch=branch_name,
                base_branch=project.default_branch,
            )

            if pr_data:
                pr_url = pr_data.get("html_url", "")
                pr_number = pr_data.get("number", 0)

                # Update run with PR info
                await self.store.update_run(
                    run.id, pr_url=pr_url, pr_number=pr_number
                )
                # Store on run object for downstream use
                run.pr_number = pr_number
                run.pr_url = pr_url

                # Comment on the original issue
                await self.github_actions.comment_on_issue(
                    owner=project.owner,
                    repo=project.repo,
                    issue_number=task.github_number,
                    body=f"AIPM has created a pull request to address this issue: #{pr_number}\n\n"
                         f"Model used: {run.model_used}",
                )

                logger.info(f"Published PR #{pr_number} for task {task.id}")

                # Check Render preview if applicable
                preview = await self._check_render_preview(project, branch_name)
                if preview:
                    preview_status = preview.get("status", "unknown")
                    preview_note = f"\n**Preview:** {preview_status}"
                    if preview.get("url"):
                        preview_note += f" — {preview['url']}"
                    await self.github_actions.comment_on_issue(
                        owner=project.owner,
                        repo=project.repo,
                        issue_number=task.github_number,
                        body=f"Render preview deploy: {preview_status}{' — ' + preview['url'] if preview.get('url') else ''}",
                    )

            # Task stays IN_REVIEW until user merges/rejects via Telegram
            await self.store.update_task_status(task.id, TaskStatus.IN_REVIEW)

        except Exception as e:
            logger.error(f"Failed to publish results for {task.id}: {e}")
            # Still mark as IN_REVIEW so it's not lost
            await self.store.update_task_status(task.id, TaskStatus.IN_REVIEW)

    # --- Render Preview Check (Feature 3) ---

    async def _check_render_preview(
        self, project, branch_name: str
    ) -> Optional[dict]:
        """Check Render PR preview status if the project uses Render with a service_id."""
        if (
            not getattr(project, "deploy_platform", None) == "render"
            or not getattr(project, "deploy_service_id", None)
        ):
            return None

        try:
            from ..integrations.render_client import RenderClient

            client = RenderClient(project.deploy_service_id)
            result = await client.check_preview_health(branch_name, timeout=120)
            await client.close()
            logger.info(
                f"Render preview for {branch_name}: "
                f"found={result.get('found')}, status={result.get('status')}"
            )
            return result if result.get("found") else None
        except Exception as e:
            logger.warning(f"Render preview check failed: {e}")
            return None

    # --- Retry Logic (Gap 4) ---

    def _should_retry(self, failed_count: int) -> bool:
        """Check if a task should be retried based on failure count."""
        max_retries = self.settings.aipm.max_retries_per_issue
        return failed_count < max_retries

    async def _handle_failure(self, task: Task, run: WorkRun) -> None:
        """Handle a failed task: retry or escalate."""
        failed_count = await self.store.count_failed_runs(task.id)

        if self._should_retry(failed_count):
            # Reset to BACKLOG for retry
            await self.store.update_task_status(task.id, TaskStatus.BACKLOG)
            logger.info(
                f"Task {task.id} failed (attempt {failed_count}/"
                f"{self.settings.aipm.max_retries_per_issue}), "
                f"returning to backlog for retry"
            )
        else:
            # Retries exhausted — request human decision
            await self.store.update_task_status(task.id, TaskStatus.FAILED)
            logger.warning(
                f"Task {task.id} failed after {failed_count} attempts, "
                f"requesting human decision"
            )
            await self._request_decision(
                task,
                question=(
                    f"Task failed after {failed_count} attempts. "
                    f"Last error: {(run.error_message or 'Unknown')[:200]}"
                ),
                options=[
                    "Retry with different model",
                    "Skip this issue",
                    "Assign to human",
                ],
                run_id=run.id,
            )
            # Notify failure
            await self._notify_failure(task, run)

    # --- Decision Creation (Gap 8) ---

    async def _request_decision(
        self,
        task: Task,
        question: str,
        options: list[str],
        run_id: Optional[str] = None,
    ) -> None:
        """Create a decision record, set task to NEEDS_HUMAN, and send Telegram notification."""
        decision = Decision(
            id="",
            task_id=task.id,
            run_id=run_id,
            question=question,
            options=options,
            status=DecisionStatus.PENDING,
        )
        decision_id = await self.store.create_decision(decision)

        await self.store.update_task_status(task.id, TaskStatus.NEEDS_HUMAN)

        # Send Telegram notification
        if self._telegram:
            msg_id = await self._telegram.notify_decision_needed(
                task_id=task.id,
                title=task.title,
                question=question,
                options=options,
            )
            if msg_id:
                # Store the telegram message ID on the decision for callback routing
                await self.store.resolve_decision(decision_id, "")
                # Re-create as pending with telegram_message_id
                # (since resolve_decision marks as answered, we need to update directly)
                await self.store.db.execute(
                    "UPDATE decisions SET status = 'pending', decision = NULL, "
                    "telegram_message_id = ?, answered_at = NULL WHERE id = ?",
                    (str(msg_id), decision_id),
                )
                await self.store.db.commit()

        logger.info(f"Decision requested for task {task.id}: {question}")

    # --- Telegram Notifications (Gap 5) ---

    async def _notify_success(self, task: Task, run: WorkRun) -> None:
        """Notify via Telegram that a task succeeded."""
        if self._telegram:
            if run.pr_number:
                # Send PR notification with merge/reject buttons
                project = await self.store.get_project(task.project_id)
                if project:
                    await self._telegram.notify_pr_ready(
                        task_id=task.id,
                        title=task.title,
                        pr_number=run.pr_number,
                        pr_url=run.pr_url or "",
                        owner=project.owner,
                        repo=project.repo,
                    )
                    return
            # Fallback: no PR created
            await self._telegram.notify_task_completed(
                task_id=task.id,
                title=task.title,
                model=run.model_used,
                duration=run.duration_seconds,
                passed_qa=True,
            )

    async def _notify_failure(self, task: Task, run: WorkRun) -> None:
        """Notify via Telegram that a task has permanently failed (retries exhausted)."""
        if self._telegram:
            await self._telegram.notify_task_failed(
                task_id=task.id,
                title=task.title,
                error=run.error_message or "Unknown error",
            )

    async def _notify_budget_warning(self, spent: float, limit: float) -> None:
        """Send a budget warning via Telegram (once per budget exhaustion)."""
        if self._telegram and not self._budget_warning_sent:
            await self._telegram.notify_budget_warning(spent, limit)
            self._budget_warning_sent = True
