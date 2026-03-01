"""Quality assurance — run tests, evaluate output quality."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..config import Settings
from ..db.models import WorkRun
from ..db.store import Store
from ..integrations.anthropic_client import AnthropicClient
from ..prompts.review import build_code_review_prompt

logger = logging.getLogger("aipm.core.qa")


@dataclass
class QAResult:
    passed: bool
    test_output: str = ""
    test_exit_code: int = -1
    review_score: Optional[int] = None
    review_summary: str = ""
    issues: list[str] = field(default_factory=list)


class QARunner:
    """Runs quality checks on completed work."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    async def run_checks(
        self,
        worktree_path: Path,
        run: WorkRun,
        test_command: Optional[str] = None,
    ) -> QAResult:
        """Run all QA checks on a worktree. Returns overall QA result."""
        result = QAResult(passed=True)

        # 1. Auto-detect and run tests
        cmd = test_command or self._detect_test_command(worktree_path)
        if cmd:
            test_result = await self._run_command(cmd, worktree_path)
            result.test_output = test_result["output"]
            result.test_exit_code = test_result["returncode"]
            if test_result["returncode"] != 0:
                result.passed = False
                result.issues.append(f"Tests failed (exit code {test_result['returncode']})")
                logger.warning(f"Tests failed for run {run.id}")

        # 2. Check if there are any actual changes
        has_changes = await self._has_changes(worktree_path)
        if not has_changes:
            result.passed = False
            result.issues.append("No changes were made")
            logger.warning(f"No changes in worktree for run {run.id}")

        # 3. Code review (via Opus)
        review = await self._run_code_review(worktree_path, run)
        if review:
            result.review_score = review.get("score")
            result.review_summary = review.get("summary", "")
            if not review.get("approved", True):
                result.passed = False
                result.issues.append(
                    f"Code review failed (score {review.get('score')}): "
                    f"{review.get('summary')}"
                )

        return result

    async def _run_code_review(
        self, worktree_path: Path, run: WorkRun
    ) -> Optional[dict]:
        """Run AI code review on the changes. Returns review dict or None."""
        try:
            # Get the diff
            diff_result = await self._run_command("git diff HEAD~1", worktree_path)
            if diff_result["returncode"] != 0 or not diff_result["output"].strip():
                logger.info(f"No diff available for code review (run {run.id})")
                return None

            # Load task context
            task = await self.store.get_task(run.task_id)
            title = task.title if task else ""
            body = task.body if task else ""

            # Build the review prompt
            prompt = build_code_review_prompt(
                issue_title=title,
                issue_body=body,
                diff=diff_result["output"],
                test_output="",
            )

            # Call the review model
            client = AnthropicClient(self.settings)
            review = client.review_code(prompt)

            # Record API cost
            from .budget import BudgetTracker

            estimated_cost = BudgetTracker.estimate_cost(
                self.settings.models.review,
                tokens_in=len(prompt) // 4,
                tokens_out=256,
            )
            await self.store.record_credit(
                run_id=run.id,
                model=self.settings.models.review,
                source="api",
                tokens_in=len(prompt) // 4,
                tokens_out=256,
                cost_usd=estimated_cost,
            )

            logger.info(
                f"Code review for run {run.id}: "
                f"score={review.get('score')}, approved={review.get('approved')}"
            )
            return review

        except Exception as e:
            logger.error(f"Code review failed for run {run.id}: {e}")
            return None

    def _detect_test_command(self, worktree_path: Path) -> Optional[str]:
        """Auto-detect the test command based on project files."""
        # Python projects
        if (worktree_path / "pytest.ini").exists() or (
            worktree_path / "pyproject.toml"
        ).exists():
            if (worktree_path / "pyproject.toml").exists():
                content = (worktree_path / "pyproject.toml").read_text()
                if "pytest" in content or "[tool.pytest" in content:
                    return "python -m pytest --tb=short -q"
            if (worktree_path / "setup.py").exists() or (
                worktree_path / "requirements.txt"
            ).exists():
                return "python -m pytest --tb=short -q"

        # Node.js projects
        if (worktree_path / "package.json").exists():
            import json

            try:
                pkg = json.loads((worktree_path / "package.json").read_text())
                scripts = pkg.get("scripts", {})
                if "test" in scripts:
                    test_val = scripts["test"]
                    # Skip placeholder test scripts
                    if "no test specified" not in test_val and "exit 1" not in test_val:
                        return "npm test"
            except Exception:
                pass

        # Makefile
        if (worktree_path / "Makefile").exists():
            content = (worktree_path / "Makefile").read_text()
            if "test:" in content:
                return "make test"

        return None

    async def _run_command(
        self, command: str, cwd: Path, timeout: int = 120
    ) -> dict:
        """Run a shell command and return result."""
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            output = stdout.decode(errors="replace") + stderr.decode(errors="replace")
            return {
                "returncode": proc.returncode,
                "output": output[-5000:],  # cap output size
            }
        except asyncio.TimeoutError:
            return {"returncode": -1, "output": f"Command timed out after {timeout}s"}
        except Exception as e:
            return {"returncode": -1, "output": str(e)}

    async def _has_changes(self, worktree_path: Path) -> bool:
        """Check if the worktree has uncommitted or new committed changes."""
        result = await self._run_command("git diff HEAD~1 --stat", worktree_path)
        if result["returncode"] == 0 and result["output"].strip():
            return True
        # Check for uncommitted changes
        result = await self._run_command("git status --porcelain", worktree_path)
        return bool(result["output"].strip())
