"""SQLite schema definitions."""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    display_name TEXT,
    is_active INTEGER DEFAULT 1,
    default_branch TEXT DEFAULT 'main',
    labels_filter TEXT DEFAULT '[]',
    priority_weight INTEGER DEFAULT 5,
    auto_pickup INTEGER DEFAULT 1,
    deploy_platform TEXT,
    deploy_service_id TEXT,
    deploy_dashboard_url TEXT,
    deploy_logs_url TEXT,
    deploy_app_url TEXT,
    deploy_auto INTEGER DEFAULT 0,
    last_synced TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    github_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT DEFAULT '',
    labels TEXT DEFAULT '[]',
    github_state TEXT DEFAULT 'open',
    complexity TEXT,
    status TEXT DEFAULT 'backlog',
    assigned_model TEXT,
    priority_score INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS work_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    model_used TEXT NOT NULL,
    execution_mode TEXT NOT NULL DEFAULT 'sdk',
    status TEXT DEFAULT 'running',
    branch_name TEXT,
    pr_url TEXT,
    pr_number INTEGER,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    duration_seconds INTEGER DEFAULT 0,
    quality_score INTEGER,
    log_path TEXT,
    result_summary TEXT,
    error_message TEXT,
    started_at TEXT DEFAULT (datetime('now')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(id),
    run_id TEXT REFERENCES work_runs(id),
    question TEXT NOT NULL,
    context TEXT,
    options TEXT DEFAULT '[]',
    decision TEXT,
    status TEXT DEFAULT 'pending',
    telegram_message_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    answered_at TEXT
);

CREATE TABLE IF NOT EXISTS credit_usage (
    id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES work_runs(id),
    model TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'api',
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_project_id ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority_score DESC);
CREATE INDEX IF NOT EXISTS idx_work_runs_task_id ON work_runs(task_id);
CREATE INDEX IF NOT EXISTS idx_work_runs_status ON work_runs(status);
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);
CREATE INDEX IF NOT EXISTS idx_credit_usage_created ON credit_usage(created_at);

CREATE TABLE IF NOT EXISTS project_learnings (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    category TEXT NOT NULL DEFAULT 'error_pattern',
    content TEXT NOT NULL,
    source_run_id TEXT REFERENCES work_runs(id),
    relevance_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_learnings_project ON project_learnings(project_id);
CREATE INDEX IF NOT EXISTS idx_learnings_category ON project_learnings(category);

CREATE TABLE IF NOT EXISTS pipeline_steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES work_runs(id),
    step_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TEXT,
    finished_at TEXT,
    output_summary TEXT,
    skip_reason TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pipeline_steps_run ON pipeline_steps(run_id);

CREATE TABLE IF NOT EXISTS model_performance (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    task_type TEXT NOT NULL,
    model_used TEXT NOT NULL,
    complexity TEXT NOT NULL,
    qa_score INTEGER,
    tests_passed INTEGER DEFAULT 0,
    succeeded INTEGER DEFAULT 0,
    duration_seconds INTEGER,
    run_id TEXT REFERENCES work_runs(id),
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_model_perf_project ON model_performance(project_id);
CREATE INDEX IF NOT EXISTS idx_model_perf_model ON model_performance(model_used, task_type);
"""
