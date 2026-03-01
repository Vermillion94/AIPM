"""Configuration loader — reads TOML config with env var resolution."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


# --- Pydantic config models ---


class DeployConfig(BaseModel):
    platform: Optional[str] = None       # "render", "fly", "vercel", "self-hosted", etc.
    service_id: Optional[str] = None     # e.g. "srv-d20p2f15pdvs7399d0o0"
    dashboard_url: Optional[str] = None  # e.g. "https://dashboard.render.com/web/srv-xxx"
    logs_url: Optional[str] = None       # e.g. "https://dashboard.render.com/web/srv-xxx/logs"
    app_url: Optional[str] = None        # e.g. "https://blwebsite.onrender.com"
    auto_deploys: bool = False           # does merging to default_branch auto-deploy?


class ProjectConfig(BaseModel):
    repo: str  # "owner/repo"
    labels: list[str] = Field(default_factory=list)
    priority: int = 5
    default_branch: str = "main"
    auto_pickup: bool = True
    deploy: DeployConfig = Field(default_factory=DeployConfig)

    @property
    def owner(self) -> str:
        return self.repo.split("/")[0]

    @property
    def repo_name(self) -> str:
        return self.repo.split("/")[1]


class GithubConfig(BaseModel):
    token: Optional[str] = None
    projects: list[ProjectConfig] = Field(default_factory=list)


class BudgetConfig(BaseModel):
    daily_api_budget_usd: float = 10.0
    prefer_subscription: bool = True
    per_task_budget_usd: float = 3.0


class ModelsConfig(BaseModel):
    triage: str = "claude-haiku-4-5-20251001"
    routing: str = "claude-opus-4-6"
    coding_simple: str = "claude-sonnet-4-6"
    coding_complex: str = "claude-opus-4-6"
    review: str = "claude-opus-4-6"


class TelegramConfig(BaseModel):
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None
    enabled: bool = False


class DashboardConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class AipmConfig(BaseModel):
    data_dir: str = "./data"
    max_concurrent_workers: int = 2
    sync_interval_seconds: int = 300
    work_interval_seconds: int = 60
    max_retries_per_issue: int = 2
    review_score_threshold: int = 60


class Settings(BaseModel):
    aipm: AipmConfig = Field(default_factory=AipmConfig)
    github: GithubConfig = Field(default_factory=GithubConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    anthropic: dict = Field(default_factory=dict)

    @property
    def data_path(self) -> Path:
        return Path(self.aipm.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_path / "aipm.db"

    @property
    def logs_path(self) -> Path:
        return self.data_path / "logs"

    @property
    def worktrees_path(self) -> Path:
        return self.data_path / "worktrees"


# --- Env var resolution ---

_ENV_PATTERN = re.compile(r"\$\{(\w+)\}")


def _resolve_env_vars(value: str) -> str:
    """Replace ${ENV_VAR} references in strings with actual env values."""

    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        env_val = os.environ.get(var_name)
        if env_val is None:
            return match.group(0)  # leave unresolved
        return env_val

    return _ENV_PATTERN.sub(replacer, value)


def _resolve_dict(d: dict) -> dict:
    """Recursively resolve env vars in all string values."""
    result = {}
    for key, value in d.items():
        if isinstance(value, str):
            result[key] = _resolve_env_vars(value)
        elif isinstance(value, dict):
            result[key] = _resolve_dict(value)
        elif isinstance(value, list):
            result[key] = [
                _resolve_dict(item) if isinstance(item, dict) else
                _resolve_env_vars(item) if isinstance(item, str) else item
                for item in value
            ]
        else:
            result[key] = value
    return result


def load_settings(config_path: Optional[str | Path] = None) -> Settings:
    """Load settings from TOML file + env vars.

    Resolution order:
    1. Read TOML file (config.toml by default)
    2. Resolve ${ENV_VAR} references in string values
    3. Fall back to env vars for secrets (GITHUB_TOKEN, ANTHROPIC_API_KEY, etc.)
    4. Validate with Pydantic
    """
    raw = {}

    if config_path is None:
        config_path = Path("config.toml")

    config_path = Path(config_path)
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

    # Resolve env var references in TOML values
    raw = _resolve_dict(raw)

    # Build settings from TOML
    settings = Settings(**raw)

    # Fall back to env vars for secrets not in TOML
    if not settings.github.token:
        settings.github.token = os.environ.get("GITHUB_TOKEN")

    anthropic_key = raw.get("anthropic", {}).get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        settings.anthropic["api_key"] = anthropic_key

    if not settings.telegram.bot_token:
        settings.telegram.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")

    if not settings.telegram.chat_id:
        settings.telegram.chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    # Auto-enable telegram if token + chat_id are present
    if settings.telegram.bot_token and settings.telegram.chat_id:
        settings.telegram.enabled = True

    # Ensure data directories exist
    settings.data_path.mkdir(parents=True, exist_ok=True)
    settings.logs_path.mkdir(parents=True, exist_ok=True)
    settings.worktrees_path.mkdir(parents=True, exist_ok=True)

    return settings
