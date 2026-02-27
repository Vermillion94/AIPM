"""GitHub issue synchronization — fetches issues from configured repos."""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from ..config import ProjectConfig, Settings
from ..db.models import Project, Task, TaskStatus
from ..db.store import Store

logger = logging.getLogger("aipm.github.sync")

GITHUB_API = "https://api.github.com"


class GitHubSync:
    """Syncs issues from GitHub repos into the local database."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
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

    async def sync_all(self) -> dict[str, int]:
        """Sync issues for all configured projects. Returns {project_id: issue_count}."""
        results = {}
        for project_config in self.settings.github.projects:
            try:
                count = await self.sync_project(project_config)
                results[project_config.repo] = count
                logger.info(f"Synced {count} issues from {project_config.repo}")
            except Exception as e:
                logger.error(f"Failed to sync {project_config.repo}: {e}")
                results[project_config.repo] = -1
        return results

    async def sync_project(self, project_config: ProjectConfig) -> int:
        """Sync issues for a single project. Returns number of issues synced."""
        # Ensure project exists in DB
        project = Project(
            id=project_config.repo,
            owner=project_config.owner,
            repo=project_config.repo_name,
            display_name=project_config.repo,
            labels_filter=project_config.labels,
            priority_weight=project_config.priority,
            test_command=project_config.test_command,
            build_command=project_config.build_command,
            auto_pickup=project_config.auto_pickup,
        )
        await self.store.upsert_project(project)

        # Fetch issues from GitHub
        issues = await self._fetch_issues(project_config)

        # Upsert into DB
        count = 0
        for issue_data in issues:
            task = self._issue_to_task(project_config.repo, issue_data)
            if task:
                await self.store.upsert_task(task)
                count += 1

        await self.store.update_project_synced(project_config.repo)
        return count

    async def _fetch_issues(
        self, project_config: ProjectConfig
    ) -> list[dict]:
        """Fetch open issues from GitHub API with pagination."""
        client = await self._get_client()
        owner = project_config.owner
        repo = project_config.repo_name

        all_issues = []
        page = 1
        per_page = 100

        while True:
            params: dict = {
                "state": "open",
                "per_page": per_page,
                "page": page,
                "sort": "updated",
                "direction": "desc",
            }

            # Filter by labels if configured
            if project_config.labels:
                params["labels"] = ",".join(project_config.labels)

            response = await client.get(
                f"/repos/{owner}/{repo}/issues", params=params
            )

            if response.status_code == 404:
                logger.warning(f"Repo {owner}/{repo} not found (404)")
                return []

            response.raise_for_status()
            issues = response.json()

            if not issues:
                break

            # Filter out pull requests (GitHub API returns PRs as issues too)
            real_issues = [i for i in issues if "pull_request" not in i]
            all_issues.extend(real_issues)

            if len(issues) < per_page:
                break
            page += 1

            # Safety limit
            if page > 10:
                logger.warning(f"Hit pagination limit for {owner}/{repo}")
                break

        return all_issues

    def _issue_to_task(self, project_id: str, issue_data: dict) -> Optional[Task]:
        """Convert a GitHub issue API response to a Task model."""
        number = issue_data.get("number")
        if number is None:
            return None

        labels = [label["name"] for label in issue_data.get("labels", [])]
        title = issue_data.get("title", "")
        body = issue_data.get("body") or ""

        # Calculate initial priority from labels and metadata
        priority = self._calculate_priority(labels, issue_data)

        return Task(
            id=f"{project_id}#{number}",
            project_id=project_id,
            github_number=number,
            title=title,
            body=body[:10000],  # cap body size
            labels=labels,
            github_state=issue_data.get("state", "open"),
            priority_score=priority,
        )

    @staticmethod
    def _calculate_priority(labels: list[str], issue_data: dict) -> int:
        """Score priority 0-100 based on labels and metadata."""
        score = 50  # base

        # Label-based boosts
        label_lower = {l.lower() for l in labels}
        if "critical" in label_lower or "urgent" in label_lower or "p0" in label_lower:
            score += 30
        elif "high" in label_lower or "p1" in label_lower:
            score += 20
        elif "low" in label_lower or "p3" in label_lower:
            score -= 20

        if "bug" in label_lower:
            score += 10
        if "enhancement" in label_lower or "feature" in label_lower:
            score += 5
        if "good first issue" in label_lower or "good-first-issue" in label_lower:
            score += 15  # easy wins
        if "ai-ready" in label_lower:
            score += 25  # explicitly marked for AI

        # Reactions boost (popular issues)
        reactions = issue_data.get("reactions", {})
        total_reactions = reactions.get("total_count", 0) if isinstance(reactions, dict) else 0
        score += min(total_reactions * 2, 10)

        # Comment count boost (discussed issues)
        comments = issue_data.get("comments", 0)
        score += min(comments, 5)

        return max(0, min(100, score))
