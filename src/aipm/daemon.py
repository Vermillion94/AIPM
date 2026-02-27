"""Main daemon — starts scheduler, web dashboard, and Telegram bot."""

from __future__ import annotations

import asyncio
import logging
import signal

from .config import Settings
from .core.scheduler import Scheduler
from .db.store import Store
from .integrations.telegram_bot import TelegramNotifier

logger = logging.getLogger("aipm.daemon")


class AIPMDaemon:
    """Top-level daemon that orchestrates all AIPM components."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.db_path)
        self.scheduler = Scheduler(settings, self.store)
        self.telegram = TelegramNotifier(settings, self.store)
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """Start all daemon components."""
        logger.info("Starting AIPM daemon...")

        # Initialize database
        await self.store.connect()
        logger.info("Database connected")

        # Initialize Telegram
        if self.telegram.enabled:
            success = await self.telegram.initialize()
            if success:
                await self.telegram.send_message("<b>AIPM daemon started</b>")

        # Set up signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: self._shutdown_event.set())

        # Start components
        tasks = [
            asyncio.create_task(self.scheduler.start(), name="scheduler"),
        ]

        if self.telegram.enabled:
            tasks.append(
                asyncio.create_task(self.telegram.start_polling(), name="telegram")
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

    async def stop(self) -> None:
        """Gracefully stop all components."""
        logger.info("Stopping AIPM daemon...")

        await self.scheduler.stop()

        if self.telegram.enabled:
            await self.telegram.send_message("<b>AIPM daemon stopping</b>")
            await self.telegram.stop_polling()

        await self.store.close()
        logger.info("AIPM daemon stopped")
