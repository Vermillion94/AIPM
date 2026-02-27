"""Prompt templates for Opus strategic planning."""


def build_daily_planning_prompt(
    backlog_summary: str,
    recent_activity: str,
    budget_info: str,
) -> str:
    """Build the daily planning prompt for Opus."""
    return f"""You are the technical lead of an AI engineering team. Review the current state and plan today's work.

## Current Backlog
{backlog_summary}

## Recent Activity (last 24h)
{recent_activity}

## Budget
{budget_info}

## Your Task
1. Assess the overall health of the projects
2. Identify the top 5 tasks to work on today
3. For each, specify the model to use and approach
4. Flag any tasks that need human input

## Response Format
Respond with JSON:
{{
    "assessment": "<overall project health assessment>",
    "today_plan": [
        {{
            "task_id": "<id>",
            "priority": <1-5, 1 being highest>,
            "model": "haiku" | "sonnet" | "opus",
            "approach": "<brief implementation approach>",
            "risk": "low" | "medium" | "high",
            "needs_human": false,
            "human_question": null
        }}
    ],
    "blocked_tasks": [
        {{
            "task_id": "<id>",
            "reason": "<why it's blocked>",
            "question_for_human": "<what to ask>"
        }}
    ],
    "recommendations": "<any general recommendations>"
}}
"""


def build_approach_prompt(
    issue_title: str,
    issue_body: str,
    repo_context: str = "",
) -> str:
    """Build a prompt for Opus to plan the implementation approach for a task."""
    return f"""Plan the implementation approach for this issue.

## Issue
**Title:** {issue_title}

**Description:**
{issue_body}

{f"## Repository Context{chr(10)}{repo_context}" if repo_context else ""}

## Your Task
Provide a clear, actionable implementation plan that a coding agent can follow.

## Response Format
Respond with JSON:
{{
    "approach": "<step-by-step implementation plan>",
    "files_likely_affected": ["<list of files>"],
    "key_considerations": ["<important things to watch out for>"],
    "test_strategy": "<how to test the changes>",
    "estimated_complexity": "simple" | "medium" | "complex"
}}
"""
