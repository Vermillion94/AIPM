"""Model router — classifies tasks and assigns the right model."""

from __future__ import annotations

import logging
from typing import Optional

from ..config import Settings
from ..db.models import Complexity, Task
from ..db.store import Store
from ..integrations.anthropic_client import AnthropicClient
from ..prompts.triage import build_triage_prompt

logger = logging.getLogger("aipm.core.router")

# Model mapping from complexity
COMPLEXITY_MODEL_MAP = {
    Complexity.SIMPLE: "haiku",
    Complexity.MEDIUM: "sonnet",
    Complexity.COMPLEX: "opus",
}

# Full model IDs
MODEL_IDS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
}


class TaskRouter:
    """Classifies tasks by complexity and routes them to the right model."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self._anthropic: Optional[AnthropicClient] = None

    @property
    def anthropic(self) -> AnthropicClient:
        if self._anthropic is None:
            self._anthropic = AnthropicClient(self.settings)
        return self._anthropic

    async def classify_and_route(self, task: Task) -> tuple[Complexity, str]:
        """Classify a task and return (complexity, model_name).

        Uses Haiku for fast, cheap classification.
        """
        try:
            prompt = build_triage_prompt(
                title=task.title,
                body=task.body,
                labels=task.labels,
            )
            result = self.anthropic.classify_issue(prompt)

            complexity_str = result.get("complexity", "medium")
            model_str = result.get("model", "sonnet")

            # Parse complexity
            try:
                complexity = Complexity(complexity_str)
            except ValueError:
                complexity = Complexity.MEDIUM

            # Use the model from classification, or map from complexity
            model = model_str if model_str in MODEL_IDS else COMPLEXITY_MODEL_MAP.get(
                complexity, "sonnet"
            )

            confidence = result.get("confidence", 0.5)
            logger.info(
                f"Classified {task.id}: complexity={complexity.value}, "
                f"model={model}, confidence={confidence}"
            )

            # Update task in DB
            await self.store.update_task_status(
                task.id,
                task.status,
                assigned_model=model,
                complexity=complexity,
            )

            return complexity, model

        except Exception as e:
            logger.error(f"Classification failed for {task.id}, defaulting to medium/sonnet: {e}")
            return Complexity.MEDIUM, "sonnet"

    def get_model_id(self, model_name: str) -> str:
        """Get the full model ID from a short name."""
        return MODEL_IDS.get(model_name, model_name)

    async def classify_batch(self, tasks: list[Task]) -> None:
        """Classify multiple tasks (used during sync)."""
        for task in tasks:
            if task.complexity is None:
                await self.classify_and_route(task)
