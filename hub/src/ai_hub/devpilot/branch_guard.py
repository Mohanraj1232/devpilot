"""Branch ownership and foreign commit detection."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("ai_hub.devpilot")


EXECUTION_TRAILER = "DevPilot-Execution"


@dataclass
class BranchOwnership:
    is_owned: bool
    foreign_commits: list[str]


def check_branch_ownership(repo_path: Path, base_branch: str, execution_id: str) -> BranchOwnership:
    """Verify all commits on this branch belong to the given execution."""
    try:
        result = subprocess.run(
            ["git", "log", f"origin/{base_branch}..HEAD", "--format=%H %s%n%b"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return BranchOwnership(False, ["Cannot read branch history"])

    foreign: list[str] = []
    current_sha = ""

    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if len(stripped.split(" ", 1)[0]) == 40:
            current_sha = stripped.split(" ", 1)[0]
            continue

        if stripped.startswith(f"{EXECUTION_TRAILER}:"):
            trailer_id = stripped.split(":", 1)[1].strip()
            if trailer_id != execution_id and current_sha:
                foreign.append(current_sha)

    return BranchOwnership(
        is_owned=len(foreign) == 0,
        foreign_commits=foreign,
    )


def is_devpilot_branch(branch_name: str) -> bool:
    """Check if a branch name follows the DevPilot naming convention."""
    return branch_name.startswith("devpilot/issue-")


def make_unique_branch(issue_number: int, title_slug: str, execution_id: str) -> str:
    """Generate a unique branch name using the execution ID suffix."""
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", title_slug.lower().strip())[:40].strip("-")
    return f"devpilot/issue-{issue_number}-{slug}-{execution_id[:8]}"
