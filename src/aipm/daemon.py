"""Main daemon — starts scheduler, web dashboard, and Telegram bot."""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Optional

from .config import Settings
from .core.scheduler import Scheduler
from .db.store import Store
from .integrations.telegram_bot import TelegramNotifier

logger = logging.getLogger("aipm.daemon")


class AIPMDaemon:
    """Top-level daemon that orchestrates all AIPM components."""

    def __init__(
        self,
        settings: Settings,
        dashboard_host: Optional[str] = None,
        dashboard_port: Optional[int] = None,
    ):
        self.settings = settings
        self.store = Store(settings.db_path)
        self.scheduler = Scheduler(settings, self.store)
        self.telegram = TelegramNotifier(settings, self.store)
        self._shutdown_event = asyncio.Event()

        # Dashboard config (overrides from settings if provided)
        self.dashboard_host = dashboard_host or settings.dashboard.host
        self.dashboard_port = dashboard_port or settings.dashboard.port

    async def start(self) -> None:
        """Start all daemon components."""
        logger.info("Starting AIPM daemon...")

        # Initialize database
        await self.store.connect()
        logger.info("Database connected")

        # Wire Telegram ↔ Scheduler
        self.scheduler.set_telegram(self.telegram)

        # Initialize Telegram and wire pause event
        if self.telegram.enabled:
            success = await self.telegram.initialize()
            if success:
                # Wire the pause event from scheduler to telegram
                self.telegram.set_pause_event(self.scheduler.pause_event)
                await self.telegram.send_message("<b>AIPM daemon started</b>")

        # Set up signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: self._shutdown_event.set())

        # Start components as tasks
        tasks = [
            asyncio.create_task(self.scheduler.start(), name="scheduler"),
        ]

        if self.telegram.enabled:
            tasks.append(
                asyncio.create_task(self.telegram.start_polling(), name="telegram")
            )

        # Start web dashboard
        tasks.append(
            asyncio.create_task(self._run_dashboard(), name="dashboard")
        )

        # Wait for shutdown signal
        await self._shutdown_event.wait()

        logger.info("Shutdown signal received")
        await self.stop()

        # Cancel remaining tasks
        for task in tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run_dashboard(self) -> None:
        """Run the web dashboard as an async task."""
        try:
            import uvicorn

            from .web.app import create_app

            app = create_app(self.settings, self.store)
            config = uvicorn.Config(
                app,
                host=self.dashboard_host,
                port=self.dashboard_port,
                log_level="info",
            )
            server = uvicorn.Server(config)
            logger.info(
                f"Dashboard starting at http://{self.dashboard_host}:{self.dashboard_port}"
            )
            await server.serve()
        except ImportError:
            logger.warning("uvicorn not installed — dashboard disabled")
        except Exception as e:
            logger.error(f"Dashboard failed to start: {e}")

    async def stop(self) -> None:
        """Gracefully stop all components."""
        logger.info("Stopping AIPM daemon...")

        await self.scheduler.stop()

        if self.telegram.enabled:
            await self.telegram.send_message("<b>AIPM daemon stopping</b>")
            await self.telegram.stop_polling()

        await self.store.close()
        logger.info("AIPM daemon stopped")
