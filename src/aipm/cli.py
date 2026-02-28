"""CLI interface — click commands for AIPM."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click

from .config import load_settings


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@click.group()
@click.option("-c", "--config", "config_path", default="config.toml", help="Config file path")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
@click.pass_context
def cli(ctx: click.Context, config_path: str, verbose: bool) -> None:
    """AIPM — AI Project Manager. Autonomous engineering workforce for GitHub projects."""
    setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["verbose"] = verbose


@cli.command()
@click.pass_context
def init(ctx: click.Context) -> None:
    """Initialize AIPM — create database and data directories."""

    async def _init():
        settings = load_settings(ctx.obj["config_path"])
        from .db.store import Store

        store = Store(settings.db_path)
        await store.connect()
        await store.close()
        click.echo(f"Database initialized at {settings.db_path}")
        click.echo(f"Data directory: {settings.data_path}")
        click.echo("Run 'aipm sync' to fetch issues from GitHub.")

    asyncio.run(_init())


@cli.command()
@click.pass_context
def sync(ctx: click.Context) -> None:
    """Sync issues from all configured GitHub projects."""

    async def _sync():
        settings = load_settings(ctx.obj["config_path"])
        from .db.store import Store
        from .github.sync import GitHubSync

        store = Store(settings.db_path)
        await store.connect()

        syncer = GitHubSync(settings, store)
        results = await syncer.sync_all()
        await syncer.close()

        for repo, count in results.items():
            if count >= 0:
                click.echo(f"  {repo}: {count} issues synced")
            else:
                click.echo(f"  {repo}: FAILED")

        # Show summary
        stats = await store.get_stats()
        click.echo(f"\nTotal: {sum(v for v in stats['task_counts'].values())} tasks across {stats['project_count']} projects")
        await store.close()

    asyncio.run(_sync())


@cli.command(name="list")
@click.option("--project", "-p", help="Filter by project (owner/repo)")
@click.option("--status", "-s", help="Filter by status")
@click.pass_context
def list_tasks(ctx: click.Context, project: str | None, status: str | None) -> None:
    """List all tracked issues/tasks."""

    async def _list():
        settings = load_settings(ctx.obj["config_path"])
        from .db.models import TaskStatus
        from .db.store import Store

        store = Store(settings.db_path)
        await store.connect()

        task_status = None
        if status:
            try:
                task_status = TaskStatus(status)
            except ValueError:
                click.echo(f"Invalid status: {status}")
                click.echo(f"Valid: {', '.join(s.value for s in TaskStatus)}")
                return

        tasks = await store.get_tasks(project_id=project, status=task_status)
        await store.close()

        if not tasks:
            click.echo("No tasks found.")
            return

        # Table output
        click.echo(f"{'ID':<30} {'Status':<14} {'Model':<8} {'Pri':>3} {'Title'}")
        click.echo("-" * 100)
        for t in tasks:
            model = t.assigned_model or "-"
            click.echo(
                f"{t.id:<30} {t.status.value:<14} {model:<8} {t.priority_score:>3} {t.title[:45]}"
            )

    asyncio.run(_list())


@cli.command()
@click.argument("task_id")
@click.option("--model", "-m", default="sonnet", help="Model to use (haiku/sonnet/opus)")
@click.pass_context
def work(ctx: click.Context, task_id: str, model: str) -> None:
    """Manually dispatch a worker to an issue.

    TASK_ID is the issue identifier, e.g., 'owner/repo#123'
    """

    async def _work():
        settings = load_settings(ctx.obj["config_path"])
        from .core.qa import QARunner
        from .core.worker import Worker
        from .core.worktree import WorktreeManager
        from .db.store import Store

        store = Store(settings.db_path)
        await store.connect()

        task = await store.get_task(task_id)
        if not task:
            click.echo(f"Task not found: {task_id}")
            click.echo("Run 'aipm sync' first, then 'aipm list' to see available tasks.")
            await store.close()
            return

        project = await store.get_project(task.project_id)
        if not project:
            click.echo(f"Project not found: {task.project_id}")
            await store.close()
            return

        click.echo(f"Working on: {task.title}")
        click.echo(f"  Model: {model}")
        click.echo(f"  Project: {task.project_id}")

        # Create worktree
        worktree_mgr = WorktreeManager(settings.worktrees_path)
        repo_url = f"https://github.com/{project.owner}/{project.repo}"
        branch_name = f"aipm/issue-{task.github_number}"

        click.echo(f"  Branch: {branch_name}")
        click.echo("  Creating worktree...")

        worktree_path = await worktree_mgr.create(
            repo_url=repo_url,
            branch_name=branch_name,
            base_branch=project.default_branch,
            task_id=task.id,
        )

        if not worktree_path:
            click.echo("Failed to create worktree.")
            await store.close()
            return

        click.echo(f"  Worktree: {worktree_path}")
        click.echo("  Executing...")

        worker = Worker(settings, store)
        run = await worker.execute(
            task=task,
            worktree_path=worktree_path,
            model=model,
            execution_mode="cli",
        )

        click.echo(f"\n  Run status: {run.status.value}")

        if run.status.value == "succeeded":
            click.echo("  Running QA checks...")
            qa = QARunner(settings, store)
            qa_result = await qa.run_checks(worktree_path, run, project.test_command)
            click.echo(f"  QA passed: {qa_result.passed}")
            if qa_result.issues:
                for issue in qa_result.issues:
                    click.echo(f"    - {issue}")
        elif run.error_message:
            click.echo(f"  Error: {run.error_message[:200]}")

        await store.close()

    asyncio.run(_work())


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show current AIPM status — projects, tasks, runs, budget."""

    async def _status():
        settings = load_settings(ctx.obj["config_path"])
        from .db.store import Store

        store = Store(settings.db_path)
        await store.connect()

        stats = await store.get_stats()
        await store.close()

        click.echo("=== AIPM Status ===\n")
        click.echo(f"Projects: {stats['project_count']}")
        click.echo(f"Total runs: {stats['total_runs']}")
        click.echo(f"Active runs: {stats['active_runs']}")
        click.echo(f"Daily spend: ${stats['daily_spend_usd']:.2f}")
        click.echo(f"\nTasks by status:")
        for status_name, count in sorted(stats["task_counts"].items()):
            click.echo(f"  {status_name}: {count}")

    asyncio.run(_status())


@cli.command()
@click.option("--host", default=None, help="Dashboard host")
@click.option("--port", default=None, type=int, help="Dashboard port")
@click.pass_context
def run(ctx: click.Context, host: str | None, port: int | None) -> None:
    """Start the AIPM daemon — scheduler + Telegram bot + web dashboard."""

    async def _run():
        settings = load_settings(ctx.obj["config_path"])
        from .daemon import AIPMDaemon

        daemon = AIPMDaemon(
            settings,
            dashboard_host=host,
            dashboard_port=port,
        )

        dashboard_host = host or settings.dashboard.host
        dashboard_port = port or settings.dashboard.port

        click.echo("Starting AIPM daemon...")
        click.echo(f"Dashboard: http://{dashboard_host}:{dashboard_port}")
        click.echo(f"Configured projects: {len(settings.github.projects)}")
        click.echo(f"Telegram: {'enabled' if daemon.telegram.enabled else 'disabled'}")
        click.echo("Press Ctrl+C to stop.\n")

        await daemon.start()

    asyncio.run(_run())
