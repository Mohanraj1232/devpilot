"""Pre-push revalidation — checks run just before pushing."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ai_hub.errors import FailureReason

if TYPE_CHECKING:
    from pathlib import Path

    from ai_hub.devpilot.trigger import IssueSnapshot

logger = logging.getLogger("ai_hub.devpilot")


@dataclass
class PrePushCheck:
    passed: bool
    reason: str | None = None


def check_issue_still_open(issue: IssueSnapshot) -> PrePushCheck:
    if issue.state != "open":
        return PrePushCheck(False, "Issue was closed during execution")
    return PrePushCheck(True)


def check_issue_not_edited(original_hash: str, current: IssueSnapshot) -> PrePushCheck:
    if current.body_hash != original_hash:
        return PrePushCheck(False, "Issue was edited during execution")
    return PrePushCheck(True)


def check_base_not_moved(original_base_sha: str, current_base_sha: str) -> PrePushCheck:
    if original_base_sha != current_base_sha:
        return PrePushCheck(False, "Base branch moved during execution")
    return PrePushCheck(True)


def check_label_still_present(labels: list[str]) -> PrePushCheck:
    if "devpilot" not in labels:
        return PrePushCheck(False, "DevPilot label was removed during execution")
    return PrePushCheck(True)


def check_clean_tree(repo_path: Path) -> PrePushCheck:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.stdout.strip():
            return PrePushCheck(False, "Working tree is not clean")
        return PrePushCheck(True)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return PrePushCheck(False, "Cannot check tree state")


@dataclass
class PrePushResult:
    passed: bool
    failures: list[str]
    failure_reason: FailureReason | None = None


def run_preflight_checks(
    issue: IssueSnapshot,
    original_hash: str,
    original_base_sha: str,
    current_base_sha: str,
    repo_path: Path,
) -> PrePushResult:
    """Run all pre-push checks. Returns PrePushResult with aggregated failures."""
    failures: list[str] = []

    checks = [
        check_issue_still_open(issue),
        check_issue_not_edited(original_hash, issue),
        check_base_not_moved(original_base_sha, current_base_sha),
        check_label_still_present(issue.labels),
        check_clean_tree(repo_path),
    ]

    for check in checks:
        if not check.passed and check.reason:
            failures.append(check.reason)

    if failures:
        reason = FailureReason.BASE_MOVED
        for f in failures:
            if "closed" in f:
                reason = FailureReason.ISSUE_CLOSED
                break
            if "edited" in f:
                reason = FailureReason.ISSUE_EDITED
                break
            if "label" in f.lower():
                reason = FailureReason.ISSUE_CLOSED
                break

        return PrePushResult(False, failures, reason)

    return PrePushResult(True, [])
