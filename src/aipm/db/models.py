"""Pydantic models for database entities."""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    BACKLOG = "backlog"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    FAILED = "failed"
    NEEDS_HUMAN = "needs_human"


class RunStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_HUMAN = "needs_human"
    CANCELLED = "cancelled"


class Complexity(str, Enum):
    SIMPLE = "simple"
    MEDIUM = "medium"
    COMPLEX = "complex"


class DecisionStatus(str, Enum):
    PENDING = "pending"
    ANSWERED = "answered"
    EXPIRED = "expired"


class Project(BaseModel):
    id: str  # "owner/repo"
    owner: str
    repo: str
    display_name: Optional[str] = None
    is_active: bool = True
    default_branch: str = "main"
    labels_filter: list[str] = Field(default_factory=list)
    priority_weight: int = 5
    auto_pickup: bool = True
    deploy_platform: Optional[str] = None
    deploy_service_id: Optional[str] = None
    deploy_dashboard_url: Optional[str] = None
    deploy_logs_url: Optional[str] = None
    deploy_app_url: Optional[str] = None
    deploy_auto: bool = False
    last_synced: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_row(self) -> dict:
        return {
            "id": self.id,
            "owner": self.owner,
            "repo": self.repo,
            "display_name": self.display_name or f"{self.owner}/{self.repo}",
            "is_active": int(self.is_active),
            "default_branch": self.default_branch,
            "labels_filter": json.dumps(self.labels_filter),
            "priority_weight": self.priority_weight,
            "auto_pickup": int(self.auto_pickup),
            "deploy_platform": self.deploy_platform,
            "deploy_service_id": self.deploy_service_id,
            "deploy_dashboard_url": self.deploy_dashboard_url,
            "deploy_logs_url": self.deploy_logs_url,
            "deploy_app_url": self.deploy_app_url,
            "deploy_auto": int(self.deploy_auto),
        }


class Task(BaseModel):
    id: str  # "owner/repo#123"
    project_id: str
    github_number: int
    title: str
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    github_state: str = "open"
    complexity: Optional[Complexity] = None
    status: TaskStatus = TaskStatus.BACKLOG
    assigned_model: Optional[str] = None
    priority_score: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def to_row(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "github_number": self.github_number,
            "title": self.title,
            "body": self.body,
            "labels": json.dumps(self.labels),
            "github_state": self.github_state,
            "complexity": self.complexity.value if self.complexity else None,
            "status": self.status.value,
            "assigned_model": self.assigned_model,
            "priority_score": self.priority_score,
        }


class WorkRun(BaseModel):
    id: str
    task_id: str
    model_used: str
    execution_mode: str = "sdk"
    status: RunStatus = RunStatus.RUNNING
    branch_name: Optional[str] = None
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    duration_seconds: int = 0
    quality_score: Optional[int] = None
    log_path: Optional[str] = None
    result_summary: Optional[str] = None
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def to_row(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "model_used": self.model_used,
            "execution_mode": self.execution_mode,
            "status": self.status.value,
            "branch_name": self.branch_name,
            "pr_url": self.pr_url,
            "pr_number": self.pr_number,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
            "duration_seconds": self.duration_seconds,
            "quality_score": self.quality_score,
            "log_path": self.log_path,
            "result_summary": self.result_summary,
            "error_message": self.error_message,
        }


class Decision(BaseModel):
    id: str
    task_id: Optional[str] = None
    run_id: Optional[str] = None
    question: str
    context: Optional[str] = None
    options: list[str] = Field(default_factory=list)
    decision: Optional[str] = None
    status: DecisionStatus = DecisionStatus.PENDING
    telegram_message_id: Optional[str] = None
    created_at: Optional[datetime] = None
    answered_at: Optional[datetime] = None
