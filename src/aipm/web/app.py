"""FastAPI web dashboard — routes and app factory."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..config import Settings
from ..db.models import TaskStatus
from ..db.store import Store

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(settings: Settings, store: Store) -> FastAPI:
    """Create the FastAPI dashboard app."""
    app = FastAPI(title="AIPM Dashboard", version="0.1.0")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    # Make settings and store available to routes
    app.state.settings = settings
    app.state.store = store

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        """Main dashboard — project overview, task queue, stats."""
        stats = await store.get_stats()
        projects = await store.get_projects()
        tasks = await store.get_tasks(limit=50)
        active_runs = await store.get_runs()

        # Group tasks by status
        tasks_by_status = {}
        for task in tasks:
            status = task.status.value
            if status not in tasks_by_status:
                tasks_by_status[status] = []
            tasks_by_status[status].append(task)

        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "stats": stats,
                "projects": projects,
                "tasks": tasks,
                "tasks_by_status": tasks_by_status,
                "runs": active_runs[:10],
            },
        )

    @app.get("/projects/{project_id:path}", response_class=HTMLResponse)
    async def project_detail(request: Request, project_id: str):
        """Single project view with its tasks."""
        project = await store.get_project(project_id)
        if not project:
            return HTMLResponse("<h1>Project not found</h1>", status_code=404)

        tasks = await store.get_tasks(project_id=project_id, limit=100)
        return templates.TemplateResponse(
            "project.html",
            {
                "request": request,
                "project": project,
                "tasks": tasks,
            },
        )

    @app.get("/tasks/{task_id:path}", response_class=HTMLResponse)
    async def task_detail(request: Request, task_id: str):
        """Task detail view with run history."""
        task = await store.get_task(task_id)
        if not task:
            return HTMLResponse("<h1>Task not found</h1>", status_code=404)

        runs = await store.get_runs(task_id=task_id)
        return templates.TemplateResponse(
            "task.html",
            {
                "request": request,
                "task": task,
                "runs": runs,
            },
        )

    # --- API endpoints for HTMX ---

    @app.get("/api/stats")
    async def api_stats():
        """Get current stats as JSON."""
        return await store.get_stats()

    @app.get("/api/tasks")
    async def api_tasks(
        project_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ):
        """Get tasks as JSON."""
        task_status = None
        if status:
            try:
                task_status = TaskStatus(status)
            except ValueError:
                pass
        tasks = await store.get_tasks(
            project_id=project_id, status=task_status, limit=limit
        )
        return [
            {
                "id": t.id,
                "project_id": t.project_id,
                "github_number": t.github_number,
                "title": t.title,
                "status": t.status.value,
                "complexity": t.complexity.value if t.complexity else None,
                "assigned_model": t.assigned_model,
                "priority_score": t.priority_score,
                "labels": t.labels,
            }
            for t in tasks
        ]

    @app.get("/api/runs")
    async def api_runs(task_id: Optional[str] = None, limit: int = 20):
        """Get work runs as JSON."""
        runs = await store.get_runs(task_id=task_id, limit=limit)
        return [
            {
                "id": r.id,
                "task_id": r.task_id,
                "model_used": r.model_used,
                "status": r.status.value,
                "duration_seconds": r.duration_seconds,
                "cost_usd": r.cost_usd,
                "started_at": str(r.started_at) if r.started_at else None,
            }
            for r in runs
        ]

    @app.post("/api/tasks/{task_id:path}/dispatch")
    async def dispatch_task(task_id: str, model: str = "sonnet"):
        """Manually dispatch a task for work (API endpoint for dashboard)."""
        task = await store.get_task(task_id)
        if not task:
            return {"error": "Task not found"}
        # This would trigger the scheduler — for now just update status
        await store.update_task_status(task_id, TaskStatus.QUEUED, assigned_model=model)
        return {"status": "queued", "task_id": task_id, "model": model}

    return app
