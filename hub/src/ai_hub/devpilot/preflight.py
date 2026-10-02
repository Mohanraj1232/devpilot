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
    failure_reason: FailureReason | None = None


def check_issue_still_open(issue: IssueSnapshot) -> PrePushCheck:
    if issue.state != "open":
        return PrePushCheck(False, "Issue was closed during execution", FailureReason.ISSUE_CLOSED)
    return PrePushCheck(True)


def check_issue_not_edited(original_hash: str, current: IssueSnapshot) -> PrePushCheck:
    if current.body_hash != original_hash:
        return PrePushCheck(False, "Issue was edited during execution", FailureReason.ISSUE_EDITED)
    return PrePushCheck(True)


def check_base_not_moved(original_base_sha: str, current_base_sha: str) -> PrePushCheck:
    if original_base_sha != current_base_sha:
        return PrePushCheck(False, "Base branch moved during execution", FailureReason.BASE_MOVED)
    return PrePushCheck(True)


def check_label_still_present(labels: list[str]) -> PrePushCheck:
    if "devpilot" not in labels:
        return PrePushCheck(
            False, "DevPilot label was removed during execution", FailureReason.LABEL_REMOVED
        )
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
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return PrePushCheck(False, "Cannot check tree state", FailureReason.DIRTY_WORKSPACE)
    if result.returncode != 0:
        return PrePushCheck(False, "Cannot check tree state", FailureReason.DIRTY_WORKSPACE)
    if result.stdout.strip():
        return PrePushCheck(False, "Working tree is not clean", FailureReason.DIRTY_WORKSPACE)
    return PrePushCheck(True)


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
    """Run all pre-push checks. The failure reason is that of the first failing check."""
    checks = [
        check_issue_still_open(issue),
        check_issue_not_edited(original_hash, issue),
        check_label_still_present(issue.labels),
        check_base_not_moved(original_base_sha, current_base_sha),
        check_clean_tree(repo_path),
    ]
    failed = [c for c in checks if not c.passed]
    if not failed:
        return PrePushResult(True, [])

    return PrePushResult(
        False,
        [c.reason for c in failed if c.reason],
        failed[0].failure_reason,
    )
