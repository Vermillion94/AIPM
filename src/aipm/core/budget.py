"""Budget tracking — daily caps, per-task limits, source preference."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import Settings
from ..db.store import Store

logger = logging.getLogger("aipm.core.budget")

# Rough cost estimates per 1M tokens (input/output)
MODEL_COSTS = {
    "claude-opus-4-6": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.0},
    # Aliases
    "opus": {"input": 15.0, "output": 75.0},
    "sonnet": {"input": 3.0, "output": 15.0},
    "haiku": {"input": 0.80, "output": 4.0},
}


@dataclass
class BudgetStatus:
    daily_limit: float
    daily_spent: float
    daily_remaining: float
    per_task_limit: float
    prefer_subscription: bool
    can_work: bool
    reason: str = ""


class BudgetTracker:
    """Tracks spending and enforces budget limits."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    async def check_budget(self) -> BudgetStatus:
        """Check if we have budget to do work."""
        daily_spent = await self.store.get_daily_spend()
        daily_limit = self.settings.budget.daily_api_budget_usd
        daily_remaining = daily_limit - daily_spent

        can_work = daily_remaining > 0.10  # minimum $0.10 to start a task
        reason = "" if can_work else f"Daily budget exhausted (${daily_spent:.2f}/${daily_limit:.2f})"

        return BudgetStatus(
            daily_limit=daily_limit,
            daily_spent=daily_spent,
            daily_remaining=daily_remaining,
            per_task_limit=self.settings.budget.per_task_budget_usd,
            prefer_subscription=self.settings.budget.prefer_subscription,
            can_work=can_work,
            reason=reason,
        )

    @staticmethod
    def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
        """Estimate cost for a given model and token count."""
        costs = MODEL_COSTS.get(model, MODEL_COSTS.get("sonnet", {}))
        input_cost = (tokens_in / 1_000_000) * costs.get("input", 3.0)
        output_cost = (tokens_out / 1_000_000) * costs.get("output", 15.0)
        return input_cost + output_cost

    def select_execution_mode(self, model: str) -> str:
        """Decide whether to use SDK (API) or CLI (subscription).

        Returns 'sdk' or 'cli'.
        """
        if self.settings.budget.prefer_subscription:
            # Use CLI for coding tasks (uses subscription credits)
            # Use SDK for triage/review (needs structured output)
            return "cli"
        return "sdk"
