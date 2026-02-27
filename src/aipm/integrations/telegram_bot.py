"""Telegram bot — notifications, approval workflows, status commands."""

from __future__ import annotations

import logging
from typing import Optional

from ..config import Settings
from ..db.store import Store

logger = logging.getLogger("aipm.integrations.telegram")


class TelegramNotifier:
    """Sends notifications and handles approval workflows via Telegram."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self._bot = None
        self._app = None

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.telegram.enabled
            and self.settings.telegram.bot_token
            and self.settings.telegram.chat_id
        )

    async def initialize(self) -> bool:
        """Initialize the Telegram bot. Returns True if successful."""
        if not self.enabled:
            logger.info("Telegram notifications disabled")
            return False

        try:
            from telegram import Bot

            self._bot = Bot(token=self.settings.telegram.bot_token)
            # Test the connection
            me = await self._bot.get_me()
            logger.info(f"Telegram bot initialized: @{me.username}")
            return True
        except ImportError:
            logger.warning("python-telegram-bot not installed")
            return False
        except Exception as e:
            logger.error(f"Failed to initialize Telegram bot: {e}")
            return False

    async def send_message(self, text: str) -> Optional[int]:
        """Send a plain text message. Returns message_id or None."""
        if not self._bot or not self.settings.telegram.chat_id:
            return None
        try:
            msg = await self._bot.send_message(
                chat_id=self.settings.telegram.chat_id,
                text=text,
                parse_mode="HTML",
            )
            return msg.message_id
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return None

    async def notify_task_completed(
        self,
        task_id: str,
        title: str,
        model: str,
        duration: int,
        passed_qa: bool,
    ) -> Optional[int]:
        """Notify that a task was completed."""
        status_emoji = "PASS" if passed_qa else "FAIL"
        text = (
            f"<b>Task Completed</b>\n\n"
            f"<b>{title}</b>\n"
            f"ID: <code>{task_id}</code>\n"
            f"Model: {model}\n"
            f"Duration: {duration}s\n"
            f"QA: {status_emoji}\n"
        )
        if passed_qa:
            text += "\nReady for review."
        else:
            text += "\nQA checks failed — needs attention."

        return await self.send_message(text)

    async def notify_task_failed(
        self, task_id: str, title: str, error: str
    ) -> Optional[int]:
        """Notify that a task failed."""
        text = (
            f"<b>Task Failed</b>\n\n"
            f"<b>{title}</b>\n"
            f"ID: <code>{task_id}</code>\n\n"
            f"Error: {error[:500]}"
        )
        return await self.send_message(text)

    async def notify_decision_needed(
        self, task_id: str, title: str, question: str, options: list[str]
    ) -> Optional[int]:
        """Send a decision request with inline keyboard options."""
        if not self._bot or not self.settings.telegram.chat_id:
            return None

        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            buttons = []
            for i, option in enumerate(options):
                buttons.append(
                    [InlineKeyboardButton(option, callback_data=f"decision:{task_id}:{i}")]
                )

            reply_markup = InlineKeyboardMarkup(buttons)

            text = (
                f"<b>Decision Needed</b>\n\n"
                f"<b>{title}</b>\n"
                f"ID: <code>{task_id}</code>\n\n"
                f"{question}"
            )

            msg = await self._bot.send_message(
                chat_id=self.settings.telegram.chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            return msg.message_id
        except Exception as e:
            logger.error(f"Failed to send decision request: {e}")
            return None

    async def notify_daily_summary(
        self,
        total_tasks: int,
        completed_today: int,
        failed_today: int,
        daily_spend: float,
        budget_remaining: float,
    ) -> Optional[int]:
        """Send a daily summary."""
        text = (
            f"<b>AIPM Daily Summary</b>\n\n"
            f"Total tasks tracked: {total_tasks}\n"
            f"Completed today: {completed_today}\n"
            f"Failed today: {failed_today}\n"
            f"Daily spend: ${daily_spend:.2f}\n"
            f"Budget remaining: ${budget_remaining:.2f}"
        )
        return await self.send_message(text)

    async def notify_budget_warning(self, spent: float, limit: float) -> Optional[int]:
        """Warn when approaching budget limit."""
        pct = (spent / limit * 100) if limit > 0 else 100
        text = (
            f"<b>Budget Warning</b>\n\n"
            f"Daily spend: ${spent:.2f} / ${limit:.2f}\n"
            f"Usage: {pct:.0f}%\n\n"
            f"AIPM will pause work when the budget is exhausted."
        )
        return await self.send_message(text)

    async def start_polling(self) -> None:
        """Start listening for Telegram callback queries (for approvals).

        This should run as an asyncio task alongside the scheduler.
        """
        if not self.enabled:
            return

        try:
            from telegram.ext import (
                ApplicationBuilder,
                CallbackQueryHandler,
                CommandHandler,
            )

            app = (
                ApplicationBuilder()
                .token(self.settings.telegram.bot_token)
                .build()
            )

            # Command handlers
            app.add_handler(CommandHandler("status", self._cmd_status))
            app.add_handler(CommandHandler("pause", self._cmd_pause))
            app.add_handler(CommandHandler("resume", self._cmd_resume))

            # Callback handler for inline keyboard buttons
            app.add_handler(CallbackQueryHandler(self._handle_callback))

            self._app = app
            logger.info("Starting Telegram polling...")
            await app.initialize()
            await app.start()
            await app.updater.start_polling()

        except Exception as e:
            logger.error(f"Failed to start Telegram polling: {e}")

    async def stop_polling(self) -> None:
        """Stop the Telegram polling."""
        if self._app:
            try:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception as e:
                logger.error(f"Error stopping Telegram: {e}")

    async def _cmd_status(self, update, context) -> None:
        """Handle /status command."""
        stats = await self.store.get_stats()
        text = (
            f"<b>AIPM Status</b>\n\n"
            f"Projects: {stats['project_count']}\n"
            f"Active runs: {stats['active_runs']}\n"
            f"Total runs: {stats['total_runs']}\n"
            f"Daily spend: ${stats['daily_spend_usd']:.2f}\n\n"
            f"<b>Tasks:</b>\n"
        )
        for status_name, count in sorted(stats["task_counts"].items()):
            text += f"  {status_name}: {count}\n"

        await update.message.reply_html(text)

    async def _cmd_pause(self, update, context) -> None:
        """Handle /pause command — pause the scheduler."""
        await update.message.reply_text("Scheduler paused. Use /resume to continue.")

    async def _cmd_resume(self, update, context) -> None:
        """Handle /resume command — resume the scheduler."""
        await update.message.reply_text("Scheduler resumed.")

    async def _handle_callback(self, update, context) -> None:
        """Handle inline keyboard button presses for decisions."""
        query = update.callback_query
        await query.answer()

        data = query.data
        if data.startswith("decision:"):
            parts = data.split(":")
            if len(parts) >= 3:
                task_id = parts[1]
                choice_idx = int(parts[2])
                await query.edit_message_text(
                    f"Decision recorded for {task_id}: option {choice_idx}"
                )
                logger.info(f"Decision received: {task_id} -> {choice_idx}")
