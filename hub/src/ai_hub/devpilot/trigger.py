"""Trigger guard — validates all preconditions before a DevPilot run."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from ai_hub.errors import FailureReason

logger = logging.getLogger("ai_hub.devpilot")

_MIN_BODY_CHARS = 20


@dataclass
class IssueSnapshot:
    number: int
    title: str
    body: str
    state: str
    labels: list[str]
    updated_at: str
    is_pull_request: bool = False

    @property
    def body_hash(self) -> str:
        raw = f"{self.title}:{self.body}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> IssueSnapshot:
        """Build a snapshot from a GitHub REST issue payload."""
        labels = [
            (label.get("name") if isinstance(label, dict) else str(label)) or ""
            for label in data.get("labels", [])
        ]
        return cls(
            number=int(data["number"]),
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            state=str(data.get("state") or ""),
            labels=[name for name in labels if name],
            updated_at=str(data.get("updated_at") or ""),
            is_pull_request="pull_request" in data,
        )


@dataclass
class TriggerResult:
    allowed: bool
    reason: str | None = None
    issue: IssueSnapshot | None = None
    failure_reason: FailureReason | None = None


def _deny(message: str, reason: FailureReason) -> TriggerResult:
    return TriggerResult(False, message, failure_reason=reason)


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
    if issue.is_pull_request:
        return _deny("Target is a pull request, not an issue", FailureReason.ISSUE_NOT_ACTIONABLE)

    if issue.state != "open":
        return _deny("Issue is not open", FailureReason.ISSUE_CLOSED)

    if "devpilot" not in issue.labels:
        return _deny("Issue does not have devpilot label", FailureReason.LABEL_REMOVED)

    if not dashboard_reachable:
        return _deny("Dashboard unreachable — fail closed", FailureReason.DASHBOARD_UNREACHABLE)

    if not repo_registered:
        return _deny("Repository is not registered", FailureReason.REPO_NOT_REGISTERED)

    if not devpilot_enabled:
        return _deny("DevPilot is disabled for this repository", FailureReason.REPO_DISABLED)

    if not bot_is_collaborator:
        return _deny(
            "Bot is not a collaborator on this repository", FailureReason.BOT_NOT_COLLABORATOR
        )

    if has_active_execution:
        return _deny(
            "An active execution already exists for this issue", FailureReason.DUPLICATE_EXECUTION
        )

    if has_open_pr:
        return _deny("An open DevPilot PR already exists for this issue", FailureReason.EXISTING_PR)

    return TriggerResult(True, issue=issue)


@dataclass
class TriageResult:
    actionable: bool
    missing_info: list[str] = field(default_factory=list)
    already_resolved_hint: bool = False
    rationale: str = ""


def triage_issue(issue: IssueSnapshot) -> TriageResult:
    """Cheap heuristic triage — rejects issues that obviously lack detail (no LLM call)."""
    missing: list[str] = []

    if not issue.title.strip():
        missing.append("Issue title is empty")

    body = (issue.body or "").strip()
    if len(body) < _MIN_BODY_CHARS:
        missing.append(f"Issue body is too short (< {_MIN_BODY_CHARS} chars)")

    return TriageResult(
        actionable=len(missing) == 0,
        missing_info=missing,
    )
