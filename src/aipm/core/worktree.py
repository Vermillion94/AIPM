"""Git worktree management — isolate each task in its own working copy."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger("aipm.core.worktree")


async def _run_git(
    *args: str, cwd: Optional[str | Path] = None
) -> tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


class WorktreeManager:
    """Manages git worktrees for isolated task execution."""

    def __init__(self, worktrees_dir: Path):
        self.worktrees_dir = worktrees_dir
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)

    async def create(
        self,
        repo_url: str,
        branch_name: str,
        base_branch: str = "main",
        task_id: str = "",
    ) -> Optional[Path]:
        """Clone the repo (if needed) and create a worktree for the task.

        Returns the worktree path, or None on failure.
        """
        # Sanitize task_id for directory name — use absolute path so git
        # worktree add resolves correctly regardless of cwd
        safe_name = branch_name.replace("/", "_").replace("#", "_")
        worktree_path = (self.worktrees_dir / safe_name).resolve()

        # We need a bare/shared clone to create worktrees from
        shared_repo = self.worktrees_dir / "_shared_repos"
        shared_repo.mkdir(parents=True, exist_ok=True)

        # Extract owner/repo from URL or repo string
        repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
        repo_dir = shared_repo / repo_name

        if worktree_path.exists():
            logger.info(f"Removing stale worktree to recreate from latest: {worktree_path}")
            await self.cleanup(branch_name)
            # Delete stale branch ref from bare repo so we get a clean start
            if repo_dir.exists():
                await _run_git("branch", "-D", branch_name, cwd=repo_dir)

        if not repo_dir.exists():
            logger.info(f"Cloning {repo_url} into {repo_dir}")
            rc, out, err = await _run_git(
                "clone", "--bare", repo_url, str(repo_dir)
            )
            if rc != 0:
                # Try with .git suffix
                rc, out, err = await _run_git(
                    "clone", "--bare", f"{repo_url}.git", str(repo_dir)
                )
                if rc != 0:
                    logger.error(f"Failed to clone {repo_url}: {err}")
                    return None
        else:
            # Fetch latest
            rc, out, err = await _run_git("fetch", "--all", cwd=repo_dir)
            if rc != 0:
                logger.warning(f"Failed to fetch in {repo_dir}: {err}")

        # Create the worktree
        logger.info(f"Creating worktree: {worktree_path} on branch {branch_name}")
        rc, out, err = await _run_git(
            "worktree", "add", str(worktree_path), "-b", branch_name,
            f"refs/heads/{base_branch}",
            cwd=repo_dir,
        )
        if rc != 0:
            # Branch might already exist, try without -b
            rc, out, err = await _run_git(
                "worktree", "add", str(worktree_path), branch_name,
                cwd=repo_dir,
            )
            if rc != 0:
                logger.error(f"Failed to create worktree: {err}")
                return None

        return worktree_path

    async def create_from_local(
        self,
        local_repo: Path,
        branch_name: str,
        base_branch: str = "main",
    ) -> Optional[Path]:
        """Create a worktree from an existing local repo clone."""
        safe_name = branch_name.replace("/", "_").replace("#", "_")
        worktree_path = (self.worktrees_dir / safe_name).resolve()

        if worktree_path.exists():
            return worktree_path

        # Ensure we have latest
        await _run_git("fetch", "origin", cwd=local_repo)

        # Create worktree
        rc, out, err = await _run_git(
            "worktree", "add", str(worktree_path), "-b", branch_name,
            base_branch,
            cwd=local_repo,
        )
        if rc != 0:
            rc, out, err = await _run_git(
                "worktree", "add", str(worktree_path), branch_name,
                cwd=local_repo,
            )
            if rc != 0:
                logger.error(f"Failed to create worktree: {err}")
                return None

        return worktree_path

    async def cleanup(self, branch_name: str) -> bool:
        """Remove a worktree by branch name."""
        safe_name = branch_name.replace("/", "_").replace("#", "_")
        worktree_path = (self.worktrees_dir / safe_name).resolve()

        if not worktree_path.exists():
            return True

        try:
            shutil.rmtree(worktree_path)
            logger.info(f"Cleaned up worktree: {worktree_path}")

            # Prune in shared repos
            shared_repo = self.worktrees_dir / "_shared_repos"
            if shared_repo.exists():
                for repo_dir in shared_repo.iterdir():
                    if repo_dir.is_dir():
                        await _run_git("worktree", "prune", cwd=repo_dir)

            return True
        except Exception as e:
            logger.error(f"Failed to cleanup worktree {worktree_path}: {e}")
            return False

    async def get_diff(self, worktree_path: Path) -> str:
        """Get the git diff of changes in a worktree."""
        rc, out, err = await _run_git("diff", "HEAD", cwd=worktree_path)
        if rc != 0:
            # Try staged + unstaged
            rc, out, err = await _run_git("diff", cwd=worktree_path)
        return out

    async def commit_all(self, worktree_path: Path, message: str) -> Optional[str]:
        """Stage all changes and commit. Returns commit SHA or None."""
        await _run_git("add", "-A", cwd=worktree_path)

        rc, out, err = await _run_git(
            "commit", "-m", message, cwd=worktree_path
        )
        if rc != 0:
            if "nothing to commit" in err or "nothing to commit" in out:
                logger.info("Nothing to commit")
                return None
            logger.error(f"Commit failed: {err}")
            return None

        rc, sha, err = await _run_git("rev-parse", "HEAD", cwd=worktree_path)
        return sha if rc == 0 else None
