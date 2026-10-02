"""Tests for DevPilot core modules (M4)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from ai_hub.devpilot.agent import _handle_apply_edit, run_agent_loop
from ai_hub.devpilot.git_ops import (
    has_changes,
    make_branch_name,
    stage_and_commit,
)
from ai_hub.devpilot.guardrails import (
    check_diff_size,
    check_forbidden_changes,
    check_unrelated_changes,
    run_all_guardrails,
)
from ai_hub.devpilot.lock import ExecutionLock, create_lock_key
from ai_hub.devpilot.pr import build_pr_body, build_pr_payload
from ai_hub.devpilot.tester import run_test_repair_loop, run_tests
from ai_hub.devpilot.tools import (
    ToolPolicy,
    _is_path_allowed,
    tool_list_dir,
    tool_read_file,
    tool_search_code,
    tool_write_file,
)
from ai_hub.devpilot.trigger import (
    IssueSnapshot,
    check_trigger_preconditions,
    triage_issue,
)
from ai_hub.devpilot.workspace import (
    KNOWN_PROJECTS,
    detect_project_type,
    is_empty_repo,
)
from ai_hub.models import CheckStatus

# ── Trigger tests ──────────────────────────────────────────────


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


class TestTriggerGuard:
    def test_all_preconditions_met(self) -> None:
        result = check_trigger_preconditions(
            _make_issue(),
            repo_registered=True,
            devpilot_enabled=True,
            bot_is_collaborator=True,
            has_active_execution=False,
            has_open_pr=False,
        )
        assert result.allowed is True
        assert result.issue is not None

    def test_closed_issue_rejected(self) -> None:
        result = check_trigger_preconditions(
            _make_issue(state="closed"),
            repo_registered=True,
            devpilot_enabled=True,
            bot_is_collaborator=True,
            has_active_execution=False,
            has_open_pr=False,
        )
        assert result.allowed is False
        assert "not open" in (result.reason or "")

    def test_missing_label_rejected(self) -> None:
        result = check_trigger_preconditions(
            _make_issue(labels=["bug"]),
            repo_registered=True,
            devpilot_enabled=True,
            bot_is_collaborator=True,
            has_active_execution=False,
            has_open_pr=False,
        )
        assert result.allowed is False
        assert "label" in (result.reason or "").lower()

    def test_unregistered_repo_rejected(self) -> None:
        result = check_trigger_preconditions(
            _make_issue(),
            repo_registered=False,
            devpilot_enabled=True,
            bot_is_collaborator=True,
            has_active_execution=False,
            has_open_pr=False,
        )
        assert result.allowed is False
        assert "registered" in (result.reason or "").lower()

    def test_disabled_repo_rejected(self) -> None:
        result = check_trigger_preconditions(
            _make_issue(),
            repo_registered=True,
            devpilot_enabled=False,
            bot_is_collaborator=True,
            has_active_execution=False,
            has_open_pr=False,
        )
        assert result.allowed is False

    def test_bot_not_collaborator_rejected(self) -> None:
        result = check_trigger_preconditions(
            _make_issue(),
            repo_registered=True,
            devpilot_enabled=True,
            bot_is_collaborator=False,
            has_active_execution=False,
            has_open_pr=False,
        )
        assert result.allowed is False
        assert "collaborator" in (result.reason or "").lower()

    def test_active_execution_rejected(self) -> None:
        result = check_trigger_preconditions(
            _make_issue(),
            repo_registered=True,
            devpilot_enabled=True,
            bot_is_collaborator=True,
            has_active_execution=True,
            has_open_pr=False,
        )
        assert result.allowed is False

    def test_existing_pr_rejected(self) -> None:
        result = check_trigger_preconditions(
            _make_issue(),
            repo_registered=True,
            devpilot_enabled=True,
            bot_is_collaborator=True,
            has_active_execution=False,
            has_open_pr=True,
        )
        assert result.allowed is False

    def test_dashboard_unreachable_fails_closed(self) -> None:
        result = check_trigger_preconditions(
            _make_issue(),
            repo_registered=True,
            devpilot_enabled=True,
            bot_is_collaborator=True,
            has_active_execution=False,
            has_open_pr=False,
            dashboard_reachable=False,
        )
        assert result.allowed is False
        assert "unreachable" in (result.reason or "").lower()


class TestTriage:
    def test_valid_issue_is_actionable(self) -> None:
        issue = _make_issue()
        result = triage_issue(issue)
        assert result.actionable is True
        assert result.missing_info == []

    def test_empty_title_flagged(self) -> None:
        issue = _make_issue(title="")
        result = triage_issue(issue)
        assert result.actionable is False
        assert len(result.missing_info) > 0

    def test_short_body_flagged(self) -> None:
        issue = _make_issue(body="fix it")
        result = triage_issue(issue)
        assert result.actionable is False


class TestIssueSnapshot:
    def test_body_hash_deterministic(self) -> None:
        issue = _make_issue()
        assert issue.body_hash == issue.body_hash
        assert len(issue.body_hash) == 12

    def test_different_body_different_hash(self) -> None:
        a = _make_issue(body="body a")
        b = _make_issue(body="body b")
        assert a.body_hash != b.body_hash


# ── Lock tests ─────────────────────────────────────────────────


class TestLock:
    def test_make_key(self) -> None:
        key = ExecutionLock.make_key("owner/repo", 42)
        assert key == "devpilot:owner/repo:issue-42"

    def test_create_lock(self) -> None:
        lock = create_lock_key("owner/repo", 42, "exec-abc123")
        assert lock.repo_full_name == "owner/repo"
        assert lock.issue_number == 42
        assert lock.execution_id == "exec-abc123"
        assert "issue-42" in lock.lock_key


# ── Workspace tests ────────────────────────────────────────────


class TestWorkspace:
    def test_detect_python_project(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").touch()
        project = detect_project_type(tmp_path)
        assert project is not None
        assert project.language == "python"

    def test_detect_node_project(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").touch()
        project = detect_project_type(tmp_path)
        assert project is not None
        assert project.language == "javascript"

    def test_detect_go_project(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").touch()
        project = detect_project_type(tmp_path)
        assert project is not None
        assert project.language == "go"

    def test_detect_unknown_project(self, tmp_path: Path) -> None:
        project = detect_project_type(tmp_path)
        assert project is None

    def test_empty_repo(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        assert is_empty_repo(tmp_path) is True

    def test_nonempty_repo(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "README.md").touch()
        assert is_empty_repo(tmp_path) is False

    def test_known_projects_have_markers(self) -> None:
        for project in KNOWN_PROJECTS:
            assert project.markers is not None
            assert len(project.markers) > 0


# ── Tools tests ────────────────────────────────────────────────


class TestToolPolicy:
    def test_git_dir_denied(self) -> None:
        assert _is_path_allowed(".git/config", ToolPolicy(workspace=Path("."))) is False

    def test_workflow_denied_by_default(self) -> None:
        assert (
            _is_path_allowed(
                ".github/workflows/ci.yml",
                ToolPolicy(workspace=Path(".")),
            )
            is False
        )

    def test_workflow_allowed_when_enabled(self) -> None:
        assert (
            _is_path_allowed(
                ".github/workflows/ci.yml",
                ToolPolicy(workspace=Path("."), allow_workflow_changes=True),
            )
            is True
        )

    def test_lock_files_denied(self) -> None:
        policy = ToolPolicy(workspace=Path("."))
        assert _is_path_allowed("poetry.lock", policy) is False
        assert _is_path_allowed("yarn.lock", policy) is False

    def test_normal_files_allowed(self) -> None:
        policy = ToolPolicy(workspace=Path("."))
        assert _is_path_allowed("src/main.py", policy) is True


class TestToolOperations:
    def test_list_dir(self, tmp_path: Path) -> None:
        (tmp_path / "file.txt").touch()
        (tmp_path / "subdir").mkdir()
        policy = ToolPolicy(workspace=tmp_path)
        result = tool_list_dir(".", policy)
        assert "entries" in result
        names = [e["name"] for e in result["entries"]]
        assert "file.txt" in names
        assert "subdir" in names

    def test_list_dir_denied_path(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        policy = ToolPolicy(workspace=tmp_path)
        result = tool_list_dir(".git", policy)
        assert "error" in result

    def test_read_file(self, tmp_path: Path) -> None:
        (tmp_path / "test.py").write_text("hello = 1")
        policy = ToolPolicy(workspace=tmp_path)
        result = tool_read_file("test.py", policy)
        assert result["content"] == "hello = 1"

    def test_read_nonexistent_file(self, tmp_path: Path) -> None:
        policy = ToolPolicy(workspace=tmp_path)
        result = tool_read_file("missing.py", policy)
        assert "error" in result

    def test_write_file(self, tmp_path: Path) -> None:
        policy = ToolPolicy(workspace=tmp_path)
        result = tool_write_file("new.py", "x = 1", policy)
        assert result["success"] is True
        assert (tmp_path / "new.py").read_text() == "x = 1"

    def test_write_creates_subdirs(self, tmp_path: Path) -> None:
        policy = ToolPolicy(workspace=tmp_path)
        result = tool_write_file("a/b/c.py", "deep", policy)
        assert result["success"] is True
        assert (tmp_path / "a" / "b" / "c.py").read_text() == "deep"

    def test_path_traversal_blocked(self, tmp_path: Path) -> None:
        policy = ToolPolicy(workspace=tmp_path)
        result = tool_read_file("../../etc/passwd", policy)
        assert "error" in result

    def test_search_code(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text("def hello():\n    pass")
        policy = ToolPolicy(workspace=tmp_path)
        result = tool_search_code("hello", policy)
        assert "matches" in result


class TestApplyEdit:
    def test_apply_edit_success(self, tmp_path: Path) -> None:
        f = tmp_path / "edit.py"
        f.write_text("old_value = 1")
        policy = ToolPolicy(workspace=tmp_path)
        result = _handle_apply_edit("edit.py", "old_value", "new_value", policy)
        assert result["success"] is True
        assert f.read_text() == "new_value = 1"

    def test_apply_edit_not_found(self, tmp_path: Path) -> None:
        f = tmp_path / "edit.py"
        f.write_text("something else")
        policy = ToolPolicy(workspace=tmp_path)
        result = _handle_apply_edit("edit.py", "nonexistent", "new", policy)
        assert "error" in result

    def test_apply_edit_denied_path(self, tmp_path: Path) -> None:
        policy = ToolPolicy(workspace=tmp_path)
        result = _handle_apply_edit("../../etc/hosts", "a", "b", policy)
        assert "error" in result


# ── Agent loop tests ───────────────────────────────────────────


class TestAgentLoop:
    def _mock_client(self, responses: list[dict[str, Any]]) -> MagicMock:
        client = MagicMock()
        client.converse = MagicMock(side_effect=responses)
        return client

    def test_agent_finishes_on_end_turn(self, tmp_path: Path) -> None:
        client = self._mock_client(
            [
                {
                    "output": {
                        "message": {
                            "content": [{"text": "Done implementing the feature."}],
                        }
                    },
                    "stopReason": "end_turn",
                }
            ]
        )
        policy = ToolPolicy(workspace=tmp_path)
        result = run_agent_loop(client, "system", "implement feature", policy)
        assert result.finished is True
        assert "Done" in result.summary

    def test_agent_finishes_on_tool(self, tmp_path: Path) -> None:
        client = self._mock_client(
            [
                {
                    "output": {
                        "message": {
                            "content": [
                                {
                                    "toolUse": {
                                        "toolUseId": "t1",
                                        "name": "finish",
                                        "input": {"summary": "All done"},
                                    }
                                }
                            ],
                        }
                    },
                    "stopReason": "tool_use",
                }
            ]
        )
        policy = ToolPolicy(workspace=tmp_path)
        result = run_agent_loop(client, "system", "implement feature", policy)
        assert result.finished is True
        assert result.summary == "All done"

    def test_agent_budget_exhausted(self, tmp_path: Path) -> None:
        list_response = {
            "output": {
                "message": {
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "t1",
                                "name": "list_dir",
                                "input": {"path": "."},
                            }
                        }
                    ],
                }
            },
            "stopReason": "tool_use",
        }
        client = self._mock_client([list_response] * 10)
        policy = ToolPolicy(workspace=tmp_path)
        result = run_agent_loop(client, "system", "do stuff", policy, max_tool_calls=3)
        assert result.finished is False
        assert "Budget" in result.summary

    def test_agent_handles_llm_error(self, tmp_path: Path) -> None:
        from ai_hub.errors import FailureReason, LLMError

        client = MagicMock()
        client.converse = MagicMock(
            side_effect=LLMError(FailureReason.MODEL_UNAVAILABLE, "Model down")
        )
        policy = ToolPolicy(workspace=tmp_path)
        result = run_agent_loop(client, "system", "implement", policy)
        assert result.finished is False
        assert "Model down" in result.summary

    def test_agent_unknown_tool_limit(self, tmp_path: Path) -> None:
        bad_tool = {
            "output": {
                "message": {
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "t1",
                                "name": "nonexistent_tool",
                                "input": {},
                            }
                        }
                    ],
                }
            },
            "stopReason": "tool_use",
        }
        client = self._mock_client([bad_tool] * 5)
        policy = ToolPolicy(workspace=tmp_path)
        result = run_agent_loop(client, "system", "do stuff", policy)
        assert result.finished is False
        assert "unknown" in result.summary.lower()

    def test_agent_write_tracks_files(self, tmp_path: Path) -> None:
        responses = [
            {
                "output": {
                    "message": {
                        "content": [
                            {
                                "toolUse": {
                                    "toolUseId": "t1",
                                    "name": "write_file",
                                    "input": {"path": "src/fix.py", "content": "fixed = True"},
                                }
                            }
                        ],
                    }
                },
                "stopReason": "tool_use",
            },
            {
                "output": {
                    "message": {
                        "content": [{"text": "Done."}],
                    }
                },
                "stopReason": "end_turn",
            },
        ]
        client = self._mock_client(responses)
        policy = ToolPolicy(workspace=tmp_path)
        result = run_agent_loop(client, "system", "fix bug", policy)
        assert result.finished is True
        assert "src/fix.py" in result.files_changed


# ── Tester tests ───────────────────────────────────────────────


class TestTester:
    def test_run_tests_success(self, tmp_path: Path) -> None:
        with patch("ai_hub.devpilot.tester.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="OK", stderr=""
            )
            result = run_tests(tmp_path, "pytest")
        assert result.status == CheckStatus.SUCCESS

    def test_run_tests_failure(self, tmp_path: Path) -> None:
        with patch("ai_hub.devpilot.tester.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="FAILED", stderr="error"
            )
            result = run_tests(tmp_path, "pytest")
        assert result.status == CheckStatus.FAILED

    def test_run_tests_timeout(self, tmp_path: Path) -> None:
        with patch("ai_hub.devpilot.tester.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="pytest", timeout=10)
            result = run_tests(tmp_path, "pytest")
        assert result.status == CheckStatus.ERROR

    def test_run_tests_missing_command(self, tmp_path: Path) -> None:
        with patch("ai_hub.devpilot.tester.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("not found")
            result = run_tests(tmp_path, "nonexistent")
        assert result.status == CheckStatus.ERROR

    def test_repair_loop_success_first_try(self, tmp_path: Path) -> None:
        with patch("ai_hub.devpilot.tester.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="OK", stderr=""
            )
            result = run_test_repair_loop(tmp_path, "pytest")
        assert result.final_status == CheckStatus.SUCCESS
        assert result.attempts == 1

    def test_repair_loop_exhausted(self, tmp_path: Path) -> None:
        call_count = 0

        def side_effect(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 0:
                return subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=f"hash-{call_count}", stderr=""
                )
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="FAIL", stderr="")

        with patch("ai_hub.devpilot.tester.subprocess.run", side_effect=side_effect):
            result = run_test_repair_loop(tmp_path, "pytest", max_attempts=3)
        assert result.attempts <= 3


# ── Git ops tests ──────────────────────────────────────────────


class TestGitOps:
    def test_make_branch_name(self) -> None:
        name = make_branch_name(42, "Fix login bug!")
        assert name == "devpilot/issue-42-fix-login-bug"

    def test_make_branch_name_with_execution(self) -> None:
        name = make_branch_name(42, "Fix login bug!", "abcdef1234567890")
        assert name == "devpilot/issue-42-fix-login-bug-abcdef12"

    def test_make_branch_name_long_title(self) -> None:
        title = "a" * 100
        name = make_branch_name(1, title)
        assert len(name) < 80

    def test_has_changes(self, tmp_path: Path) -> None:
        with patch("ai_hub.devpilot.git_ops._run_git") as mock:
            mock.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="M file.py\n", stderr=""
            )
            assert has_changes(tmp_path) is True

    def test_no_changes(self, tmp_path: Path) -> None:
        with patch("ai_hub.devpilot.git_ops._run_git") as mock:
            mock.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            assert has_changes(tmp_path) is False

    def test_stage_and_commit_no_changes(self, tmp_path: Path) -> None:
        with patch("ai_hub.devpilot.git_ops.has_changes", return_value=False):
            result = stage_and_commit(tmp_path, "msg")
            assert result is None


# ── PR tests ───────────────────────────────────────────────────


class TestPR:
    def test_build_pr_body_minimal(self) -> None:
        body = build_pr_body(42, "Fixed the login bug")
        assert "## Summary" in body
        assert "Fixed the login bug" in body
        assert "Closes #42" in body

    def test_build_pr_body_full(self) -> None:
        body = build_pr_body(
            42,
            "Fixed the bug",
            plan="1. Find bug\n2. Fix it",
            test_evidence="All tests pass",
            limitations=["Only tested on Chrome"],
        )
        assert "## Plan" in body
        assert "## Test Evidence" in body
        assert "## Limitations" in body
        assert "Only tested on Chrome" in body

    def test_build_pr_payload(self) -> None:
        payload = build_pr_payload(
            "Fix: login bug",
            "body",
            "devpilot/issue-42",
            "main",
        )
        assert payload["title"] == "Fix: login bug"
        assert payload["head"] == "devpilot/issue-42"
        assert payload["base"] == "main"


# ── Guardrails tests ──────────────────────────────────────────


class TestGuardrails:
    def test_diff_size_within_limits(self) -> None:
        result = check_diff_size(["a.py", "b.py"], 100)
        assert result.passed is True

    def test_diff_size_too_many_files(self) -> None:
        files = [f"file_{i}.py" for i in range(25)]
        result = check_diff_size(files, 100)
        assert result.passed is False
        assert any("files" in v.lower() for v in result.violations)

    def test_diff_size_too_many_lines(self) -> None:
        result = check_diff_size(["a.py"], 1000)
        assert result.passed is False

    def test_forbidden_changes_codeowners(self) -> None:
        result = check_forbidden_changes(["CODEOWNERS"])
        assert result.passed is False

    def test_forbidden_changes_workflow(self) -> None:
        result = check_forbidden_changes([".github/workflows/ci.yml"])
        assert result.passed is False

    def test_forbidden_changes_workflow_allowed(self) -> None:
        result = check_forbidden_changes(
            [".github/workflows/ci.yml"],
            allow_workflow_changes=True,
        )
        assert result.passed is True

    def test_unrelated_changes_ok(self) -> None:
        result = check_unrelated_changes(["a.py", "b.py"], ["a.py", "b.py", "c.py"])
        assert result.passed is True

    def test_unrelated_changes_too_many(self) -> None:
        result = check_unrelated_changes(
            ["x.py", "y.py", "z.py", "w.py"],
            ["a.py"],
        )
        assert result.passed is False

    def test_run_all_guardrails_pass(self) -> None:
        result = run_all_guardrails(["src/main.py"], 50, ["src/main.py"])
        assert result.passed is True
        assert result.violations == []

    def test_run_all_guardrails_multiple_violations(self) -> None:
        files = [f"file_{i}.py" for i in range(25)]
        files.append("CODEOWNERS")
        result = run_all_guardrails(files, 1000, ["file_0.py"])
        assert result.passed is False
        assert len(result.violations) >= 2
