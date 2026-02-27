"""Prompt templates for code review by Opus."""


def build_code_review_prompt(
    issue_title: str,
    issue_body: str,
    diff: str,
    test_output: str = "",
) -> str:
    """Build a code review prompt for Opus."""
    diff_truncated = diff[:8000] if len(diff) > 8000 else diff
    test_section = ""
    if test_output:
        test_section = f"""
## Test Output
```
{test_output[:2000]}
```
"""

    return f"""Review this code change for the following issue.

## Issue
**Title:** {issue_title}
**Description:** {issue_body[:1000]}

## Changes
```diff
{diff_truncated}
```
{test_section}
## Review Criteria
1. **Correctness**: Does it fix the issue as described?
2. **Quality**: Clean code, proper patterns, good naming?
3. **Safety**: No security issues, edge cases handled?
4. **Tests**: Are changes adequately tested?

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
