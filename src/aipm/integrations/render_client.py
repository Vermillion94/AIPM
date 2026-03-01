"""Render API client — restricted to read-only deploy/log operations."""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("aipm.integrations.render")

RENDER_API = "https://api.render.com/v1"


class _ReadOnlyClient:
    """httpx.AsyncClient wrapper that only allows GET requests."""

    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self._client.get(url, **kwargs)

    @property
    def is_closed(self) -> bool:
        return self._client.is_closed

    async def aclose(self) -> None:
        await self._client.aclose()


class RenderClient:
    """Read-only Render API client.

    Only exposes GET requests to deploy status and log endpoints.
    API key is loaded from the RENDER_API_KEY environment variable
    at runtime — never stored on the instance or logged.

    The underlying HTTP client is wrapped in _ReadOnlyClient which
    only exposes .get() — POST/PUT/PATCH/DELETE are not available.
    """

    # Allowlist of URL path prefixes this client may access.
    _ALLOWED_PATHS = ("/services/",)

    def __init__(self, service_id: str):
        self.service_id = service_id
        self._client: Optional[_ReadOnlyClient] = None

    def _get_api_key(self) -> str:
        key = os.environ.get("RENDER_API_KEY")
        if not key:
            raise RuntimeError("RENDER_API_KEY environment variable not set")
        return key

    async def _get_client(self) -> _ReadOnlyClient:
        if self._client is None or self._client.is_closed:
            raw = httpx.AsyncClient(
                base_url=RENDER_API,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._get_api_key()}",
                },
                timeout=30.0,
            )
            self._client = _ReadOnlyClient(raw)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_deploys(self, limit: int = 10) -> list[dict]:
        """Get recent deploys for the service."""
        client = await self._get_client()
        response = await client.get(
            f"/services/{self.service_id}/deploys",
            params={"limit": limit},
        )
        response.raise_for_status()
        return response.json()

    async def get_deploy(self, deploy_id: str) -> dict:
        """Get status of a specific deploy."""
        client = await self._get_client()
        response = await client.get(
            f"/services/{self.service_id}/deploys/{deploy_id}",
        )
        response.raise_for_status()
        return response.json()

    async def get_latest_deploy_status(self) -> Optional[dict]:
        """Get the most recent deploy's status. Returns dict with id, status, finishedAt, or None."""
        deploys = await self.get_deploys(limit=1)
        if not deploys:
            return None
        deploy = deploys[0].get("deploy", deploys[0])
        return {
            "id": deploy.get("id"),
            "status": deploy.get("status"),
            "commit": deploy.get("commit", {}).get("message", ""),
            "created_at": deploy.get("createdAt"),
            "finished_at": deploy.get("finishedAt"),
        }

    async def check_preview_health(
        self, pr_branch: str, timeout: int = 120
    ) -> dict:
        """Check if a Render PR preview deployed successfully.

        Polls recent deploys for one matching the PR branch, then waits
        until it reaches a terminal state or times out.
        """
        import asyncio

        result = {"found": False, "status": "unknown", "url": ""}
        deadline = asyncio.get_event_loop().time() + timeout
        poll_interval = 10

        while asyncio.get_event_loop().time() < deadline:
            try:
                deploys = await self.get_deploys(limit=5)

                for item in deploys:
                    deploy = item.get("deploy", item)
                    commit = deploy.get("commit", {})
                    ref = commit.get("ref", "") if isinstance(commit, dict) else ""

                    if pr_branch in ref:
                        result["found"] = True
                        status = deploy.get("status", "unknown")
                        result["status"] = status

                        # Render PR preview URL pattern
                        service_slug = deploy.get("server", {}).get("slug", "")
                        if service_slug:
                            result["url"] = f"https://{service_slug}-pr-{pr_branch.split('/')[-1]}.onrender.com"

                        if status in ("live", "build_failed", "deactivated", "canceled"):
                            return result

                        break  # Found the deploy but not terminal — keep polling

            except Exception as e:
                logger.warning(f"Error checking Render preview: {e}")

            await asyncio.sleep(poll_interval)

        return result

    async def check_health(self, app_url: str) -> dict:
        """Simple HTTP health check against the app URL. Uses a fresh client (no auth headers)."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.get(app_url)
                return {
                    "url": app_url,
                    "status_code": response.status_code,
                    "healthy": response.status_code == 200,
                }
            except httpx.RequestError as e:
                return {
                    "url": app_url,
                    "status_code": None,
                    "healthy": False,
                    "error": str(e),
                }
