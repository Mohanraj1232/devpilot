"""Duplicate execution prevention and label-based concurrency guard."""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("ai_hub.devpilot")

IN_PROGRESS_LABEL = "devpilot:in-progress"


@dataclass
class DuplicateCheckResult:
    is_duplicate: bool
    reason: str | None = None
    existing_execution_id: str | None = None


def check_for_duplicate(
    labels: list[str],
    *,
    has_dashboard_lock: bool = False,
    existing_execution_id: str | None = None,
) -> DuplicateCheckResult:
    """Check if a duplicate execution exists using labels and dashboard lock.

    Uses a layered approach: label check first, then dashboard lock.
    """
    if IN_PROGRESS_LABEL in labels:
        return DuplicateCheckResult(
            is_duplicate=True,
            reason="In-progress label already present",
            existing_execution_id=existing_execution_id,
        )

    if has_dashboard_lock:
        return DuplicateCheckResult(
            is_duplicate=True,
            reason="Dashboard lock exists for this issue",
            existing_execution_id=existing_execution_id,
        )

    return DuplicateCheckResult(is_duplicate=False)


def should_update_existing_pr(
    has_open_pr: bool,
    pr_branch_owned: bool,
) -> bool:
    """Decide whether to update an existing PR instead of creating a new one."""
    return has_open_pr and pr_branch_owned
