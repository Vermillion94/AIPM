"""GitHub actions — create PRs, post comments, add labels."""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from ..config import Settings

logger = logging.getLogger("aipm.github.actions")

GITHUB_API = "https://api.github.com"


class GitHubActions:
    """Push results back to GitHub — PRs, comments, labels."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            if self.settings.github.token:
                headers["Authorization"] = f"Bearer {self.settings.github.token}"
            self._client = httpx.AsyncClient(
                base_url=GITHUB_API,
                headers=headers,
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str = "main",
    ) -> Optional[dict]:
        """Create a pull request on GitHub. Returns PR data or None on failure."""
        client = await self._get_client()
        try:
            response = await client.post(
                f"/repos/{owner}/{repo}/pulls",
                json={
                    "title": title,
                    "body": body,
                    "head": head_branch,
                    "base": base_branch,
                },
            )
            response.raise_for_status()
            pr_data = response.json()
            logger.info(f"Created PR #{pr_data['number']} on {owner}/{repo}")
            return pr_data
        except Exception as e:
            logger.error(f"Failed to create PR on {owner}/{repo}: {e}")
            return None

    async def comment_on_issue(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> bool:
        """Post a comment on a GitHub issue."""
        client = await self._get_client()
        try:
            response = await client.post(
                f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
                json={"body": body},
            )
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to comment on {owner}/{repo}#{issue_number}: {e}")
            return False

    async def add_labels(
        self, owner: str, repo: str, issue_number: int, labels: list[str]
    ) -> bool:
        """Add labels to a GitHub issue."""
        client = await self._get_client()
        try:
            response = await client.post(
                f"/repos/{owner}/{repo}/issues/{issue_number}/labels",
                json={"labels": labels},
            )
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to add labels to {owner}/{repo}#{issue_number}: {e}")
            return False

    async def merge_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        merge_method: str = "squash",
    ) -> bool:
        """Merge a pull request on GitHub."""
        client = await self._get_client()
        try:
            response = await client.put(
                f"/repos/{owner}/{repo}/pulls/{pr_number}/merge",
                json={"merge_method": merge_method},
            )
            response.raise_for_status()
            logger.info(f"Merged PR #{pr_number} on {owner}/{repo}")
            return True
        except Exception as e:
            logger.error(f"Failed to merge PR #{pr_number} on {owner}/{repo}: {e}")
            return False

    async def close_pull_request(
        self, owner: str, repo: str, pr_number: int
    ) -> bool:
        """Close a pull request on GitHub."""
        client = await self._get_client()
        try:
            response = await client.patch(
                f"/repos/{owner}/{repo}/pulls/{pr_number}",
                json={"state": "closed"},
            )
            response.raise_for_status()
            logger.info(f"Closed PR #{pr_number} on {owner}/{repo}")
            return True
        except Exception as e:
            logger.error(f"Failed to close PR #{pr_number} on {owner}/{repo}: {e}")
            return False

    async def push_branch(
        self, repo_path: str, branch_name: str, remote: str = "origin"
    ) -> bool:
        """Push a local branch to the remote."""
        import asyncio

        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "push", "-u", remote, branch_name,
                cwd=repo_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.error(f"git push failed: {stderr.decode()}")
                return False
            return True
        except Exception as e:
            logger.error(f"Failed to push branch {branch_name}: {e}")
            return False
