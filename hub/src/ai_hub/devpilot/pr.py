"""Pull request creation and management for DevPilot."""

from __future__ import annotations

from typing import Any


def build_pr_body(
    issue_number: int,
    summary: str,
    plan: str | None = None,
    test_evidence: str | None = None,
    limitations: list[str] | None = None,
) -> str:
    """Build the PR body markdown."""
    lines: list[str] = [
        "## Summary",
        summary,
        "",
    ]

    if plan:
        lines.extend(["## Plan", plan, ""])

    lines.extend(
        [
            "## Test Evidence",
            test_evidence or "_Automated verification unavailable_",
            "",
        ]
    )

    if limitations:
        lines.append("## Limitations")
        for lim in limitations:
            lines.append(f"- {lim}")
        lines.append("")

    lines.append(f"Closes #{issue_number}")
    lines.append("")

    return "\n".join(lines)


def build_pr_payload(
    title: str,
    body: str,
    head_branch: str,
    base_branch: str,
) -> dict[str, Any]:
    """Build the GitHub API payload for PR creation."""
    return {
        "title": title,
        "body": body,
        "head": head_branch,
        "base": base_branch,
    }
