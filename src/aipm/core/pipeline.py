"""Pipeline runner — enforces mandatory 8-step workflow for every task."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ..config import Settings
from ..db.models import (
    Decision,
    LearningCategory,
    PipelineStep,
    PipelineStepName,
    PipelineStepStatus,
    RunStatus,
    TaskStatus,
    WorkRun,
)
from ..db.store import Store
from .qa import QARunner
from .worker import Worker
from .worktree import WorktreeManager

if TYPE_CHECKING:
    from ..db.models import Task
    from ..github.actions import GitHubActions
    from ..integrations.telegram_bot import TelegramNotifier

logger = logging.getLogger("aipm.core.pipeline")


@dataclass
class PipelineResult:
    run: WorkRun
    steps: list[PipelineStep]
    passed: bool = False


class PipelineRunner:
    """Enforces the mandatory 8-step pipeline for every task.

    Steps: CLASSIFY → EXECUTE → TEST → VERIFY_ENV → CODE_REVIEW → CREATE_PR → REQUEST_MERGE → DOCUMENT

    All steps are recorded in pipeline_steps. If the process crashes, we can see
    exactly where it stopped.
    """

    STEPS = list(PipelineStepName)

    def __init__(
        self,
        settings: Settings,
        store: Store,
        worker: Worker,
        qa: QARunner,
        worktree_mgr: WorktreeManager,
        github_actions: GitHubActions,
    ):
        self.settings = settings
        self.store = store
        self.worker = worker
        self.qa = qa
        self.worktree_mgr = worktree_mgr
        self.github_actions = github_actions
        self._telegram: Optional[TelegramNotifier] = None

    def set_telegram(self, telegram: TelegramNotifier) -> None:
        self._telegram = telegram

    async def run(
        self,
        task: Task,
        project,
        worktree_path: Path,
        model: str,
        execution_mode: str,
        complexity_str: str,
        branch_name: str,
    ) -> PipelineResult:
        """Run the full 8-step pipeline. Returns PipelineResult."""

        # Create run record
        run = WorkRun(
            id="",
            task_id=task.id,
            model_used=model,
            execution_mode=execution_mode,
            branch_name=branch_name,
        )
        run_id = await self.store.create_run(run)
        run.id = run_id

        # Create all 8 steps as PENDING upfront (crash-safe)
        steps = await self.store.create_pipeline_steps(run.id)
        step_map = {s.step_name: s for s in steps}

        all_gates_passed = True

        # ── Step 1: CLASSIFY — already done by caller, just record ──
        await self._mark_step(step_map[PipelineStepName.CLASSIFY], PipelineStepStatus.PASSED,
                              output_summary=f"complexity={complexity_str}, model={model}")

        # ── Step 2: EXECUTE — run worker ──
        await self._mark_step(step_map[PipelineStepName.EXECUTE], PipelineStepStatus.RUNNING)
        try:
            run = await self._execute_step(task, worktree_path, model, execution_mode, run)
            if run.status == RunStatus.SUCCEEDED:
                await self._mark_step(step_map[PipelineStepName.EXECUTE], PipelineStepStatus.PASSED,
                                      output_summary=run.result_summary)
            else:
                await self._mark_step(step_map[PipelineStepName.EXECUTE], PipelineStepStatus.FAILED,
                                      output_summary=run.error_message)
                all_gates_passed = False
                # Skip remaining except DOCUMENT
                await self._skip_remaining(
                    step_map, after=PipelineStepName.EXECUTE,
                    reason=f"Execution failed: {(run.error_message or 'Unknown')[:200]}",
                    task=task,
                )
                # DOCUMENT always runs
                await self._run_document_step(step_map, task, run, project, model, complexity_str,
                                              all_gates_passed=False)
                steps = await self.store.get_pipeline_steps(run.id)
                await self._send_pipeline_summary(task, steps)
                return PipelineResult(run=run, steps=steps, passed=False)
        except Exception as e:
            logger.exception(f"Pipeline EXECUTE failed for {task.id}: {e}")
            await self._mark_step(step_map[PipelineStepName.EXECUTE], PipelineStepStatus.FAILED,
                                  output_summary=str(e)[:500])
            await self._skip_remaining(step_map, after=PipelineStepName.EXECUTE,
                                       reason=f"Exception during execution: {e}", task=task)
            await self._run_document_step(step_map, task, run, project, model, complexity_str,
                                          all_gates_passed=False)
            steps = await self.store.get_pipeline_steps(run.id)
            await self._send_pipeline_summary(task, steps)
            return PipelineResult(run=run, steps=steps, passed=False)

        # ── Step 3: TEST — hard gate ──
        await self._mark_step(step_map[PipelineStepName.TEST], PipelineStepStatus.RUNNING)
        test_output, test_passed = await self._run_test_step(worktree_path)
        if test_passed:
            await self._mark_step(step_map[PipelineStepName.TEST], PipelineStepStatus.PASSED,
                                  output_summary="Tests passed")
        elif test_output is None:
            # No tests detected — notify and continue
            await self._mark_step(
                step_map[PipelineStepName.TEST], PipelineStepStatus.SKIPPED_WITH_REASON,
                skip_reason="No test framework detected",
                output_summary="No tests found",
            )
            await self._notify_step_skipped(task, PipelineStepName.TEST, "No test framework detected")
        else:
            await self._mark_step(step_map[PipelineStepName.TEST], PipelineStepStatus.FAILED,
                                  output_summary=test_output[:500])
            all_gates_passed = False

        # ── Step 4: VERIFY_ENV — check dev env impact ──
        await self._mark_step(step_map[PipelineStepName.VERIFY_ENV], PipelineStepStatus.RUNNING)
        env_result = await self._run_verify_env_step(project, worktree_path)
        if env_result["method"] == "none":
            await self._mark_step(
                step_map[PipelineStepName.VERIFY_ENV], PipelineStepStatus.SKIPPED_WITH_REASON,
                skip_reason="No dev env config available",
                output_summary=env_result["details"],
            )
            await self._notify_step_skipped(
                task, PipelineStepName.VERIFY_ENV, "No dev env config — consider adding [dev_env]",
            )
            # Record a DEV_ENV_GAP learning
            await self.store.record_learning(
                task.project_id, LearningCategory.DEV_ENV_GAP,
                f"No dev env config for {task.project_id}. Add [dev_env] to config.toml.",
                source_run_id=run.id,
            )
        else:
            await self._mark_step(step_map[PipelineStepName.VERIFY_ENV], PipelineStepStatus.PASSED,
                                  output_summary=f"Verified via {env_result['method']}")

        # ── Step 5: CODE_REVIEW ──
        await self._mark_step(step_map[PipelineStepName.CODE_REVIEW], PipelineStepStatus.RUNNING)
        qa_result = await self.qa.run_checks(worktree_path, run, None, project=project)

        review_passed = qa_result.passed
        review_score = qa_result.review_score

        if review_passed:
            await self._mark_step(step_map[PipelineStepName.CODE_REVIEW], PipelineStepStatus.PASSED,
                                  output_summary=f"score={review_score}: {qa_result.review_summary}")
        else:
            await self._mark_step(step_map[PipelineStepName.CODE_REVIEW], PipelineStepStatus.FAILED,
                                  output_summary=f"score={review_score}: {qa_result.issues}")
            all_gates_passed = False

        # Extract learnings from QA review
        await self._extract_learnings_from_qa(task, run, qa_result)

        # ── Gate: if TEST or CODE_REVIEW failed, skip PR/merge ──
        if not all_gates_passed:
            await self._skip_remaining(
                step_map, after=PipelineStepName.CODE_REVIEW,
                reason="TEST or CODE_REVIEW gate failed",
                task=task,
            )
            await self.store.update_run(run.id, status=RunStatus.FAILED)
            await self._run_document_step(step_map, task, run, project, model, complexity_str,
                                          all_gates_passed=False, qa_result=qa_result)
            steps = await self.store.get_pipeline_steps(run.id)
            await self._send_pipeline_summary(task, steps)
            return PipelineResult(run=run, steps=steps, passed=False)

        # ── Step 6: CREATE_PR ──
        await self._mark_step(step_map[PipelineStepName.CREATE_PR], PipelineStepStatus.RUNNING)
        pr_data = await self._create_pr_step(task, run, project, worktree_path, branch_name)
        if pr_data:
            await self._mark_step(step_map[PipelineStepName.CREATE_PR], PipelineStepStatus.PASSED,
                                  output_summary=f"PR #{pr_data.get('number')}: {pr_data.get('html_url')}")
        else:
            await self._mark_step(step_map[PipelineStepName.CREATE_PR], PipelineStepStatus.FAILED,
                                  output_summary="Failed to create PR")
            all_gates_passed = False

        # ── Step 7: REQUEST_MERGE ──
        if pr_data and run.pr_number:
            await self._mark_step(step_map[PipelineStepName.REQUEST_MERGE], PipelineStepStatus.RUNNING)
            merge_notified = await self._request_merge_step(task, run, project)
            if merge_notified:
                await self._mark_step(step_map[PipelineStepName.REQUEST_MERGE], PipelineStepStatus.PASSED,
                                      output_summary="Merge request sent via Telegram")
            else:
                await self._mark_step(
                    step_map[PipelineStepName.REQUEST_MERGE], PipelineStepStatus.SKIPPED_WITH_REASON,
                    skip_reason="Telegram unavailable",
                )
            await self.store.update_task_status(task.id, TaskStatus.IN_REVIEW)
        else:
            await self._mark_step(
                step_map[PipelineStepName.REQUEST_MERGE], PipelineStepStatus.SKIPPED_WITH_REASON,
                skip_reason="No PR to merge",
            )

        # Increment relevance for injected learnings that contributed to success
        if self.worker._last_injected_learning_ids:
            await self.store.increment_learning_relevance(self.worker._last_injected_learning_ids)

        # ── Step 8: DOCUMENT — ALWAYS runs ──
        await self._run_document_step(step_map, task, run, project, model, complexity_str,
                                      all_gates_passed=all_gates_passed, qa_result=qa_result)

        steps = await self.store.get_pipeline_steps(run.id)
        await self._send_pipeline_summary(task, steps)
        return PipelineResult(run=run, steps=steps, passed=all_gates_passed)

    # ── Step implementations ────────────────────────────────────────────

    async def _execute_step(
        self, task: Task, worktree_path: Path, model: str, execution_mode: str, run: WorkRun,
    ) -> WorkRun:
        """Run the worker to execute the coding task. Reuses existing run record."""
        result = await self.worker.execute(
            task=task,
            worktree_path=worktree_path,
            model=model,
            execution_mode=execution_mode,
        )
        # The worker creates its own run; update our run with its results
        await self.store.update_run(
            run.id,
            status=result.status,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=result.cost_usd,
            duration_seconds=result.duration_seconds,
            quality_score=result.quality_score,
            result_summary=result.result_summary,
            error_message=result.error_message,
        )
        run.status = result.status
        run.error_message = result.error_message
        run.result_summary = result.result_summary
        run.duration_seconds = result.duration_seconds
        run.tokens_in = result.tokens_in
        run.tokens_out = result.tokens_out
        run.cost_usd = result.cost_usd
        return run

    async def _run_test_step(self, worktree_path: Path) -> tuple[Optional[str], bool]:
        """Run tests. Returns (output, passed). output=None if no tests detected."""
        cmd = self.qa._detect_test_command(worktree_path)
        if not cmd:
            return None, False
        result = await self.qa._run_command(cmd, worktree_path)
        return result["output"], result["returncode"] == 0

    async def _run_verify_env_step(self, project, worktree_path: Path) -> dict:
        """Assess dev env capability."""
        dev_env = self.qa._resolve_dev_env(project)
        return self.qa.assess_dev_env_capability(project, worktree_path, dev_env=dev_env)

    async def _create_pr_step(
        self, task: Task, run: WorkRun, project, worktree_path: Path, branch_name: str,
    ) -> Optional[dict]:
        """Push branch and create PR. Returns PR data or None."""
        try:
            pushed = await self.github_actions.push_branch(str(worktree_path), branch_name)
            if not pushed:
                logger.error(f"Failed to push branch {branch_name} for task {task.id}")
                return None

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
                await self.store.update_run(run.id, pr_url=pr_url, pr_number=pr_number)
                run.pr_number = pr_number
                run.pr_url = pr_url

                await self.github_actions.comment_on_issue(
                    owner=project.owner,
                    repo=project.repo,
                    issue_number=task.github_number,
                    body=(
                        f"AIPM has created a pull request to address this issue: #{pr_number}\n\n"
                        f"Model used: {run.model_used}"
                    ),
                )
                logger.info(f"Published PR #{pr_number} for task {task.id}")
                return pr_data

            return None
        except Exception as e:
            logger.error(f"Failed to create PR for {task.id}: {e}")
            return None

    async def _request_merge_step(self, task: Task, run: WorkRun, project) -> bool:
        """Send Telegram notification with merge/reject buttons. Returns True if sent."""
        if not self._telegram or not run.pr_number:
            return False
        try:
            msg_id = await self._telegram.notify_pr_ready(
                task_id=task.id,
                title=task.title,
                pr_number=run.pr_number,
                pr_url=run.pr_url or "",
                owner=project.owner,
                repo=project.repo,
            )
            return msg_id is not None
        except Exception as e:
            logger.error(f"Failed to send merge request notification: {e}")
            return False

    async def _run_document_step(
        self,
        step_map: dict,
        task: Task,
        run: WorkRun,
        project,
        model: str,
        complexity_str: str,
        all_gates_passed: bool,
        qa_result=None,
    ) -> None:
        """DOCUMENT step — always runs. Records learnings + model performance + wiki."""
        step = step_map[PipelineStepName.DOCUMENT]
        await self._mark_step(step, PipelineStepStatus.RUNNING)
        try:
            # Record model performance
            task_type = self._infer_task_type(task)
            qa_score = qa_result.review_score if qa_result else None
            tests_passed = 1 if (qa_result and qa_result.test_exit_code == 0) else 0

            await self.store.record_model_performance(
                project_id=task.project_id,
                task_type=task_type,
                model_used=model,
                complexity=complexity_str,
                qa_score=qa_score,
                tests_passed=tests_passed,
                succeeded=1 if all_gates_passed else 0,
                duration_seconds=run.duration_seconds,
                run_id=run.id,
            )

            # Record MODEL_FEEDBACK learning if score is poor
            if qa_score is not None and qa_score < 50:
                await self.store.record_learning(
                    task.project_id,
                    LearningCategory.MODEL_FEEDBACK,
                    f"{model} scored {qa_score} on {task_type} task. "
                    f"Consider using a more capable model for similar tasks.",
                    source_run_id=run.id,
                )

            # Record error as learning on failure
            if not all_gates_passed and run.error_message:
                await self.store.record_learning(
                    task.project_id,
                    LearningCategory.ERROR_PATTERN,
                    run.error_message[:500],
                    source_run_id=run.id,
                )

            # Update wiki on successful runs
            if all_gates_passed:
                await self._update_wiki_from_run(task, run, project, qa_result)

            await self._mark_step(step, PipelineStepStatus.PASSED,
                                  output_summary="Documented: model_perf + learnings + wiki recorded")
        except Exception as e:
            logger.error(f"DOCUMENT step failed: {e}")
            await self._mark_step(step, PipelineStepStatus.FAILED, output_summary=str(e)[:500])

    async def _update_wiki_from_run(
        self, task: Task, run: WorkRun, project, qa_result=None,
    ) -> None:
        """Update wiki sections based on a successful run."""
        try:
            sections = await self.store.get_wiki_sections(task.project_id)

            # Bootstrap: if wiki is empty, add an Overview section
            if not sections:
                repo_name = f"{project.owner}/{project.repo}" if project else task.project_id
                await self.store.add_wiki_section(
                    project_id=task.project_id,
                    title="Overview",
                    content=f"Repository: {repo_name}",
                    source_run_id=run.id,
                )
                logger.info(f"Wiki bootstrapped for {task.project_id}")

            # If QA has praise, auto-add a Conventions section if it doesn't exist
            if qa_result and qa_result.review_raw:
                praise_list = qa_result.review_raw.get("praise", [])
                if praise_list:
                    existing = await self.store.get_wiki_section_by_title(
                        task.project_id, "Conventions",
                    )
                    if not existing:
                        conventions = "\n".join(f"- {p}" for p in praise_list if p)
                        if conventions:
                            await self.store.add_wiki_section(
                                project_id=task.project_id,
                                title="Conventions",
                                content=conventions,
                                source_run_id=run.id,
                            )
                            logger.info(f"Wiki 'Conventions' section added for {task.project_id}")
        except Exception as e:
            logger.error(f"Wiki update failed for {task.project_id}: {e}")

    async def _propose_wiki_edit(
        self,
        project_id: str,
        section_id: str,
        section_title: str,
        new_content: str,
        source_run_id: Optional[str] = None,
    ) -> Optional[str]:
        """Propose an edit to an existing wiki section via Decision approval flow."""
        ctx = json.dumps({
            "action": "edit",
            "section_id": section_id,
            "new_content": new_content,
            "source_run_id": source_run_id,
        })
        decision = Decision(
            id="",
            task_id=f"wiki__{section_id}",
            question=f"Approve wiki edit to '{section_title}' in {project_id}?",
            context=ctx,
            options=["Approve", "Reject"],
        )
        decision_id = await self.store.create_decision(decision)

        if self._telegram:
            await self._telegram.notify_decision_needed(
                task_id=f"wiki__{section_id}",
                title=f"Wiki Edit: {section_title}",
                question=f"Approve edit to wiki section '{section_title}'?\n\nNew content preview:\n{new_content[:300]}",
                options=["Approve", "Reject"],
            )
        return decision_id

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _mark_step(
        self,
        step: PipelineStep,
        status: PipelineStepStatus,
        output_summary: Optional[str] = None,
        skip_reason: Optional[str] = None,
    ) -> None:
        """Update a pipeline step status."""
        await self.store.update_pipeline_step(
            step.id, status, output_summary=output_summary, skip_reason=skip_reason,
        )
        step.status = status

    async def _skip_remaining(
        self,
        step_map: dict,
        after: PipelineStepName,
        reason: str,
        task: Task,
    ) -> None:
        """Mark all steps after `after` as SKIPPED_WITH_REASON (except DOCUMENT)."""
        found = False
        for step_name in self.STEPS:
            if step_name == after:
                found = True
                continue
            if found and step_name != PipelineStepName.DOCUMENT:
                step = step_map[step_name]
                if step.status == PipelineStepStatus.PENDING:
                    await self._mark_step(step, PipelineStepStatus.SKIPPED_WITH_REASON,
                                          skip_reason=reason)
                    await self._notify_step_skipped(task, step_name, reason)

    async def _notify_step_skipped(
        self, task: Task, step_name: PipelineStepName, reason: str,
    ) -> None:
        """Notify via Telegram when a step is skipped."""
        if self._telegram:
            await self._telegram.notify_step_skipped(
                task_id=task.id, title=task.title,
                step_name=step_name.value, reason=reason,
            )

    async def _send_pipeline_summary(self, task: Task, steps: list[PipelineStep]) -> None:
        """Send end-of-run pipeline summary via Telegram."""
        if self._telegram:
            await self._telegram.notify_pipeline_summary(
                task_id=task.id, title=task.title, steps=steps,
            )

    async def _extract_learnings_from_qa(self, task: Task, run: WorkRun, qa_result) -> None:
        """Extract learnings from QA review_raw and record them."""
        review = qa_result.review_raw
        if not review:
            return
        for issue in review.get("issues", []):
            desc = issue.get("description", "") if isinstance(issue, dict) else str(issue)
            if desc:
                await self.store.record_learning(
                    task.project_id, LearningCategory.QA_FEEDBACK, desc,
                    source_run_id=run.id,
                )
        for praise in review.get("praise", []):
            if praise:
                await self.store.record_learning(
                    task.project_id, LearningCategory.POSITIVE_PATTERN, str(praise),
                    source_run_id=run.id,
                )

    @staticmethod
    def _infer_task_type(task: Task) -> str:
        """Infer a task type from labels and title for model performance tracking."""
        labels_lower = [lbl.lower() for lbl in (task.labels or [])]
        title_lower = task.title.lower()

        if "bug" in labels_lower or "fix" in title_lower:
            return "bug_fix"
        if "enhancement" in labels_lower or "feature" in labels_lower:
            return "feature"
        if "refactor" in labels_lower or "refactor" in title_lower:
            return "refactor"
        if "docs" in labels_lower or "documentation" in labels_lower:
            return "docs"
        if "test" in labels_lower or "test" in title_lower:
            return "test"
        return "general"
