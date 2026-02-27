"""Scheduler — main work loop that ties everything together."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from ..config import Settings
from ..db.models import RunStatus, TaskStatus
from ..db.store import Store
from ..github.sync import GitHubSync
from .budget import BudgetTracker
from .qa import QARunner
from .router import TaskRouter
from .worker import Worker
from .worktree import WorktreeManager

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
        self._running = False

    async def run_once(self) -> dict:
        """Run a single cycle: sync → pick task → execute → QA.

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
            test_command = project.test_command
            qa_result = await self.qa.run_checks(worktree_path, run, test_command)
            summary["qa"] = {
                "passed": qa_result.passed,
                "issues": qa_result.issues,
            }

            if qa_result.passed:
                await self.store.update_task_status(task.id, TaskStatus.IN_REVIEW)
                logger.info(f"Task {task.id} completed and passed QA!")
            else:
                await self.store.update_task_status(task.id, TaskStatus.FAILED)
                logger.warning(f"Task {task.id} failed QA: {qa_result.issues}")
        else:
            await self.store.update_task_status(task.id, TaskStatus.FAILED)

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
                    active_runs = await self.store.count_active_runs()
                    if active_runs < self.settings.aipm.max_concurrent_workers:
                        task = await self.store.get_next_task()
                        if task:
                            # Run work in background
                            asyncio.create_task(self._work_on_task(task))

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
        await self.github_sync.close()

    async def _work_on_task(self, task) -> None:
        """Background task to work on a single issue."""
        try:
            complexity, model = await self.router.classify_and_route(task)
            project = await self.store.get_project(task.project_id)
            if not project:
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
                    worktree_path, run, project.test_command
                )
                if qa_result.passed:
                    await self.store.update_task_status(task.id, TaskStatus.IN_REVIEW)
                else:
                    await self.store.update_task_status(task.id, TaskStatus.FAILED)
            else:
                await self.store.update_task_status(task.id, TaskStatus.FAILED)

        except Exception as e:
            logger.exception(f"Background work failed for {task.id}: {e}")
            await self.store.update_task_status(task.id, TaskStatus.FAILED)
