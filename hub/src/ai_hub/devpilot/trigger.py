"""Trigger guard — validates all preconditions before a DevPilot run."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

logger = logging.getLogger("ai_hub.devpilot")


@dataclass
class IssueSnapshot:
    number: int
    title: str
    body: str
    state: str
    labels: list[str]
    updated_at: str

    @property
    def body_hash(self) -> str:
        raw = f"{self.title}:{self.body}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]


@dataclass
class TriggerResult:
    allowed: bool
    reason: str | None = None
    issue: IssueSnapshot | None = None


def check_trigger_preconditions(
    issue: IssueSnapshot,
    *,
    repo_registered: bool,
    devpilot_enabled: bool,
    bot_is_collaborator: bool,
    has_active_execution: bool,
    has_open_pr: bool,
    dashboard_reachable: bool = True,
) -> TriggerResult:
    """Validate all preconditions for a DevPilot execution.

    Returns TriggerResult with allowed=True if all checks pass.
    """
    if issue.state != "open":
        return TriggerResult(False, "Issue is not open")

    if "devpilot" not in issue.labels:
        return TriggerResult(False, "Issue does not have devpilot label")

    if not dashboard_reachable:
        return TriggerResult(False, "Dashboard unreachable — fail closed")

    if not repo_registered:
        return TriggerResult(False, "Repository is not registered")

    if not devpilot_enabled:
        return TriggerResult(False, "DevPilot is disabled for this repository")

    if not bot_is_collaborator:
        return TriggerResult(False, "Bot is not a collaborator on this repository")

    if has_active_execution:
        return TriggerResult(False, "An active execution already exists for this issue")

    if has_open_pr:
        return TriggerResult(False, "An open DevPilot PR already exists for this issue")

    return TriggerResult(True, issue=issue)


@dataclass
class TriageResult:
    actionable: bool
    missing_info: list[str]
    already_resolved_hint: bool = False


def triage_issue(issue: IssueSnapshot) -> TriageResult:
    """Basic heuristic triage — checks if issue has enough detail."""
    missing: list[str] = []

    if not issue.title.strip():
        missing.append("Issue title is empty")

    body = (issue.body or "").strip()
    if len(body) < 20:
        missing.append("Issue body is too short (< 20 chars)")

    return TriageResult(
        actionable=len(missing) == 0,
        missing_info=missing,
    )
