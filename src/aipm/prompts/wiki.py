"""Wiki formatting for prompt injection."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..db.models import WikiSection


def format_wiki_for_prompt(sections: list[WikiSection], max_chars: int = 4000) -> str:
    """Format wiki sections as markdown for prompt injection.

    Returns empty string if no sections. Truncates at char budget.
    """
    if not sections:
        return ""

    parts: list[str] = []
    total = 0

    for section in sections:
        block = f"### {section.title}\n{section.content}\n"
        if total + len(block) > max_chars:
            remaining = max_chars - total
            if remaining > 50:
                parts.append(block[:remaining] + "...")
            break
        parts.append(block)
        total += len(block)

    return "\n".join(parts)
