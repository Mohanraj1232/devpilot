"""Shared helpers for the review-pipeline tests: real git repositories with a PR merge commit."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from ai_hub.analysis.base import ToolAdapter
from ai_hub.models import CheckResult, Finding, FindingCategory, FindingSource, Severity

if TYPE_CHECKING:
    from pathlib import Path


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def write_files(repo: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def make_pr_repo(
    root: Path,
    *,
    base: dict[str, str],
    pr: dict[str, str],
    pr_deletes: tuple[str, ...] = (),
) -> Path:
    """A repository checked out at a merge commit, like `actions/checkout` does for a PR.

    HEAD^1 is the base branch tip and HEAD^2 the PR head, so `git diff HEAD^1 HEAD` is
    exactly the pull request's changes.
    """
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    write_files(repo, base)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base", "--allow-empty")
    git(repo, "checkout", "-b", "pr")
    write_files(repo, pr)
    for name in pr_deletes:
        (repo / name).unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "the change", "--allow-empty")
    git(repo, "checkout", "main")
    # A commit on main after the branch point, so the merge is a real merge commit.
    write_files(repo, {"BASE_MARKER.txt": "base moved on\n"})
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base moves on")
    git(repo, "merge", "--no-ff", "pr", "-m", "Merge pull request")
    return repo


def make_finding(
    file: str = "src/app.py",
    line: int = 1,
    severity: Severity = Severity.HIGH,
    *,
    tool: str = "ruff",
    source: FindingSource = FindingSource.STATIC,
    title: str = "problem",
    line_end: int | None = None,
) -> Finding:
    return Finding(
        source=source,
        tool=tool,
        category=FindingCategory.BUG,
        severity=severity,
        file=file,
        line_start=line,
        line_end=line_end,
        title=title,
        explanation="explanation",
    )


class FakeAdapter(ToolAdapter):
    """An adapter with scripted behaviour for pipeline tests."""

    tool_cmd = "fake"

    def __init__(
        self,
        name: str,
        findings: list[Finding] | None = None,
        *,
        error: Exception | None = None,
        applicable: tuple[bool, str] = (True, ""),
    ) -> None:
        self.name = name
        self._findings = findings or []
        self._error = error
        self._applicable = applicable

    def applicable(self, repo_path: Path, changed_files: list[str] | None = None):  # type: ignore[override]
        return self._applicable

    def run(self, repo_path: Path, changed_files: list[str] | None = None) -> list[Finding]:
        if self._error:
            raise self._error
        return list(self._findings)


def check(name: str, status: str, summary: str = "") -> CheckResult:
    return CheckResult(name=name, status=status, summary=summary or status)  # type: ignore[arg-type]
