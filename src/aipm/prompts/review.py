"""Prompt templates for code review by Opus."""

from __future__ import annotations

import re


def classify_change_type(
    title: str, body: str, labels: list[str] | None, diff: str
) -> str:
    """Classify the type of change from issue metadata and diff content."""
    labels = [l.lower() for l in (labels or [])]
    title_lower = title.lower()
    body_lower = body.lower()

    # Check labels first
    if "revert" in labels:
        return "revert"
    if "bug" in labels:
        return "bug_fix"
    if "enhancement" in labels or "feature" in labels:
        return "enhancement"
    if "refactor" in labels:
        return "refactor"

    # Check title/body keywords
    if "revert" in title_lower or "revert" in body_lower:
        return "revert"
    if any(kw in title_lower for kw in ("fix", "bug", "broken", "crash", "error")):
        return "bug_fix"

    # Check diff ratio: more deletions than additions → revert/simplification
    additions = len(re.findall(r"^\+[^+]", diff, re.MULTILINE))
    deletions = len(re.findall(r"^-[^-]", diff, re.MULTILINE))
    if deletions > additions * 2 and deletions > 5:
        return "revert_or_simplification"

    return "unknown"


_CHANGE_TYPE_GUIDANCE = {
    "revert": (
        "This is a REVERT — the developer is undoing a previous change that caused problems. "
        "Reverting broken code is a valid and often correct fix. Do NOT penalize for removing "
        "code, even if the removed code looks well-structured. The code being removed was the "
        "problem. Judge whether the revert targets the right commit/change."
    ),
    "bug_fix": (
        "This is a BUG FIX. Focus on whether the fix addresses the root cause, not just "
        "symptoms. A minimal, targeted fix is often better than a large refactor."
    ),
    "revert_or_simplification": (
        "This change removes more code than it adds, which may indicate a revert or "
        "simplification. More deletions than additions can be the correct approach — "
        "removing broken or unnecessary code is valid. Judge the result, not the diff size."
    ),
    "enhancement": (
        "This is a new feature or enhancement. Check that it integrates well with "
        "existing code and doesn't introduce regressions."
    ),
    "refactor": (
        "This is a refactor. The behavior should be preserved. Check that no functionality "
        "is accidentally changed or lost."
    ),
}


def build_code_review_prompt(
    issue_title: str,
    issue_body: str,
    diff: str,
    test_output: str = "",
    project_description: str = "",
    deploy_url: str = "",
    change_type: str = "",
    labels: list[str] | None = None,
    live_site_content: str = "",
    learnings: str = "",
    wiki: str = "",
) -> str:
    """Build a code review prompt for Opus."""
    diff_truncated = diff[:12000] if len(diff) > 12000 else diff

    test_section = ""
    if test_output:
        test_section = f"""
## Test Output
```
{test_output[:2000]}
```
"""

    project_section = ""
    if project_description or deploy_url:
        project_section = "\n## Project Context\n"
        if project_description:
            project_section += f"**Project:** {project_description}\n"
        if deploy_url:
            project_section += f"**Live URL:** {deploy_url}\n"

    change_type_section = ""
    if change_type:
        guidance = _CHANGE_TYPE_GUIDANCE.get(change_type, "")
        change_type_section = f"\n## Change Type: {change_type}\n"
        if guidance:
            change_type_section += f"{guidance}\n"
        if labels:
            change_type_section += f"**Labels:** {', '.join(labels)}\n"

    live_site_section = ""
    if live_site_content:
        live_site_section = f"""
## Live Site Content (Current Production)
The following is the current content visible on the live/deployed site. Use this to
understand what the user actually sees — the diff modifies the code that produces this output.
If the live site shows broken content, errors, or missing elements, a fix that addresses
those problems should be scored favorably even if it removes code.

```
{live_site_content[:4000]}
```
"""

    wiki_section = ""
    if wiki:
        wiki_section = f"\n## Project Wiki\n{wiki}\n"

    learnings_section = ""
    if learnings:
        learnings_section = f"\n## Known Project Patterns\n{learnings}\n"

    return f"""Review this code change for the following issue.
{project_section}{change_type_section}{wiki_section}{learnings_section}
## Issue
**Title:** {issue_title}
**Description:** {issue_body[:4000]}

## Changes
```diff
{diff_truncated}
```
{test_section}{live_site_section}
## Review Criteria
1. **Correctness**: Does it fix the issue as described?
2. **Appropriateness**: Is this the right approach for the problem? (A revert, a minimal fix, or a refactor can all be correct depending on context.)
3. **Quality**: Clean code, proper patterns, good naming?
4. **Safety**: No security issues, edge cases handled?
5. **Tests**: Are changes adequately tested?
6. **Visual impact**: If live site content is provided, does this change improve the user experience?

## Response Format
Respond with JSON:
{{
    "approved": true | false,
    "score": <0-100>,
    "summary": "<1-2 sentence review summary>",
    "issues": [
        {{
            "severity": "critical" | "major" | "minor" | "nit",
            "description": "<what's wrong>",
            "suggestion": "<how to fix>"
        }}
    ],
    "praise": ["<things done well>"]
}}
"""
