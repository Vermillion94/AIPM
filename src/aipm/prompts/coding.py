"""Prompt templates for worker coding tasks."""


def _learnings_section(learnings: str) -> str:
    if not learnings:
        return ""
    return f"## Project Notes\n{learnings}\n\n"


def build_coding_prompt(
    issue_number: int,
    project_repo: str,
    title: str,
    body: str,
    labels: list[str],
    approach: str = "",
    learnings: str = "",
) -> str:
    """Build the main coding prompt for a worker agent."""
    labels_str = ", ".join(labels) if labels else "none"

    approach_section = ""
    if approach:
        approach_section = f"""
## Approach
The technical lead has suggested the following approach:
{approach}

Follow this guidance unless you identify a clearly better alternative.
"""

    return f"""You are working on issue #{issue_number} in the repository {project_repo}.

## Issue
**Title:** {title}
**Labels:** {labels_str}

**Description:**
{body}

{approach_section}{_learnings_section(learnings)}## Instructions

1. **Understand the issue**: Read the relevant source code to fully understand the context and requirements.
2. **Implement the fix/feature**: Make the minimum necessary changes to resolve the issue correctly.
3. **Write or update tests**: Ensure your changes are covered by tests where applicable.
4. **Run existing tests**: Verify that all existing tests still pass.
5. **Commit your changes**: Create a single, well-described commit.

## Rules
- Do NOT push to remote. Only commit locally.
- Do NOT create a pull request.
- Keep changes minimal and focused on the issue.
- Follow existing code style and conventions.
- If you encounter something unclear, add a TODO comment rather than guessing.
- Do not modify unrelated code.
"""


def build_retry_prompt(
    original_prompt: str,
    error_output: str,
    attempt: int,
) -> str:
    """Build a retry prompt when the previous attempt failed."""
    return f"""{original_prompt}

## Previous Attempt Failed (attempt {attempt})

The previous attempt to resolve this issue failed with the following error:

```
{error_output[:3000]}
```

Please fix the issues from the previous attempt. Focus on making the tests pass.
"""


def build_review_request_prompt(
    issue_number: int,
    project_repo: str,
    title: str,
    diff: str,
) -> str:
    """Build a prompt for Opus to review a completed task."""
    diff_truncated = diff[:8000] if len(diff) > 8000 else diff

    return f"""Review this code change for issue #{issue_number} in {project_repo}.

**Issue:** {title}

## Diff
```diff
{diff_truncated}
```

## Review Criteria
Rate the change from 0-100 on these dimensions:
1. **Correctness**: Does it actually fix the issue?
2. **Quality**: Is the code clean, well-structured, and following conventions?
3. **Safety**: Are there any security concerns, edge cases, or potential bugs?
4. **Completeness**: Are there tests? Is anything missing?

## Response Format
Respond with JSON:
{{
    "score": <0-100 overall score>,
    "correctness": <0-100>,
    "quality": <0-100>,
    "safety": <0-100>,
    "completeness": <0-100>,
    "summary": "<1-2 sentence summary>",
    "issues": ["<list of specific issues found, if any>"],
    "approved": <true/false - would you approve this PR?>
}}
"""
