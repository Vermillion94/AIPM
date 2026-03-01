"""Quality assurance — run tests, evaluate output quality."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import socket
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from urllib.parse import urljoin, urlparse

import httpx

if TYPE_CHECKING:
    from ..db.models import Project

from ..config import Settings
from ..db.models import WorkRun
from ..db.store import Store
from ..prompts.review import build_code_review_prompt, classify_change_type
from .site_reviewer import crawl_site, html_to_text

logger = logging.getLogger("aipm.core.qa")


@dataclass
class QAResult:
    passed: bool
    test_output: str = ""
    test_exit_code: int = -1
    review_score: Optional[int] = None
    review_summary: str = ""
    issues: list[str] = field(default_factory=list)
    review_raw: Optional[dict] = None


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
        project: Optional[Project] = None,
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
        review = await self._run_code_review(
            worktree_path, run, test_output=result.test_output, project=project,
        )
        if review:
            result.review_raw = review
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
        self,
        worktree_path: Path,
        run: WorkRun,
        test_output: str = "",
        project: Optional[Project] = None,
    ) -> Optional[dict]:
        """Run AI code review on the changes. Returns review dict or None."""
        try:
            # Get the diff
            diff_result = await self._run_command("git diff HEAD~1", worktree_path)
            if diff_result["returncode"] != 0 or not diff_result["output"].strip():
                logger.info(f"No diff available for code review (run {run.id})")
                return None

            diff = diff_result["output"]

            # Load task context
            task = await self.store.get_task(run.task_id)
            title = task.title if task else ""
            body = task.body if task else ""
            labels = getattr(task, "labels", None) or []

            # Classify change type
            change_type = classify_change_type(title, body, labels, diff)

            # Get visual context — try local server first, fall back to live site
            visual = await self._get_visual_context(project, worktree_path)

            # Build project description
            project_description = ""
            deploy_url = ""
            if project:
                project_description = f"{project.owner}/{project.repo}"
                deploy_url = getattr(project, "deploy_app_url", "") or ""

            # Fetch project learnings for review context
            learnings_text = ""
            if task:
                learnings = await self.store.get_learnings(task.project_id, limit=15)
                if learnings:
                    learnings_text = "\n".join(
                        f"- [{lr.category.value}] {lr.content}" for lr in learnings
                    )

            # Build the review prompt
            prompt = build_code_review_prompt(
                issue_title=title,
                issue_body=body,
                diff=diff,
                test_output=test_output,
                project_description=project_description,
                deploy_url=deploy_url,
                change_type=change_type,
                labels=labels,
                live_site_content=visual.get("live_content", ""),
                learnings=learnings_text,
            )

            # Call review via Claude CLI (uses subscription, not API tokens)
            model_map = {
                "opus": "opus",
                "sonnet": "sonnet",
                "haiku": "haiku",
                "claude-opus-4-6": "opus",
                "claude-sonnet-4-6": "sonnet",
                "claude-haiku-4-5-20251001": "haiku",
            }
            review_model = self.settings.models.review
            cli_model = model_map.get(review_model, "sonnet")

            env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

            cmd = [
                "/usr/bin/claude",
                "--print",
                "--model", cli_model,
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(worktree_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode()), timeout=120,
            )

            stdout_text = stdout.decode(errors="replace")
            if proc.returncode != 0:
                logger.error(
                    f"Review CLI failed (exit {proc.returncode}): "
                    f"{stderr.decode(errors='replace')[:500]}"
                )
                return None

            if not stdout_text.strip():
                logger.error(f"Review CLI returned empty stdout for run {run.id}")
                return None

            # Extract the review JSON from text output
            # Try markdown-fenced JSON first, then bare JSON object
            fence_match = re.search(
                r"```(?:json)?\s*(\{.*?\})\s*```", stdout_text, re.DOTALL
            )
            if fence_match:
                review = json.loads(fence_match.group(1))
            else:
                json_match = re.search(r"\{.*\}", stdout_text, re.DOTALL)
                if not json_match:
                    logger.error(
                        f"No JSON found in review output for run {run.id}. "
                        f"Output: {stdout_text[:300]}"
                    )
                    return None
                review = json.loads(json_match.group())

            # Record credit usage (CLI = subscription, cost tracked as $0)
            await self.store.record_credit(
                run_id=run.id,
                model=review_model,
                source="cli",
                tokens_in=len(prompt) // 4,
                tokens_out=len(stdout_text) // 4,
                cost_usd=0.0,
            )

            logger.info(
                f"Code review for run {run.id}: "
                f"score={review.get('score')}, approved={review.get('approved')}, "
                f"change_type={change_type}"
            )
            return review

        except Exception as e:
            logger.error(f"Code review failed for run {run.id}: {e}")
            return None

    async def _get_visual_context(
        self, project: Optional[Project], worktree_path: Optional[Path] = None,
    ) -> dict:
        """Get visual context. Try local dev server first, fall back to live site."""
        if not project:
            return {}

        # Try local server for authenticated page access
        if worktree_path and (worktree_path / "requirements.txt").exists():
            try:
                pages = await self._crawl_local_server(worktree_path)
                if pages:
                    content_parts = []
                    for page in pages:
                        content_parts.append(f"[{page['url']}]\n{page['content'][:2000]}")
                    logger.info(
                        f"Local server crawl succeeded for {project.id}: {len(pages)} pages"
                    )
                    return {"live_content": "\n\n---\n\n".join(content_parts)}
            except Exception as e:
                logger.info(f"Local server crawl failed, falling back to live site: {e}")

        # Fall back to live site crawl
        deploy_url = getattr(project, "deploy_app_url", None)
        if not deploy_url:
            return {}
        try:
            pages = await crawl_site(deploy_url, max_pages=2)
            if not pages:
                return {}
            content_parts = []
            for page in pages:
                content_parts.append(f"[{page['url']}]\n{page['content'][:2000]}")
            return {"live_content": "\n\n---\n\n".join(content_parts)}
        except Exception as e:
            logger.debug(f"Visual context crawl failed for {project.id}: {e}")
            return {}

    # ── Local dev server for authenticated QA crawling ──────────────────

    _SAFE_SKIP_SEGMENTS = (
        "/api/", "/delete", "/remove", "/create", "/update",
        "/edit", "/submit", "/action", "/logout", "/login",
        "/signup", "/register", "/invite", "/admin/", "/upload",
    )

    _SEED_PATHS = (
        "/", "/seasons", "/teams", "/players", "/stats", "/profile",
        "/about", "/watch", "/champion_stats", "/resources/links",
        "/resources/howto",
    )

    async def _crawl_local_server(self, worktree_path: Path) -> list[dict]:
        """Orchestrate: venv → start server → seed DB → forge cookie → crawl → cleanup."""
        import secrets as secrets_mod

        secret = secrets_mod.token_hex(32)
        port = self._find_free_port()
        venv_path = self._get_qa_venv_path(worktree_path)
        db_path = worktree_path / "DevData" / "lol_rec_league.db"
        proc = None

        try:
            await self._ensure_qa_venv(worktree_path, venv_path)
            proc = await self._start_local_server(worktree_path, venv_path, port, secret)
            await self._wait_for_server(port, timeout=45)
            self._seed_test_user(db_path)
            cookie = self._forge_session_cookie(secret)
            pages = await self._crawl_with_auth(port, cookie, max_pages=8)
            return pages
        finally:
            # Always kill server and clean up throwaway DB
            if proc is not None:
                try:
                    proc.send_signal(signal.SIGTERM)
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                except ProcessLookupError:
                    pass
                logger.debug("Local QA server stopped")
            if db_path.exists():
                try:
                    db_path.unlink()
                    logger.debug(f"Deleted throwaway DB: {db_path}")
                except OSError:
                    pass

    @staticmethod
    def _find_free_port() -> int:
        """Bind to port 0 and return the OS-assigned port."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _get_qa_venv_path(self, worktree_path: Path) -> Path:
        """Return a cached venv path keyed by requirements.txt hash."""
        req_file = worktree_path / "requirements.txt"
        req_hash = hashlib.sha256(req_file.read_bytes()).hexdigest()[:16]
        # data/ is at /opt/aipm/data/
        data_dir = Path(self.settings.data_dir) if hasattr(self.settings, "data_dir") else (
            Path("/opt/aipm/data")
        )
        return data_dir / "qa_venvs" / req_hash

    async def _ensure_qa_venv(self, worktree_path: Path, venv_path: Path) -> None:
        """Create a Python venv and install requirements.txt if not already cached."""
        if (venv_path / "bin" / "python").exists():
            logger.debug(f"QA venv cache hit: {venv_path}")
            return

        venv_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"Creating QA venv at {venv_path}")

        # Create venv
        result = await self._run_command(
            f"python3 -m venv {venv_path}", worktree_path, timeout=60,
        )
        if result["returncode"] != 0:
            raise RuntimeError(f"venv creation failed: {result['output']}")

        # Install deps
        pip = venv_path / "bin" / "pip"
        req = worktree_path / "requirements.txt"
        result = await self._run_command(
            f"{pip} install -r {req}", worktree_path, timeout=180,
        )
        if result["returncode"] != 0:
            raise RuntimeError(f"pip install failed: {result['output']}")

        logger.info("QA venv ready")

    async def _start_local_server(
        self, worktree_path: Path, venv_path: Path, port: int, secret: str,
    ) -> asyncio.subprocess.Process:
        """Start uvicorn on localhost with safe env vars. Returns the process."""
        python = str(venv_path / "bin" / "python")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "SESSION_SECRET": secret,
            "AUTH_CLIENT_ID": "qa_dummy",
            "AUTH_CLIENT_SECRET": "qa_dummy",
            # RENDER and FLY_APP_NAME explicitly omitted → local SQLite
        }

        proc = await asyncio.create_subprocess_exec(
            python, "-m", "uvicorn", "main:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--no-access-log",
            cwd=str(worktree_path),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info(f"Started local QA server on 127.0.0.1:{port} (pid={proc.pid})")
        return proc

    async def _wait_for_server(self, port: int, timeout: int = 45) -> None:
        """Poll http://127.0.0.1:{port}/ until it responds or timeout."""
        url = f"http://127.0.0.1:{port}/"
        deadline = asyncio.get_event_loop().time() + timeout
        async with httpx.AsyncClient(timeout=3.0) as client:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    resp = await client.get(url)
                    if resp.status_code > 0:
                        logger.debug(f"Server ready on port {port}")
                        return
                except (httpx.ConnectError, httpx.ReadError):
                    pass
                await asyncio.sleep(1)
        raise TimeoutError(f"Server did not start within {timeout}s on port {port}")

    @staticmethod
    def _seed_test_user(db_path: Path) -> None:
        """Insert a test user into the local SQLite DB."""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO user (id, username, email, role) "
                "VALUES (99999, 'qa_test_user', 'qa@test.local', 'PLAYER')"
            )
            conn.commit()
            logger.debug("Seeded test user (id=99999) into local DB")
        finally:
            conn.close()

    @staticmethod
    def _forge_session_cookie(secret: str) -> str:
        """Forge a signed bl_session cookie using itsdangerous (same as Starlette)."""
        import base64

        import itsdangerous

        session_data = {"user_id": "99999", "auth": "99999"}
        data = base64.b64encode(json.dumps(session_data).encode())
        signer = itsdangerous.TimestampSigner(secret)
        return signer.sign(data).decode()

    async def _crawl_with_auth(
        self, port: int, cookie: str, max_pages: int = 8,
    ) -> list[dict]:
        """Crawl localhost with the forged session cookie."""
        base_url = f"http://127.0.0.1:{port}"
        pages: list[dict] = []
        visited: set[str] = set()

        # Start with seed URLs
        to_visit = [f"{base_url}{path}" for path in self._SEED_PATHS]

        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            cookies={"bl_session": cookie},
            headers={"User-Agent": "AIPM-QA/1.0"},
        ) as client:
            while to_visit and len(pages) < max_pages:
                url = to_visit.pop(0)
                normalized = url.rstrip("/")
                if normalized in visited:
                    continue
                visited.add(normalized)

                # Skip unsafe paths
                parsed = urlparse(url)
                path_lower = parsed.path.lower()
                if any(seg in path_lower for seg in self._SAFE_SKIP_SEGMENTS):
                    continue

                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue
                    content_type = resp.headers.get("content-type", "")
                    if "text/html" not in content_type:
                        continue

                    html = resp.text
                    text = html_to_text(html)
                    pages.append({"url": url, "content": text})

                    # Discover internal links
                    links = re.findall(r'href=["\']([^"\']+)["\']', html)
                    for link in links:
                        abs_url = urljoin(url, link)
                        link_parsed = urlparse(abs_url)
                        # Only follow links on same host
                        if link_parsed.netloc != parsed.netloc:
                            continue
                        if abs_url.rstrip("/") in visited:
                            continue
                        link_path = link_parsed.path.lower()
                        if any(seg in link_path for seg in self._SAFE_SKIP_SEGMENTS):
                            continue
                        to_visit.append(abs_url)
                except Exception as e:
                    logger.debug(f"Failed to crawl {url}: {e}")

        logger.info(f"Local auth crawl: {len(pages)} pages crawled")
        return pages

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
