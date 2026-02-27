"""Prompt templates for issue triage and classification."""


def build_triage_prompt(
    title: str,
    body: str,
    labels: list[str],
) -> str:
    """Build a prompt for Haiku to classify issue complexity."""
    labels_str = ", ".join(labels) if labels else "none"
    body_truncated = body[:2000] if len(body) > 2000 else body

    return f"""Classify this GitHub issue by complexity and suggest the appropriate AI model.

**Title:** {title}
**Labels:** {labels_str}
**Description:**
{body_truncated}

## Classification Rules
- **simple**: Typo fixes, small doc changes, config tweaks, single-file changes with clear instructions. → Use Haiku.
- **medium**: Standard bug fixes, feature additions, multi-file changes with clear requirements. → Use Sonnet.
- **complex**: Architectural changes, security-sensitive code, ambiguous requirements, refactoring across many files. → Use Opus.

## Response Format
Respond with JSON only:
{{
    "complexity": "simple" | "medium" | "complex",
    "model": "haiku" | "sonnet" | "opus",
    "reasoning": "<brief explanation>",
    "estimated_files": <number of files likely affected>,
    "confidence": <0.0-1.0 confidence in classification>
}}
"""


def build_priority_prompt(tasks_summary: str, budget_remaining: float) -> str:
    """Build a prompt for Opus to prioritize the backlog."""
    return f"""You are the technical lead for an AI engineering team. Review the backlog and prioritize the next tasks.

## Current Backlog
{tasks_summary}

## Constraints
- Daily budget remaining: ${budget_remaining:.2f}
- Max concurrent workers: 2
- Prefer quick wins (simple + high value) to maintain momentum
- Critical bugs should be prioritized over features
- Issues explicitly tagged "ai-ready" are pre-vetted for AI work

## Instructions
Select up to 5 tasks to work on next, ordered by priority. For each:
1. Explain WHY it should be next
2. Suggest the approach (brief, 1-2 sentences)
3. Estimate difficulty and risk

## Response Format
Respond with JSON:
{{
    "prioritized_tasks": [
        {{
            "task_id": "<id>",
            "priority_score": <0-100>,
            "reasoning": "<why this task is important>",
            "approach": "<suggested implementation approach>",
            "estimated_cost_usd": <estimated cost>,
            "risk": "low" | "medium" | "high"
        }}
    ],
    "summary": "<overall assessment of backlog health>"
}}
"""
