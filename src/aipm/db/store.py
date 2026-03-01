"""Async SQLite data store — all database operations."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

from .models import (
    Complexity,
    Decision,
    DecisionStatus,
    LearningCategory,
    Project,
    ProjectLearning,
    RunStatus,
    Task,
    TaskStatus,
    WorkRun,
)
from .schema import SCHEMA_SQL


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


class Store:
    """Async SQLite store for all AIPM data."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store not connected. Call connect() first.")
        return self._db

    # --- Projects ---

    async def upsert_project(self, project: Project) -> None:
        row = project.to_row()
        await self.db.execute(
            """INSERT INTO projects (id, owner, repo, display_name, is_active,
               default_branch, labels_filter, priority_weight,
               auto_pickup, deploy_platform, deploy_service_id,
               deploy_dashboard_url, deploy_logs_url, deploy_app_url,
               deploy_auto, created_at, updated_at)
               VALUES (:id, :owner, :repo, :display_name, :is_active,
               :default_branch, :labels_filter, :priority_weight,
               :auto_pickup, :deploy_platform, :deploy_service_id,
               :deploy_dashboard_url, :deploy_logs_url, :deploy_app_url,
               :deploy_auto, :created_at, :updated_at)
               ON CONFLICT(id) DO UPDATE SET
               display_name=:display_name, is_active=:is_active,
               labels_filter=:labels_filter, priority_weight=:priority_weight,
               auto_pickup=:auto_pickup, deploy_platform=:deploy_platform,
               deploy_service_id=:deploy_service_id,
               deploy_dashboard_url=:deploy_dashboard_url,
               deploy_logs_url=:deploy_logs_url, deploy_app_url=:deploy_app_url,
               deploy_auto=:deploy_auto, updated_at=:updated_at""",
            {**row, "created_at": _now(), "updated_at": _now()},
        )
        await self.db.commit()

    async def get_projects(self, active_only: bool = True) -> list[Project]:
        query = "SELECT * FROM projects"
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY priority_weight DESC"
        async with self.db.execute(query) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_project(r) for r in rows]

    async def get_project(self, project_id: str) -> Optional[Project]:
        async with self.db.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return self._row_to_project(row) if row else None

    async def update_project_synced(self, project_id: str) -> None:
        await self.db.execute(
            "UPDATE projects SET last_synced = ?, updated_at = ? WHERE id = ?",
            (_now(), _now(), project_id),
        )
        await self.db.commit()

    # --- Tasks ---

    async def upsert_task(self, task: Task) -> None:
        row = task.to_row()
        now = _now()
        await self.db.execute(
            """INSERT INTO tasks (id, project_id, github_number, title, body,
               labels, github_state, complexity, status, assigned_model,
               priority_score, created_at, updated_at)
               VALUES (:id, :project_id, :github_number, :title, :body,
               :labels, :github_state, :complexity, :status, :assigned_model,
               :priority_score, :created_at, :updated_at)
               ON CONFLICT(id) DO UPDATE SET
               title=:title, body=:body, labels=:labels, github_state=:github_state,
               updated_at=:updated_at""",
            {**row, "created_at": now, "updated_at": now},
        )
        await self.db.commit()

    async def get_tasks(
        self,
        project_id: Optional[str] = None,
        status: Optional[TaskStatus] = None,
        limit: int = 100,
    ) -> list[Task]:
        query = "SELECT * FROM tasks WHERE 1=1"
        params: list = []
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if status:
            query += " AND status = ?"
            params.append(status.value)
        query += " ORDER BY priority_score DESC, created_at ASC LIMIT ?"
        params.append(limit)
        async with self.db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_task(r) for r in rows]

    async def get_task(self, task_id: str) -> Optional[Task]:
        async with self.db.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return self._row_to_task(row) if row else None

    async def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        assigned_model: Optional[str] = None,
        complexity: Optional[Complexity] = None,
    ) -> None:
        updates = ["status = ?", "updated_at = ?"]
        params: list = [status.value, _now()]
        if assigned_model:
            updates.append("assigned_model = ?")
            params.append(assigned_model)
        if complexity:
            updates.append("complexity = ?")
            params.append(complexity.value)
        if status == TaskStatus.IN_PROGRESS:
            updates.append("started_at = ?")
            params.append(_now())
        if status == TaskStatus.DONE:
            updates.append("completed_at = ?")
            params.append(_now())
        params.append(task_id)
        await self.db.execute(
            f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params
        )
        await self.db.commit()

    async def update_task_priority(self, task_id: str, score: int) -> None:
        await self.db.execute(
            "UPDATE tasks SET priority_score = ?, updated_at = ? WHERE id = ?",
            (score, _now(), task_id),
        )
        await self.db.commit()

    async def get_active_project_ids(self) -> list[str]:
        """Get project_ids that have a currently running work run."""
        async with self.db.execute(
            """SELECT DISTINCT t.project_id
               FROM work_runs wr
               JOIN tasks t ON wr.task_id = t.id
               WHERE wr.status = 'running'"""
        ) as cursor:
            rows = await cursor.fetchall()
        return [r["project_id"] for r in rows]

    async def get_next_task(
        self, exclude_project_ids: Optional[list[str]] = None
    ) -> Optional[Task]:
        """Get the highest-priority backlog task ready for work.

        Args:
            exclude_project_ids: Skip tasks from these projects (used for
                per-repo serialization).
        """
        query = "SELECT * FROM tasks WHERE status = 'backlog' AND github_state = 'open'"
        params: list = []
        if exclude_project_ids:
            placeholders = ",".join("?" for _ in exclude_project_ids)
            query += f" AND project_id NOT IN ({placeholders})"
            params.extend(exclude_project_ids)
        query += " ORDER BY priority_score DESC, created_at ASC LIMIT 1"
        async with self.db.execute(query, params) as cursor:
            row = await cursor.fetchone()
        return self._row_to_task(row) if row else None

    async def count_tasks_by_status(self) -> dict[str, int]:
        async with self.db.execute(
            "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
        ) as cursor:
            rows = await cursor.fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    # --- Work Runs ---

    async def create_run(self, run: WorkRun) -> str:
        if not run.id:
            run.id = _new_id()
        row = run.to_row()
        await self.db.execute(
            """INSERT INTO work_runs (id, task_id, model_used, execution_mode,
               status, branch_name, started_at)
               VALUES (:id, :task_id, :model_used, :execution_mode,
               :status, :branch_name, :started_at)""",
            {**row, "started_at": _now()},
        )
        await self.db.commit()
        return run.id

    async def update_run(
        self,
        run_id: str,
        status: Optional[RunStatus] = None,
        pr_url: Optional[str] = None,
        pr_number: Optional[int] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        cost_usd: Optional[float] = None,
        duration_seconds: Optional[int] = None,
        quality_score: Optional[int] = None,
        result_summary: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        updates = []
        params: list = []
        for field, value in [
            ("status", status.value if status else None),
            ("pr_url", pr_url),
            ("pr_number", pr_number),
            ("tokens_in", tokens_in),
            ("tokens_out", tokens_out),
            ("cost_usd", cost_usd),
            ("duration_seconds", duration_seconds),
            ("quality_score", quality_score),
            ("result_summary", result_summary),
            ("error_message", error_message),
        ]:
            if value is not None:
                updates.append(f"{field} = ?")
                params.append(value)
        if status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED):
            updates.append("finished_at = ?")
            params.append(_now())
        if not updates:
            return
        params.append(run_id)
        await self.db.execute(
            f"UPDATE work_runs SET {', '.join(updates)} WHERE id = ?", params
        )
        await self.db.commit()

    async def get_runs(
        self, task_id: Optional[str] = None, limit: int = 50
    ) -> list[WorkRun]:
        query = "SELECT * FROM work_runs"
        params: list = []
        if task_id:
            query += " WHERE task_id = ?"
            params.append(task_id)
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        async with self.db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_run(r) for r in rows]

    async def get_run(self, run_id: str) -> Optional[WorkRun]:
        async with self.db.execute(
            "SELECT * FROM work_runs WHERE id = ?", (run_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return self._row_to_run(row) if row else None

    async def count_active_runs(self) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) as cnt FROM work_runs WHERE status = 'running'"
        ) as cursor:
            row = await cursor.fetchone()
        return row["cnt"] if row else 0

    # --- Decisions ---

    async def create_decision(self, decision: Decision) -> str:
        if not decision.id:
            decision.id = _new_id()
        await self.db.execute(
            """INSERT INTO decisions (id, task_id, run_id, question, context,
               options, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                decision.id,
                decision.task_id,
                decision.run_id,
                decision.question,
                decision.context,
                json.dumps(decision.options),
                decision.status.value,
                _now(),
            ),
        )
        await self.db.commit()
        return decision.id

    async def resolve_decision(self, decision_id: str, choice: str) -> None:
        await self.db.execute(
            """UPDATE decisions SET decision = ?, status = 'answered',
               answered_at = ? WHERE id = ?""",
            (choice, _now(), decision_id),
        )
        await self.db.commit()

    async def get_pending_decisions(self) -> list[Decision]:
        async with self.db.execute(
            "SELECT * FROM decisions WHERE status = 'pending' ORDER BY created_at ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_decision(r) for r in rows]

    async def get_pending_decision_for_task(self, task_id: str) -> Optional[Decision]:
        """Find the pending decision associated with a task."""
        async with self.db.execute(
            "SELECT * FROM decisions WHERE task_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return self._row_to_decision(row) if row else None

    # --- Work Run Queries for Retry Logic ---

    async def count_failed_runs(self, task_id: str) -> int:
        """Count the number of failed work runs for a task."""
        async with self.db.execute(
            "SELECT COUNT(*) as cnt FROM work_runs WHERE task_id = ? AND status = 'failed'",
            (task_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return row["cnt"] if row else 0

    async def get_latest_failed_run(self, task_id: str) -> Optional[WorkRun]:
        """Get the most recent failed run for a task."""
        async with self.db.execute(
            "SELECT * FROM work_runs WHERE task_id = ? AND status = 'failed' ORDER BY started_at DESC LIMIT 1",
            (task_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return self._row_to_run(row) if row else None

    async def reset_failed_runs(self, task_id: str) -> int:
        """Delete failed work runs for a task, resetting the retry counter.

        Used when a human explicitly approves a retry after exhausting retries.
        Returns the number of runs deleted.
        """
        async with self.db.execute(
            "SELECT COUNT(*) as cnt FROM work_runs WHERE task_id = ? AND status = 'failed'",
            (task_id,),
        ) as cursor:
            row = await cursor.fetchone()
        count = row["cnt"] if row else 0
        await self.db.execute(
            "DELETE FROM work_runs WHERE task_id = ? AND status = 'failed'",
            (task_id,),
        )
        await self.db.commit()
        return count

    # --- Credit Usage ---

    async def record_credit(
        self,
        run_id: Optional[str],
        model: str,
        source: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        await self.db.execute(
            """INSERT INTO credit_usage (id, run_id, model, source,
               tokens_in, tokens_out, cost_usd, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (_new_id(), run_id, model, source, tokens_in, tokens_out, cost_usd, _now()),
        )
        await self.db.commit()

    async def get_daily_spend(self) -> float:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        async with self.db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) as total FROM credit_usage WHERE created_at >= ?",
            (today,),
        ) as cursor:
            row = await cursor.fetchone()
        return row["total"] if row else 0.0

    # --- Stats ---

    async def get_stats(self) -> dict:
        task_counts = await self.count_tasks_by_status()
        active_runs = await self.count_active_runs()
        daily_spend = await self.get_daily_spend()

        async with self.db.execute("SELECT COUNT(*) as cnt FROM projects") as cursor:
            row = await cursor.fetchone()
        project_count = row["cnt"] if row else 0

        async with self.db.execute("SELECT COUNT(*) as cnt FROM work_runs") as cursor:
            row = await cursor.fetchone()
        total_runs = row["cnt"] if row else 0

        return {
            "project_count": project_count,
            "task_counts": task_counts,
            "active_runs": active_runs,
            "total_runs": total_runs,
            "daily_spend_usd": daily_spend,
        }

    # --- Project Learnings ---

    async def record_learning(
        self,
        project_id: str,
        category: LearningCategory,
        content: str,
        source_run_id: Optional[str] = None,
    ) -> Optional[str]:
        """Insert a learning with dedup check. Returns ID or None if duplicate."""
        async with self.db.execute(
            "SELECT id FROM project_learnings WHERE project_id = ? AND category = ? AND content = ?",
            (project_id, category.value, content),
        ) as cursor:
            if await cursor.fetchone():
                return None
        learning_id = _new_id()
        await self.db.execute(
            """INSERT INTO project_learnings (id, project_id, category, content, source_run_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (learning_id, project_id, category.value, content, source_run_id, _now()),
        )
        await self.db.commit()
        return learning_id

    async def get_learnings(
        self,
        project_id: str,
        categories: Optional[list[LearningCategory]] = None,
        limit: int = 20,
    ) -> list[ProjectLearning]:
        """Fetch learnings ordered by relevance_count DESC, created_at DESC."""
        query = "SELECT * FROM project_learnings WHERE project_id = ?"
        params: list = [project_id]
        if categories:
            placeholders = ",".join("?" for _ in categories)
            query += f" AND category IN ({placeholders})"
            params.extend(c.value for c in categories)
        query += " ORDER BY relevance_count DESC, created_at DESC LIMIT ?"
        params.append(limit)
        async with self.db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_learning(r) for r in rows]

    async def increment_learning_relevance(self, learning_ids: list[str]) -> None:
        """Bulk increment relevance_count for the given learning IDs."""
        if not learning_ids:
            return
        placeholders = ",".join("?" for _ in learning_ids)
        await self.db.execute(
            f"UPDATE project_learnings SET relevance_count = relevance_count + 1 WHERE id IN ({placeholders})",
            learning_ids,
        )
        await self.db.commit()

    # --- Row converters ---

    @staticmethod
    def _row_to_project(row: aiosqlite.Row) -> Project:
        d = dict(row)
        d["is_active"] = bool(d.get("is_active", 1))
        d["auto_pickup"] = bool(d.get("auto_pickup", 1))
        d["deploy_auto"] = bool(d.get("deploy_auto", 0))
        d["labels_filter"] = json.loads(d.get("labels_filter", "[]"))
        return Project(**d)

    @staticmethod
    def _row_to_task(row: aiosqlite.Row) -> Task:
        d = dict(row)
        d["labels"] = json.loads(d.get("labels", "[]"))
        return Task(**d)

    @staticmethod
    def _row_to_run(row: aiosqlite.Row) -> WorkRun:
        return WorkRun(**dict(row))

    @staticmethod
    def _row_to_decision(row: aiosqlite.Row) -> Decision:
        d = dict(row)
        d["options"] = json.loads(d.get("options", "[]"))
        return Decision(**d)

    @staticmethod
    def _row_to_learning(row: aiosqlite.Row) -> ProjectLearning:
        return ProjectLearning(**dict(row))
