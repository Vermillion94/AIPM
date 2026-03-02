"""Worker — executes coding tasks via Claude Code CLI or Anthropic API."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from ..config import Settings
from ..db.models import RunStatus, Task, TaskStatus, WorkRun
from ..db.store import Store
from ..prompts.coding import build_coding_prompt, build_retry_prompt
from ..prompts.wiki import format_wiki_for_prompt
from .budget import BudgetTracker

logger = logging.getLogger("aipm.core.worker")


class Worker:
    """Executes a coding task using Claude Code CLI or Anthropic SDK."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.budget = BudgetTracker(settings, store)
        self._last_injected_learning_ids: list[str] = []

    async def execute(
        self,
        task: Task,
        worktree_path: Path,
        model: str = "sonnet",
        execution_mode: str = "cli",
        approach: str = "",
    ) -> WorkRun:
        """Execute a task and return the work run result."""
        # Parse project info
        project_repo = task.project_id

        # Create the work run record
        branch_name = f"aipm/issue-{task.github_number}"
        run = WorkRun(
            id="",  # will be set by store
            task_id=task.id,
            model_used=model,
            execution_mode=execution_mode,
            branch_name=branch_name,
        )
        run_id = await self.store.create_run(run)
        run.id = run_id

        # Fetch project learnings
        learnings = await self.store.get_learnings(task.project_id, limit=15)
        self._last_injected_learning_ids = [lr.id for lr in learnings]
        learnings_text = ""
        if learnings:
            learnings_text = "\n".join(
                f"- [{lr.category.value}] {lr.content}" for lr in learnings
            )

        # Fetch project wiki
        wiki_sections = await self.store.get_wiki_sections(task.project_id)
        wiki_text = format_wiki_for_prompt(wiki_sections)

        # Build the prompt
        prompt = build_coding_prompt(
            issue_number=task.github_number,
            project_repo=project_repo,
            title=task.title,
            body=task.body,
            labels=task.labels,
            approach=approach,
            learnings=learnings_text,
            wiki=wiki_text,
        )

        # Check for previous failed runs and inject retry context
        failed_count = await self.store.count_failed_runs(task.id)
        if failed_count > 0:
            latest_failed = await self.store.get_latest_failed_run(task.id)
            if latest_failed:
                error_output = latest_failed.error_message or latest_failed.result_summary or "Unknown error"
                prompt = build_retry_prompt(
                    original_prompt=prompt,
                    error_output=error_output,
                    attempt=failed_count + 1,
                )
                logger.info(f"Retry attempt {failed_count + 1} for task {task.id}")

        # Set task to in_progress
        await self.store.update_task_status(task.id, TaskStatus.IN_PROGRESS)

        start_time = time.time()

        try:
            if execution_mode == "cli":
                result = await self._execute_cli(prompt, worktree_path, model, run_id)
            else:
                result = await self._execute_sdk(prompt, worktree_path, model, run_id)

            duration = int(time.time() - start_time)

            if result["success"]:
                await self.store.update_run(
                    run_id,
                    status=RunStatus.SUCCEEDED,
                    result_summary=result.get("summary", "Task completed"),
                    duration_seconds=duration,
                    tokens_in=result.get("tokens_in", 0),
                    tokens_out=result.get("tokens_out", 0),
                    cost_usd=result.get("cost_usd", 0.0),
                )
                logger.info(f"Worker completed task {task.id} in {duration}s")
            else:
                await self.store.update_run(
                    run_id,
                    status=RunStatus.FAILED,
                    error_message=result.get("error", "Unknown error"),
                    duration_seconds=duration,
                    tokens_in=result.get("tokens_in", 0),
                    tokens_out=result.get("tokens_out", 0),
                    cost_usd=result.get("cost_usd", 0.0),
                )
                logger.error(f"Worker failed task {task.id}: {result.get('error')}")

        except Exception as e:
            duration = int(time.time() - start_time)
            await self.store.update_run(
                run_id,
                status=RunStatus.FAILED,
                error_message=str(e),
                duration_seconds=duration,
            )
            logger.exception(f"Worker crashed on task {task.id}")

        # Refresh run from DB
        updated_run = await self.store.get_run(run_id)
        return updated_run or run

    async def _execute_cli(
        self, prompt: str, worktree_path: Path, model: str, run_id: str
    ) -> dict:
        """Execute via Claude Code CLI (uses subscription credits)."""
        # Map model names to CLI model flags
        model_map = {
            "opus": "opus",
            "sonnet": "sonnet",
            "haiku": "haiku",
            "claude-opus-4-6": "opus",
            "claude-sonnet-4-6": "sonnet",
            "claude-haiku-4-5-20251001": "haiku",
        }
        cli_model = model_map.get(model, "sonnet")

        cmd = [
            "/usr/bin/claude",
            "--print",
            "--model", cli_model,
            "--output-format", "text",
            "--max-turns", "50",
            "--dangerously-skip-permissions",
            prompt,
        ]

        logger.info(f"Executing Claude CLI with model={cli_model} in {worktree_path}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(worktree_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=600  # 10 min timeout
            )

            stdout_text = stdout.decode(errors="replace")
            stderr_text = stderr.decode(errors="replace")

            # Save logs
            log_dir = self.settings.logs_path
            log_file = log_dir / f"run_{int(time.time())}.log"
            log_file.write_text(
                f"=== STDOUT ===\n{stdout_text}\n\n=== STDERR ===\n{stderr_text}"
            )

            # Estimate tokens from text length for CLI mode (no actual usage data)
            # Rough heuristic: ~4 chars per token
            tokens_in = len(prompt) // 4
            tokens_out = len(stdout_text) // 4
            cost_usd = self.budget.estimate_cost(model, tokens_in, tokens_out)

            # Record credit usage
            await self.store.record_credit(
                run_id=run_id,
                model=model,
                source="cli",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
            )

            if proc.returncode == 0:
                return {
                    "success": True,
                    "summary": stdout_text[-500:] if stdout_text else "Completed",
                    "log_path": str(log_file),
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "cost_usd": cost_usd,
                }
            else:
                return {
                    "success": False,
                    "error": stderr_text[-1000:] if stderr_text else f"Exit code {proc.returncode}",
                    "log_path": str(log_file),
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "cost_usd": cost_usd,
                }

        except asyncio.TimeoutError:
            return {"success": False, "error": "Execution timed out after 600s"}
        except FileNotFoundError:
            return {
                "success": False,
                "error": "Claude CLI not found. Ensure 'claude' is in PATH.",
            }

    async def _execute_sdk(
        self, prompt: str, worktree_path: Path, model: str, run_id: str
    ) -> dict:
        """Execute via Anthropic SDK (uses API tokens).

        This is a simpler approach — just calls the API and applies the response.
        For full Agent SDK support, this would use claude_agent_sdk.query().
        """
        try:
            from ..integrations.anthropic_client import AnthropicClient

            client = AnthropicClient(self.settings)

            # Use raw API call to capture usage
            import anthropic

            api_client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
            message = api_client.messages.create(
                model=model if model.startswith("claude-") else f"claude-{model}-4-6",
                max_tokens=4096,
                system="You are a skilled software engineer. Provide the code changes needed to resolve the issue. Show the full file contents for each file that needs to change.",
                messages=[{"role": "user", "content": prompt}],
            )

            # Extract actual usage from API response
            tokens_in = message.usage.input_tokens
            tokens_out = message.usage.output_tokens
            cost_usd = self.budget.estimate_cost(model, tokens_in, tokens_out)

            # Record credit usage
            await self.store.record_credit(
                run_id=run_id,
                model=model,
                source="sdk",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
            )

            response_text = message.content[0].text if message.content else ""

            return {
                "success": True,
                "summary": response_text[:500] if response_text else "Completed",
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": cost_usd,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
