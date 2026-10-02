"""Tests for DevPilot hardening modules (M5)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path

from ai_hub.devpilot.branch_guard import (
    check_branch_ownership,
    is_devpilot_branch,
    make_unique_branch,
)
from ai_hub.devpilot.duplicate import (
    IN_PROGRESS_LABEL,
    check_for_duplicate,
    should_update_existing_pr,
)
from ai_hub.devpilot.flaky import (
    detect_flaky_from_hashes,
    detect_flaky_from_results,
)
from ai_hub.devpilot.guardrails import (
    check_destructive_operations,
    run_all_guardrails,
)
from ai_hub.devpilot.preflight import (
    check_base_not_moved,
    check_clean_tree,
    check_issue_not_edited,
    check_issue_still_open,
    check_label_still_present,
    run_preflight_checks,
)
from ai_hub.devpilot.secret_scan import scan_staged_diff
from ai_hub.devpilot.trigger import IssueSnapshot
from ai_hub.models import CheckStatus


def _make_issue(**overrides: Any) -> IssueSnapshot:
    defaults = {
        "number": 42,
        "title": "Fix login bug",
        "body": "The login form crashes when email has special characters",
        "state": "open",
        "labels": ["devpilot", "bug"],
        "updated_at": "2026-01-15T10:00:00Z",
    }
    defaults.update(overrides)
    return IssueSnapshot(**defaults)


# ── Preflight tests ───────────────────────────────────────────


class TestPreflight:
    def test_issue_still_open_passes(self) -> None:
        result = check_issue_still_open(_make_issue())
        assert result.passed is True

    def test_issue_closed_fails(self) -> None:
        result = check_issue_still_open(_make_issue(state="closed"))
        assert result.passed is False
        assert "closed" in (result.reason or "")

    def test_issue_not_edited_passes(self) -> None:
        issue = _make_issue()
        result = check_issue_not_edited(issue.body_hash, issue)
        assert result.passed is True

    def test_issue_edited_fails(self) -> None:
        original = _make_issue()
        edited = _make_issue(body="completely different body with new requirements")
        result = check_issue_not_edited(original.body_hash, edited)
        assert result.passed is False

    def test_base_not_moved_passes(self) -> None:
        result = check_base_not_moved("abc123", "abc123")
        assert result.passed is True

    def test_base_moved_fails(self) -> None:
        result = check_base_not_moved("abc123", "def456")
        assert result.passed is False

    def test_label_present_passes(self) -> None:
        result = check_label_still_present(["devpilot", "bug"])
        assert result.passed is True

    def test_label_removed_fails(self) -> None:
        result = check_label_still_present(["bug"])
        assert result.passed is False

    def test_clean_tree_passes(self, tmp_path: Path) -> None:
        with patch("ai_hub.devpilot.preflight.subprocess.run") as mock:
            mock.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            result = check_clean_tree(tmp_path)
        assert result.passed is True

    def test_dirty_tree_fails(self, tmp_path: Path) -> None:
        with patch("ai_hub.devpilot.preflight.subprocess.run") as mock:
            mock.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="M file.py\n", stderr=""
            )
            result = check_clean_tree(tmp_path)
        assert result.passed is False

    def test_run_preflight_all_pass(self, tmp_path: Path) -> None:
        issue = _make_issue()
        with patch("ai_hub.devpilot.preflight.subprocess.run") as mock:
            mock.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            result = run_preflight_checks(issue, issue.body_hash, "abc123", "abc123", tmp_path)
        assert result.passed is True
        assert result.failures == []

    def test_run_preflight_multiple_failures(self, tmp_path: Path) -> None:
        issue = _make_issue(state="closed", labels=["bug"])
        with patch("ai_hub.devpilot.preflight.subprocess.run") as mock:
            mock.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            result = run_preflight_checks(issue, "different_hash", "abc", "def", tmp_path)
        assert result.passed is False
        assert len(result.failures) >= 2


# ── Branch guard tests ────────────────────────────────────────


class TestBranchGuard:
    def test_is_devpilot_branch(self) -> None:
        assert is_devpilot_branch("devpilot/issue-42-fix-login") is True
        assert is_devpilot_branch("feature/add-auth") is False
        assert is_devpilot_branch("main") is False

    def test_make_unique_branch(self) -> None:
        name = make_unique_branch(42, "Fix login bug!", "abcdef1234567890")
        assert name.startswith("devpilot/issue-42-")
        assert "abcdef12" in name

    def _repo_with_origin(self, tmp_path: Path) -> Path:
        def git(*args: str) -> None:
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                cwd=tmp_path,
                check=True,
                capture_output=True,
            )

        git("init", "-b", "main")
        (tmp_path / "a.txt").write_text("a")
        git("add", "-A")
        git("commit", "-m", "base")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        return tmp_path

    def _commit(self, repo: Path, name: str, message: str) -> None:
        (repo / name).write_text(name)
        for args in (["add", "-A"], ["commit", "-m", message]):
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                cwd=repo,
                check=True,
                capture_output=True,
            )

    def test_branch_owned(self, tmp_path: Path) -> None:
        repo = self._repo_with_origin(tmp_path)
        self._commit(repo, "b.txt", "Fix login\n\nDevPilot-Execution: exec-001")
        result = check_branch_ownership(repo, "main", "exec-001")
        assert result.is_owned is True

    def test_branch_with_no_commits_is_owned(self, tmp_path: Path) -> None:
        repo = self._repo_with_origin(tmp_path)
        assert check_branch_ownership(repo, "main", "exec-001").is_owned is True

    def test_branch_foreign_commits(self, tmp_path: Path) -> None:
        repo = self._repo_with_origin(tmp_path)
        self._commit(repo, "b.txt", "Fix login\n\nDevPilot-Execution: exec-999")
        result = check_branch_ownership(repo, "main", "exec-001")
        assert result.is_owned is False
        assert len(result.foreign_commits) == 1

    def test_plain_developer_commit_is_foreign(self, tmp_path: Path) -> None:
        repo = self._repo_with_origin(tmp_path)
        self._commit(repo, "b.txt", "Fix login\n\nDevPilot-Execution: exec-001")
        self._commit(repo, "c.txt", "developer tweak")
        result = check_branch_ownership(repo, "main", "exec-001")
        assert result.is_owned is False
        assert len(result.foreign_commits) == 1

    def test_unreadable_history_is_not_owned(self, tmp_path: Path) -> None:
        # Not a git repository: ownership cannot be verified, so fail closed.
        assert check_branch_ownership(tmp_path, "main", "exec-001").is_owned is False


# ── Flaky detection tests ─────────────────────────────────────


class TestFlakyDetection:
    def test_consistent_passes(self) -> None:
        results = [CheckStatus.SUCCESS, CheckStatus.SUCCESS, CheckStatus.SUCCESS]
        report = detect_flaky_from_results(results)
        assert report.is_flaky is False

    def test_consistent_failures(self) -> None:
        results = [CheckStatus.FAILED, CheckStatus.FAILED]
        report = detect_flaky_from_results(results)
        assert report.is_flaky is False

    def test_flaky_detected(self) -> None:
        results = [CheckStatus.FAILED, CheckStatus.SUCCESS, CheckStatus.FAILED]
        report = detect_flaky_from_results(results)
        assert report.is_flaky is True
        assert "Inconsistent" in report.evidence

    def test_flaky_from_hashes_consistent(self) -> None:
        hashes = [
            ("hash1", CheckStatus.SUCCESS),
            ("hash2", CheckStatus.SUCCESS),
        ]
        report = detect_flaky_from_hashes(hashes)
        assert report.is_flaky is False

    def test_flaky_from_hashes_detected(self) -> None:
        hashes = [
            ("hash1", CheckStatus.SUCCESS),
            ("hash1", CheckStatus.FAILED),
        ]
        report = detect_flaky_from_hashes(hashes)
        assert report.is_flaky is True


# ── Duplicate detection tests ─────────────────────────────────


class TestDuplicateDetection:
    def test_no_duplicate(self) -> None:
        result = check_for_duplicate(["devpilot", "bug"])
        assert result.is_duplicate is False

    def test_label_duplicate(self) -> None:
        result = check_for_duplicate(["devpilot", "bug", IN_PROGRESS_LABEL])
        assert result.is_duplicate is True
        assert "label" in (result.reason or "").lower()

    def test_dashboard_lock_duplicate(self) -> None:
        result = check_for_duplicate(
            ["devpilot"], has_dashboard_lock=True, existing_execution_id="exec-1"
        )
        assert result.is_duplicate is True

    def test_should_update_existing_pr(self) -> None:
        assert should_update_existing_pr(True, True) is True
        assert should_update_existing_pr(True, False) is False
        assert should_update_existing_pr(False, True) is False


# ── Secret scan tests ─────────────────────────────────────────


class TestSecretScan:
    def test_scan_gitleaks_not_installed(self, tmp_path: Path) -> None:
        with patch("ai_hub.devpilot.secret_scan.subprocess.run") as mock:
            mock.side_effect = FileNotFoundError("gitleaks not found")
            result = scan_staged_diff(tmp_path)
        assert result.clean is True

    def test_scan_timeout(self, tmp_path: Path) -> None:
        with patch("ai_hub.devpilot.secret_scan.subprocess.run") as mock:
            mock.side_effect = subprocess.TimeoutExpired(cmd="gitleaks", timeout=60)
            result = scan_staged_diff(tmp_path)
        assert result.clean is False

    def test_scan_clean(self, tmp_path: Path) -> None:
        with patch("ai_hub.devpilot.secret_scan.subprocess.run") as mock:
            mock.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            with patch("ai_hub.devpilot.secret_scan._get_staged_diff", return_value=""):
                result = scan_staged_diff(tmp_path)
        assert result.clean is True

    def test_scan_found_secrets(self, tmp_path: Path) -> None:
        with patch("ai_hub.devpilot.secret_scan.subprocess.run") as mock:
            mock.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="AWS key found in config.py", stderr=""
            )
            with patch("ai_hub.devpilot.secret_scan._get_staged_diff", return_value="diff"):
                result = scan_staged_diff(tmp_path)
        assert result.clean is False
        assert len(result.findings) > 0


# ── Enhanced guardrails tests ─────────────────────────────────


class TestDestructiveGuardrails:
    def test_no_deletions_passes(self) -> None:
        result = check_destructive_operations(["a.py"], [])
        assert result.passed is True

    def test_mass_deletions_fails(self) -> None:
        deleted = [f"file_{i}.py" for i in range(10)]
        result = check_destructive_operations([], deleted)
        assert result.passed is False

    def test_mass_deletions_allowed(self) -> None:
        deleted = [f"file_{i}.py" for i in range(10)]
        result = check_destructive_operations([], deleted, allow_destructive=True)
        assert result.passed is True

    def test_security_config_blocked(self) -> None:
        result = check_destructive_operations([".github/settings.yml"], [])
        assert result.passed is False

    def test_run_all_with_destructive(self) -> None:
        deleted = [f"f_{i}.py" for i in range(10)]
        result = run_all_guardrails(["src/main.py"], 50, ["src/main.py"], deleted_files=deleted)
        assert result.passed is False
