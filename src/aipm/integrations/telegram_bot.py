"""Telegram bot — notifications, approval workflows, status commands."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ..config import Settings
from ..db.models import TaskStatus
from ..db.store import Store
from ..github.actions import GitHubActions

logger = logging.getLogger("aipm.integrations.telegram")


class TelegramNotifier:
    """Sends notifications and handles approval workflows via Telegram."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self._bot = None
        self._app = None
        self._pause_event: Optional[asyncio.Event] = None
        self._github_actions: Optional[GitHubActions] = None

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.telegram.enabled
            and self.settings.telegram.bot_token
            and self.settings.telegram.chat_id
        )

    def set_pause_event(self, event: asyncio.Event) -> None:
        """Set the shared pause/resume event from the scheduler."""
        self._pause_event = event

    def set_github_actions(self, actions: GitHubActions) -> None:
        """Set the GitHub actions reference for PR merge/close operations."""
        self._github_actions = actions

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

    async def notify_pr_ready(
        self,
        task_id: str,
        title: str,
        pr_number: int,
        pr_url: str,
        owner: str,
        repo: str,
    ) -> Optional[int]:
        """Send a PR notification with Merge/Reject inline buttons."""
        if not self._bot or not self.settings.telegram.chat_id:
            return None

        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            buttons = [
                [
                    InlineKeyboardButton(
                        "Merge PR",
                        callback_data=f"pr_merge:{owner}/{repo}:{pr_number}:{task_id}",
                    ),
                    InlineKeyboardButton(
                        "Reject PR",
                        callback_data=f"pr_reject:{owner}/{repo}:{pr_number}:{task_id}",
                    ),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(buttons)

            text = (
                f"<b>PR Ready for Review</b>\n\n"
                f"<b>{title}</b>\n"
                f"PR: <a href=\"{pr_url}\">#{pr_number}</a>\n"
                f"Repo: {owner}/{repo}\n"
                f"Task: <code>{task_id}</code>"
            )

            msg = await self._bot.send_message(
                chat_id=self.settings.telegram.chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            return msg.message_id
        except Exception as e:
            logger.error(f"Failed to send PR ready notification: {e}")
            return None

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
        paused = self._pause_event and not self._pause_event.is_set()
        scheduler_state = "PAUSED" if paused else "RUNNING"
        text = (
            f"<b>AIPM Status</b>\n\n"
            f"Scheduler: {scheduler_state}\n"
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
        if self._pause_event:
            self._pause_event.clear()
            await update.message.reply_text("Scheduler paused. Use /resume to continue.")
            logger.info("Scheduler paused via Telegram /pause command")
        else:
            await update.message.reply_text("Pause not available — scheduler not connected.")

    async def _cmd_resume(self, update, context) -> None:
        """Handle /resume command — resume the scheduler."""
        if self._pause_event:
            self._pause_event.set()
            await update.message.reply_text("Scheduler resumed.")
            logger.info("Scheduler resumed via Telegram /resume command")
        else:
            await update.message.reply_text("Resume not available — scheduler not connected.")

    async def _handle_callback(self, update, context) -> None:
        """Handle inline keyboard button presses for decisions and PR actions."""
        query = update.callback_query
        await query.answer()

        data = query.data
        if data.startswith("pr_merge:") or data.startswith("pr_reject:"):
            await self._handle_pr_callback(query, data)
        elif data.startswith("decision:"):
            parts = data.split(":")
            if len(parts) >= 3:
                task_id = parts[1]
                choice_idx = int(parts[2])

                # Look up the pending decision for this task
                decision = await self.store.get_pending_decision_for_task(task_id)
                if not decision:
                    await query.edit_message_text(
                        f"No pending decision found for task {task_id}."
                    )
                    return

                # Resolve the option text
                if 0 <= choice_idx < len(decision.options):
                    choice = decision.options[choice_idx]
                else:
                    choice = f"option_{choice_idx}"

                # Resolve the decision in the database
                await self.store.resolve_decision(decision.id, choice)

                logger.info(
                    f"Decision resolved for task {task_id}: {choice}"
                )

                # Handle the decision outcome
                if choice in ("Approve", "Retry with different model"):
                    # Re-queue the task to BACKLOG for processing
                    await self.store.update_task_status(task_id, TaskStatus.BACKLOG)
                    await query.edit_message_text(
                        f"Approved: task {task_id} re-queued to backlog.\n"
                        f"Decision: {choice}"
                    )
                elif choice == "Skip this issue":
                    await self.store.update_task_status(task_id, TaskStatus.FAILED)
                    await query.edit_message_text(
                        f"Skipped: task {task_id} marked as failed.\n"
                        f"Decision: {choice}"
                    )
                elif choice == "Assign to human":
                    # Keep as NEEDS_HUMAN
                    await query.edit_message_text(
                        f"Assigned to human: task {task_id}\n"
                        f"Decision: {choice}"
                    )
                else:
                    await query.edit_message_text(
                        f"Decision recorded for {task_id}: {choice}"
                    )

    async def _handle_pr_callback(self, query, data: str) -> None:
        """Handle PR merge/reject button presses."""
        action = "merge" if data.startswith("pr_merge:") else "reject"
        parts = data.split(":")
        # Format: pr_merge:owner/repo:pr_number:task_id
        if len(parts) < 4:
            await query.edit_message_text("Invalid callback data.")
            return

        owner_repo = parts[1]
        pr_number = int(parts[2])
        task_id = parts[3]

        if "/" not in owner_repo:
            await query.edit_message_text("Invalid repo format.")
            return

        owner, repo = owner_repo.split("/", 1)

        if not self._github_actions:
            await query.edit_message_text("GitHub actions not available.")
            return

        if action == "merge":
            success = await self._github_actions.merge_pull_request(owner, repo, pr_number)
            if success:
                await self.store.update_task_status(task_id, TaskStatus.DONE)
                await query.edit_message_text(
                    f"PR #{pr_number} merged on {owner}/{repo}.\n"
                    f"Task {task_id} marked as DONE."
                )
            else:
                await query.edit_message_text(
                    f"Failed to merge PR #{pr_number} on {owner}/{repo}.\n"
                    f"Check GitHub for details."
                )
        else:
            success = await self._github_actions.close_pull_request(owner, repo, pr_number)
            if success:
                await self.store.update_task_status(task_id, TaskStatus.FAILED)
                await query.edit_message_text(
                    f"PR #{pr_number} closed on {owner}/{repo}.\n"
                    f"Task {task_id} marked as FAILED."
                )
            else:
                await query.edit_message_text(
                    f"Failed to close PR #{pr_number} on {owner}/{repo}.\n"
                    f"Check GitHub for details."
                )

        logger.info(f"PR {action} for #{pr_number} on {owner}/{repo} (task {task_id})")
