"""Git operations — branch management, commit, push and workspace inspection.

All git commands run with repository hooks disabled: hooks installed by a
dependency install (e.g. husky) would otherwise execute inside the orchestrator
process environment, which holds the bot token.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ai_hub.errors import FailureReason, GitError
from ai_hub.safety.redact import redact

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

logger = logging.getLogger("ai_hub.devpilot")


DEFAULT_IDENTITY = ("DevPilot", "devpilot@users.noreply.github.com")


def identity_env(identity: tuple[str, str] | None) -> dict[str, str]:
    """Environment that supplies a commit author/committer.

    Commits and rebases fail on a machine with no git identity (every CI runner), so one is
    always provided; the DevPilot bot's own identity is passed in by the orchestrator.
    """
    name, email = identity or DEFAULT_IDENTITY
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
        }
    )
    return env


def git_auth_env(token: str, *, server_url: str = "https://github.com") -> dict[str, str]:
    """Environment that authenticates git over HTTPS without writing the token to disk or argv."""
    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = dict(os.environ)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{server_url.rstrip('/')}/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credentials}",
        }
    )
    return env


def _run_git(
    args: list[str],
    cwd: Path,
    *,
    timeout: int = 60,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a git command with hooks disabled and error handling."""
    full_args = ["git", "-c", f"core.hooksPath={os.devnull}", *args]
    try:
        return subprocess.run(
            full_args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=dict(env) if env is not None else None,
            input=input_text,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"Git command timed out: git {' '.join(args)}"
        raise GitError(FailureReason.TIMEOUT, msg) from exc
    except FileNotFoundError as exc:
        raise GitError(FailureReason.INTERNAL_ERROR, "git executable not found") from exc


def _fail(reason: FailureReason, action: str, result: subprocess.CompletedProcess[str]) -> GitError:
    return GitError(reason, f"{action}: {redact((result.stderr or result.stdout or '').strip())}")


def clone_repo(
    url: str, dest: Path, *, token: str, base_branch: str, server_url: str = "https://github.com"
) -> None:
    """Clone ``url`` (plain HTTPS, no embedded credentials) into ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = _run_git(
        ["clone", "--branch", base_branch, "--", url, str(dest)],
        cwd=dest.parent,
        timeout=600,
        env=git_auth_env(token, server_url=server_url),
    )
    if result.returncode != 0:
        raise _fail(FailureReason.CLONE_FAILURE, "Clone failed", result)


def checkout_base(
    repo_path: Path, base_branch: str, *, env: Mapping[str, str] | None = None
) -> str:
    """Fetch the base branch and check out its tip (detached). Returns the commit SHA."""
    fetch = _run_git(["fetch", "origin", base_branch], cwd=repo_path, timeout=300, env=env)
    if fetch.returncode != 0:
        raise _fail(FailureReason.REPO_INACCESSIBLE, "Fetch failed", fetch)
    checkout = _run_git(["checkout", "--detach", f"origin/{base_branch}"], cwd=repo_path)
    if checkout.returncode != 0:
        raise _fail(FailureReason.CLONE_FAILURE, "Checkout of base failed", checkout)
    return get_current_sha(repo_path)


def create_branch(repo_path: Path, branch_name: str) -> None:
    """Create and checkout a new branch."""
    result = _run_git(["checkout", "-b", branch_name], cwd=repo_path)
    if result.returncode != 0:
        raise _fail(FailureReason.COMMIT_FAILED, f"Failed to create branch {branch_name}", result)


_GENERATED_PATTERNS = [
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".coverage",
    "*.egg-info/",
    ".tox/",
    ".venv/",
    "venv/",
    "node_modules/",
]


def exclude_generated_files(repo_path: Path) -> None:
    """Keep files created by dependency installs and test runs out of DevPilot's commit.

    Everything untracked right now (the pre-existing baseline) plus common build/test
    artefacts is added to ``.git/info/exclude`` — a local-only ignore file.
    """
    result = _run_git(["rev-parse", "--git-path", "info/exclude"], cwd=repo_path)
    if result.returncode != 0:
        return
    exclude_file = repo_path / result.stdout.strip()
    status = _run_git(["status", "--porcelain", "--untracked-files=normal"], cwd=repo_path)
    baseline = [
        line[3:].strip().strip('"') for line in status.stdout.splitlines() if line.startswith("??")
    ]
    try:
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        with exclude_file.open("a", encoding="utf-8") as fh:
            fh.write("\n# added by DevPilot\n")
            for pattern in [*_GENERATED_PATTERNS, *baseline]:
                fh.write(pattern + "\n")
    except OSError as exc:
        logger.warning("Could not update git exclude file: %s", exc)


def make_branch_name(issue_number: int, title: str, execution_id: str | None = None) -> str:
    """Generate a branch name from an issue."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower().strip())[:40].strip("-")
    name = f"devpilot/issue-{issue_number}-{slug}" if slug else f"devpilot/issue-{issue_number}"
    if execution_id:
        name += f"-{execution_id[:8]}"
    return name


def get_current_sha(repo_path: Path) -> str:
    """Get the current HEAD SHA."""
    result = _run_git(["rev-parse", "HEAD"], cwd=repo_path)
    return result.stdout.strip()


def has_changes(repo_path: Path) -> bool:
    """Check if there are uncommitted changes (tracked or untracked)."""
    result = _run_git(["status", "--porcelain"], cwd=repo_path)
    return bool(result.stdout.strip())


def working_tree_hash(repo_path: Path) -> str:
    """Hash of the full working-tree change set, used to detect repeated (failed) fixes."""
    try:
        # Intent-to-add makes untracked files visible to `git diff` without staging content.
        _run_git(["add", "-A", "-N"], cwd=repo_path)
        diff = _run_git(["diff", "HEAD", "--no-color"], cwd=repo_path)
        return hashlib.sha256((diff.stdout or "").encode()).hexdigest()[:12]
    except GitError:
        return "unknown"


@dataclass
class ChangeSet:
    files: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    lines: int = 0
    binary: list[str] = field(default_factory=list)


def stage_all(repo_path: Path) -> None:
    result = _run_git(["add", "-A"], cwd=repo_path)
    if result.returncode != 0:
        raise _fail(FailureReason.COMMIT_FAILED, "Staging failed", result)


def staged_changes(repo_path: Path) -> ChangeSet:
    """Summarise staged changes: changed files, deletions and total changed lines."""
    status = _run_git(["diff", "--cached", "--name-status", "--no-renames"], cwd=repo_path)
    numstat = _run_git(["diff", "--cached", "--numstat", "--no-renames"], cwd=repo_path)
    changes = ChangeSet()
    for line in status.stdout.splitlines():
        code, _, path = line.partition("\t")
        if not path:
            continue
        changes.files.append(path)
        if code.startswith("D"):
            changes.deleted.append(path)
    for line in numstat.stdout.splitlines():
        added, _, rest = line.partition("\t")
        deleted, _, path = rest.partition("\t")
        if added == "-" or deleted == "-":
            changes.binary.append(path)
            continue
        changes.lines += int(added or 0) + int(deleted or 0)
    return changes


def staged_diff_text(repo_path: Path) -> str:
    return _run_git(["diff", "--cached", "--no-color"], cwd=repo_path).stdout


def stage_and_commit(
    repo_path: Path,
    message: str,
    *,
    execution_id: str | None = None,
    identity: tuple[str, str] | None = None,
) -> str | None:
    """Stage all changes and create a commit. Returns the commit SHA or None if no changes."""
    if not has_changes(repo_path):
        return None

    stage_all(repo_path)

    if execution_id:
        message += f"\n\nDevPilot-Execution: {execution_id}"

    result = _run_git(["commit", "-m", message], cwd=repo_path, env=identity_env(identity))
    if result.returncode != 0:
        raise _fail(FailureReason.COMMIT_FAILED, "Commit failed", result)

    return get_current_sha(repo_path)


def push_branch(repo_path: Path, branch_name: str, *, env: Mapping[str, str] | None = None) -> None:
    """Push the current branch to origin. Never forces."""
    result = _run_git(["push", "-u", "origin", branch_name], cwd=repo_path, timeout=300, env=env)
    if result.returncode != 0:
        raise _fail(FailureReason.PUSH_FAILED, "Push failed", result)


def rebase_on_base(
    repo_path: Path,
    base_branch: str,
    *,
    env: Mapping[str, str] | None = None,
    identity: tuple[str, str] | None = None,
) -> bool:
    """Rebase the current branch on the base.

    Returns False on a merge conflict (the rebase is aborted and the tree left clean).
    Any other failure raises GitError: a rebase *rewrites commits*, so it needs a committer
    identity (CI runners have none) and must not be mistaken for a conflict.
    """
    fetch = _run_git(["fetch", "origin", base_branch], cwd=repo_path, timeout=300, env=env)
    if fetch.returncode != 0:
        raise _fail(FailureReason.REPO_INACCESSIBLE, "Fetch failed", fetch)
    result = _run_git(
        ["rebase", f"origin/{base_branch}"], cwd=repo_path, env=identity_env(identity)
    )
    if result.returncode == 0:
        return True

    unmerged = _run_git(["diff", "--name-only", "--diff-filter=U"], cwd=repo_path).stdout.strip()
    _run_git(["rebase", "--abort"], cwd=repo_path)
    if unmerged:
        return False
    raise _fail(FailureReason.COMMIT_FAILED, "Rebase failed", result)
