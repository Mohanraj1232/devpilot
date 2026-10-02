"""Git operations — branch management, commit, push, and PR creation."""

from __future__ import annotations

import logging
import re
import subprocess
from typing import TYPE_CHECKING

from ai_hub.errors import FailureReason, GitError

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("ai_hub.devpilot")


def _run_git(args: list[str], cwd: Path, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run a git command with error handling."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"Git command timed out: git {' '.join(args)}"
        raise GitError(FailureReason.TIMEOUT, msg) from exc


def create_branch(repo_path: Path, branch_name: str) -> None:
    """Create and checkout a new branch."""
    result = _run_git(["checkout", "-b", branch_name], cwd=repo_path)
    if result.returncode != 0:
        raise GitError(
            FailureReason.COMMIT_FAILED,
            f"Failed to create branch {branch_name}: {result.stderr}",
        )


def make_branch_name(issue_number: int, title: str, execution_id: str | None = None) -> str:
    """Generate a branch name from an issue."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower().strip())[:40].strip("-")
    name = f"devpilot/issue-{issue_number}-{slug}"
    if execution_id:
        name += f"-{execution_id[:8]}"
    return name


def get_current_sha(repo_path: Path) -> str:
    """Get the current HEAD SHA."""
    result = _run_git(["rev-parse", "HEAD"], cwd=repo_path)
    return result.stdout.strip()


def has_changes(repo_path: Path) -> bool:
    """Check if there are uncommitted changes."""
    result = _run_git(["status", "--porcelain"], cwd=repo_path)
    return bool(result.stdout.strip())


def stage_and_commit(
    repo_path: Path,
    message: str,
    *,
    execution_id: str | None = None,
) -> str | None:
    """Stage all changes and create a commit. Returns the commit SHA or None."""
    if not has_changes(repo_path):
        return None

    _run_git(["add", "-A"], cwd=repo_path)

    if execution_id:
        message += f"\n\nDevPilot-Execution: {execution_id}"

    result = _run_git(["commit", "-m", message], cwd=repo_path)
    if result.returncode != 0:
        raise GitError(FailureReason.COMMIT_FAILED, f"Commit failed: {result.stderr}")

    return get_current_sha(repo_path)


def push_branch(repo_path: Path, branch_name: str) -> None:
    """Push the current branch to origin."""
    result = _run_git(["push", "-u", "origin", branch_name], cwd=repo_path, timeout=120)
    if result.returncode != 0:
        raise GitError(FailureReason.PUSH_FAILED, f"Push failed: {result.stderr}")


def rebase_on_base(repo_path: Path, base_branch: str) -> bool:
    """Attempt to rebase the current branch on the base. Returns False on conflict."""
    _run_git(["fetch", "origin", base_branch], cwd=repo_path, timeout=120)
    result = _run_git(["rebase", f"origin/{base_branch}"], cwd=repo_path)
    if result.returncode != 0:
        _run_git(["rebase", "--abort"], cwd=repo_path)
        return False
    return True


def check_no_foreign_commits(repo_path: Path, base_branch: str, execution_id: str) -> bool:
    """Verify that the branch only contains commits from this execution."""
    result = _run_git(
        ["log", f"origin/{base_branch}..HEAD", "--format=%s%n%b"],
        cwd=repo_path,
    )
    for line in result.stdout.splitlines():
        if line.startswith("DevPilot-Execution:") and execution_id not in line:
            return False
    return True
