"""Anthropic API wrapper — model routing, structured output."""

from __future__ import annotations

import json
import logging
from typing import Optional

import anthropic

from ..config import Settings

logger = logging.getLogger("aipm.integrations.anthropic")


class AnthropicClient:
    """Wrapper around the Anthropic SDK for triage, planning, and review calls."""

    def __init__(self, settings: Settings):
        self.settings = settings
        api_key = settings.anthropic.get("api_key")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set in config or environment")
        self._client = anthropic.Anthropic(api_key=api_key)

    def classify_issue(
        self, prompt: str, model: Optional[str] = None
    ) -> dict:
        """Call Haiku to classify an issue. Returns parsed JSON."""
        model = model or self.settings.models.triage
        return self._call_json(prompt, model=model, max_tokens=512)

    def plan_priorities(
        self, prompt: str, model: Optional[str] = None
    ) -> dict:
        """Call Opus to prioritize the backlog. Returns parsed JSON."""
        model = model or self.settings.models.routing
        return self._call_json(prompt, model=model, max_tokens=2048)

    def review_code(
        self, prompt: str, model: Optional[str] = None
    ) -> dict:
        """Call Opus to review a code change. Returns parsed JSON."""
        model = model or self.settings.models.review
        return self._call_json(prompt, model=model, max_tokens=1024)

    def generate(
        self, prompt: str, model: str, max_tokens: int = 4096, system: str = ""
    ) -> str:
        """General-purpose text generation."""
        messages = [{"role": "user", "content": prompt}]
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        response = self._client.messages.create(**kwargs)
        return response.content[0].text

    def _call_json(
        self, prompt: str, model: str, max_tokens: int = 1024
    ) -> dict:
        """Call a model and parse the response as JSON."""
        response_text = self.generate(
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            system="You are a technical assistant. Always respond with valid JSON only, no markdown formatting.",
        )

        # Try to parse JSON from the response
        text = response_text.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first and last lines (fences)
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Try to find JSON in the response
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end])
                except json.JSONDecodeError:
                    pass
            logger.error(f"Failed to parse JSON from model response: {text[:500]}")
            return {"error": "Failed to parse response", "raw": text[:500]}

    def get_usage_from_response(self, response) -> dict:
        """Extract token usage from an API response."""
        if hasattr(response, "usage"):
            return {
                "tokens_in": response.usage.input_tokens,
                "tokens_out": response.usage.output_tokens,
            }
        return {"tokens_in": 0, "tokens_out": 0}
