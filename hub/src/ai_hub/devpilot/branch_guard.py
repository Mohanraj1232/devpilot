"""Branch ownership and foreign commit detection."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ai_hub.devpilot.git_ops import make_branch_name

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("ai_hub.devpilot")


EXECUTION_TRAILER = "DevPilot-Execution"
_RECORD_SEP = "\x1e"
_UNIT_SEP = "\x1f"


@dataclass
class BranchOwnership:
    is_owned: bool
    foreign_commits: list[str]


def check_branch_ownership(repo_path: Path, base_branch: str, execution_id: str) -> BranchOwnership:
    """Verify every commit on this branch (beyond the base) was made by this execution.

    A commit is foreign unless it carries this execution's ``DevPilot-Execution`` trailer,
    so a developer's plain commit on the branch is detected too.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                f"origin/{base_branch}..HEAD",
                f"--format=%H{_UNIT_SEP}%B{_RECORD_SEP}",
            ],
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return BranchOwnership(False, ["Cannot read branch history"])

    if result.returncode != 0:
        return BranchOwnership(False, ["Cannot read branch history"])

    foreign: list[str] = []
    for record in result.stdout.split(_RECORD_SEP):
        sha, _, body = record.strip().partition(_UNIT_SEP)
        if not sha:
            continue
        owners = {
            line.split(":", 1)[1].strip()
            for line in body.splitlines()
            if line.startswith(f"{EXECUTION_TRAILER}:")
        }
        if execution_id not in owners:
            foreign.append(sha)

    return BranchOwnership(is_owned=not foreign, foreign_commits=foreign)


def is_devpilot_branch(branch_name: str) -> bool:
    """Check if a branch name follows the DevPilot naming convention."""
    return branch_name.startswith("devpilot/issue-")


def make_unique_branch(issue_number: int, title_slug: str, execution_id: str) -> str:
    """Generate a unique branch name using the execution ID suffix."""
    return make_branch_name(issue_number, title_slug, execution_id)
