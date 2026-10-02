"""Tests for the test runner, env scrubbing, secret scan, workspace and git operations.

These use real subprocesses and real git repositories (no mocks of subprocess).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_hub.devpilot import git_ops
from ai_hub.devpilot.secret_scan import scan_builtin, scan_diff_text
from ai_hub.devpilot.tester import run_test_repair_loop, run_tests
from ai_hub.devpilot.workspace import (
    InstallResult,
    ProjectType,
    init_submodules,
    install_dependencies,
)
from ai_hub.errors import GitError
from ai_hub.models import CheckStatus
from ai_hub.safety.env import scrubbed_env

PY = Path(sys.executable).as_posix()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-b", "main")
    (path / "a.txt").write_text("a\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "base")
    return path


# ── env scrubbing ────────────────────────────────────────────


class TestScrubbedEnv:
    def test_removes_credentials(self) -> None:
        env = scrubbed_env(
            {
                "PATH": "/bin",
                "HOME": "/home/x",
                "GITHUB_TOKEN": "t",
                "DEVPILOT_BOT_TOKEN": "t",
                "AWS_ACCESS_KEY_ID": "a",
                "AWS_SESSION_TOKEN": "s",
                "DASHBOARD_TOKEN": "d",
                "MY_API_KEY": "k",
                "DB_PASSWORD": "p",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraheader",
                "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic xxx",
            }
        )
        assert env == {"PATH": "/bin", "HOME": "/home/x"}

    def test_keeps_ci_markers(self) -> None:
        assert scrubbed_env({"CI": "true", "GITHUB_ACTIONS": "true"}) == {
            "CI": "true",
            "GITHUB_ACTIONS": "true",
        }


# ── test runner ──────────────────────────────────────────────


class TestRunTests:
    def test_command_with_quoted_arguments(self, repo: Path) -> None:
        result = run_tests(repo, f'{PY} -c "import sys; sys.exit(0 if len(sys.argv) == 1 else 1)"')
        assert result.status == CheckStatus.SUCCESS

    def test_failure_status_and_output(self, repo: Path) -> None:
        result = run_tests(repo, f"{PY} -c \"print('boom'); raise SystemExit(3)\"")
        assert result.status == CheckStatus.FAILED
        assert "boom" in result.output
        assert result.returncode == 3

    def test_secrets_are_not_inherited(self, repo: Path) -> None:
        script = "import os; raise SystemExit(0 if 'DEVPILOT_BOT_TOKEN' not in os.environ else 7)"
        with patch.dict(os.environ, {"DEVPILOT_BOT_TOKEN": "supersecret"}):
            result = run_tests(repo, f'{PY} -c "{script}"')
        assert result.status == CheckStatus.SUCCESS

    def test_secrets_in_output_are_redacted(self, repo: Path) -> None:
        token = "ghp_" + "a" * 36
        result = run_tests(repo, f"{PY} -c \"print('{token}'); raise SystemExit(1)\"")
        assert token not in result.output

    def test_timeout(self, repo: Path) -> None:
        result = run_tests(repo, f'{PY} -c "import time; time.sleep(30)"', timeout_seconds=1)
        assert result.status == CheckStatus.ERROR
        assert "timed out" in result.output

    def test_missing_command(self, repo: Path) -> None:
        assert run_tests(repo, "definitely-not-a-real-command").status == CheckStatus.ERROR

    def test_invalid_quoting(self, repo: Path) -> None:
        assert run_tests(repo, 'python -c "unterminated').status == CheckStatus.ERROR

    def test_no_tests_collected_is_skipped_not_failed(self, repo: Path) -> None:
        # pytest exits with status 5 when it collects nothing.
        result = run_tests(repo, f"{PY} -m pytest -p no:cacheprovider")
        assert result.status == CheckStatus.SKIPPED
        assert result.no_tests is True

    def test_diff_hash_tracks_working_tree(self, repo: Path) -> None:
        first = run_tests(repo, f'{PY} -c "pass"').diff_hash
        (repo / "new.py").write_text("x = 1\n")
        second = run_tests(repo, f'{PY} -c "pass"').diff_hash
        again = run_tests(repo, f'{PY} -c "pass"').diff_hash
        assert first != second
        assert second == again


class TestRepairLoop:
    def _flip_script(self, repo: Path) -> str:
        """Command that fails until marker.txt contains 'fixed'."""
        script = "import sys; sys.exit(0 if open('marker.txt').read().strip() == 'fixed' else 1)"
        return f'{PY} -c "{script}"'

    def test_repair_callback_fixes_the_code(self, repo: Path) -> None:
        (repo / "marker.txt").write_text("broken")
        calls: list[int] = []

        def repair(run, attempt):  # type: ignore[no-untyped-def]
            calls.append(attempt)
            (repo / "marker.txt").write_text("fixed")
            return True

        result = run_test_repair_loop(
            repo, self._flip_script(repo), max_attempts=3, repair=repair, flaky_reruns=0
        )
        assert result.final_status == CheckStatus.SUCCESS
        assert calls == [1]
        assert result.attempts == 2

    def test_failure_output_is_given_to_the_repair_callback(self, repo: Path) -> None:
        (repo / "marker.txt").write_text("broken")
        seen: list[str] = []

        def repair(run, attempt):  # type: ignore[no-untyped-def]
            seen.append(run.output)
            return False

        result = run_test_repair_loop(
            repo,
            f"{PY} -c \"print('assertion detail'); raise SystemExit(1)\"",
            repair=repair,
            flaky_reruns=0,
        )
        assert result.final_status == CheckStatus.FAILED
        assert result.stop_reason == "repair_failed"
        assert "assertion detail" in seen[0]

    def test_same_failing_state_stops_the_loop(self, repo: Path) -> None:
        (repo / "marker.txt").write_text("broken")
        # The "repair" changes nothing, so the working tree hash repeats.
        result = run_test_repair_loop(
            repo,
            self._flip_script(repo),
            max_attempts=5,
            repair=lambda run, attempt: True,
            flaky_reruns=0,
        )
        assert result.final_status == CheckStatus.FAILED
        assert result.stop_reason == "repeated_failed_state"
        assert result.attempts == 2

    def test_attempts_are_capped(self, repo: Path) -> None:
        (repo / "marker.txt").write_text("broken")
        counter = {"n": 0}

        def repair(run, attempt):  # type: ignore[no-untyped-def]
            counter["n"] += 1
            (repo / "marker.txt").write_text(f"still broken {counter['n']}")
            return True

        result = run_test_repair_loop(
            repo, self._flip_script(repo), max_attempts=3, repair=repair, flaky_reruns=0
        )
        assert result.final_status == CheckStatus.FAILED
        assert result.stop_reason == "attempts_exhausted"
        assert result.attempts == 3
        assert counter["n"] == 2

    def test_flaky_pass_is_reported_as_flaky(self, repo: Path) -> None:
        counter = repo / "count.txt"
        counter.write_text("0")
        script = (
            "import sys; p='count.txt'; n=int(open(p).read())+1; open(p,'w').write(str(n)); "
            "sys.exit(0 if n >= 2 else 1)"
        )
        result = run_test_repair_loop(repo, f'{PY} -c "{script}"', flaky_reruns=2)
        assert result.final_status == CheckStatus.SUCCESS
        assert result.is_flaky is True
        assert "Inconsistent" in (result.flaky_evidence or "")

    def test_timeout_is_not_retried(self, repo: Path) -> None:
        result = run_test_repair_loop(
            repo, f'{PY} -c "import time; time.sleep(30)"', timeout_seconds=1, repair=None
        )
        assert result.final_status == CheckStatus.ERROR
        assert result.attempts == 1


# ── secret scan ──────────────────────────────────────────────


def _diff(line: str, path: str = "src/config.py") -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n+{line}\n"


class TestBuiltinSecretScan:
    @pytest.mark.parametrize(
        ("line", "label"),
        [
            ("KEY = 'AKIAABCDEFGHIJKLMNOP'", "AWS access key ID"),
            ("t = 'ghp_" + "b" * 36 + "'", "GitHub token"),
            ("-----BEGIN RSA PRIVATE KEY-----", "Private key block"),
            ("password = 'correct-horse-battery-staple'", "Hard-coded credential"),
            ("slack = 'xoxb-1234567890-abcdef'", "Slack token"),
        ],
    )
    def test_detects(self, line: str, label: str) -> None:
        findings = scan_builtin(_diff(line))
        assert findings == [f"{label} in src/config.py"]

    def test_findings_never_contain_the_secret(self) -> None:
        secret = "AKIAABCDEFGHIJKLMNOP"
        assert all(secret not in f for f in scan_builtin(_diff(f"k='{secret}'")))

    def test_ignores_removed_and_context_lines(self) -> None:
        diff = "+++ b/a.py\n-KEY='AKIAABCDEFGHIJKLMNOP'\n KEY2='AKIAABCDEFGHIJKLMNOP'\n"
        assert scan_builtin(diff) == []

    def test_ignores_placeholders(self) -> None:
        assert scan_builtin(_diff("password = 'your-password-here-please'")) == []
        assert scan_builtin(_diff('api_key = "${API_KEY_FROM_ENV_VAR}"')) == []

    def test_clean_code(self) -> None:
        assert scan_builtin(_diff("def add(a, b): return a + b")) == []


class TestSecretScanFallback:
    def test_missing_gitleaks_does_not_mean_clean(self, repo: Path) -> None:
        with patch("ai_hub.devpilot.secret_scan.subprocess.run", side_effect=FileNotFoundError):
            result = scan_diff_text(_diff("k = 'AKIAABCDEFGHIJKLMNOP'"), repo)
        assert result.clean is False
        assert result.gitleaks_ran is False

    def test_missing_gitleaks_with_clean_diff(self, repo: Path) -> None:
        with patch("ai_hub.devpilot.secret_scan.subprocess.run", side_effect=FileNotFoundError):
            result = scan_diff_text(_diff("x = 1"), repo)
        assert result.clean is True
        assert result.gitleaks_ran is False

    def test_gitleaks_required_but_missing_fails(self, repo: Path) -> None:
        with patch("ai_hub.devpilot.secret_scan.subprocess.run", side_effect=FileNotFoundError):
            result = scan_diff_text(_diff("x = 1"), repo, require_gitleaks=True)
        assert result.clean is False


# ── workspace ────────────────────────────────────────────────


class TestInstallDependencies:
    def test_quoted_install_command_is_split_correctly(self, repo: Path) -> None:
        project = ProjectType(
            name="t", language="python", install_command=f"{PY} -c \"print('a b')\""
        )
        result = install_dependencies(repo, project)
        assert result.ok is True

    def test_fallback_is_used_when_primary_fails(self, repo: Path) -> None:
        project = ProjectType(
            name="t",
            language="python",
            install_command=f'{PY} -c "raise SystemExit(1)"',
            install_fallback=f'{PY} -c "pass"',
        )
        result = install_dependencies(repo, project)
        assert result.ok is True
        assert len(result.commands_run) == 2

    def test_failure_is_reported(self, repo: Path) -> None:
        project = ProjectType(
            name="t",
            language="python",
            install_command=f"{PY} -c \"print('nope'); raise SystemExit(2)\"",
        )
        result = install_dependencies(repo, project)
        assert result == InstallResult(False, result.output, result.commands_run)
        assert "nope" in result.output

    def test_missing_installer_is_a_failure(self, repo: Path) -> None:
        project = ProjectType(name="t", language="x", install_command="no-such-installer --go")
        assert install_dependencies(repo, project).ok is False

    def test_install_does_not_see_credentials(self, repo: Path) -> None:
        script = "import os; raise SystemExit(7 if 'AWS_SECRET_ACCESS_KEY' in os.environ else 0)"
        project = ProjectType(name="t", language="x", install_command=f'{PY} -c "{script}"')
        with patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "s"}):
            assert install_dependencies(repo, project).ok is True


class TestSubmodules:
    def test_no_gitmodules_is_ok(self, repo: Path) -> None:
        assert init_submodules(repo) is True

    def test_failed_submodule_init_is_reported(self, repo: Path) -> None:
        (repo / ".gitmodules").write_text(
            '[submodule "x"]\n\tpath = x\n\turl = file:///definitely/not/here\n'
        )
        # Register a gitlink so git really tries to fetch the submodule.
        head = _git(repo, "rev-parse", "HEAD").strip()
        _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},x")
        assert init_submodules(repo) is False


# ── git operations ───────────────────────────────────────────


class TestGitOps:
    def test_staged_changes_summary(self, repo: Path) -> None:
        (repo / "a.txt").write_text("a\nb\nc\n")
        (repo / "new.py").write_text("x = 1\n")
        git_ops.stage_all(repo)
        changes = git_ops.staged_changes(repo)
        assert sorted(changes.files) == ["a.txt", "new.py"]
        assert changes.lines == 3
        assert changes.deleted == []

    def test_deleted_files_are_reported(self, repo: Path) -> None:
        (repo / "a.txt").unlink()
        git_ops.stage_all(repo)
        assert git_ops.staged_changes(repo).deleted == ["a.txt"]

    def test_binary_files_are_flagged(self, repo: Path) -> None:
        (repo / "img.bin").write_bytes(bytes(range(256)) * 4)
        git_ops.stage_all(repo)
        assert git_ops.staged_changes(repo).binary == ["img.bin"]

    def test_commit_has_trailer_and_bot_identity(self, repo: Path) -> None:
        (repo / "b.txt").write_text("b")
        sha = git_ops.stage_and_commit(
            repo, "Add b", execution_id="exec-1", identity=("devpilot-bot", "bot@example.com")
        )
        assert sha == git_ops.get_current_sha(repo)
        body = _git(repo, "log", "-1", "--format=%an <%ae>%n%B")
        assert "devpilot-bot <bot@example.com>" in body
        assert "DevPilot-Execution: exec-1" in body

    def test_commit_hooks_are_disabled(self, repo: Path) -> None:
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        (repo / "b.txt").write_text("b")
        assert git_ops.stage_and_commit(repo, "Add b") is not None

    def test_working_tree_hash_changes_with_content(self, repo: Path) -> None:
        clean = git_ops.working_tree_hash(repo)
        (repo / "a.txt").write_text("changed\n")
        changed = git_ops.working_tree_hash(repo)
        (repo / "a.txt").write_text("changed again\n")
        assert len({clean, changed, git_ops.working_tree_hash(repo)}) == 3

    def test_push_never_forces(self, repo: Path, tmp_path: Path) -> None:
        origin = tmp_path / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
        )
        _git(repo, "remote", "add", "origin", origin.as_posix())
        _git(repo, "push", "origin", "main")

        # Someone else pushes to feature/x first.
        other = tmp_path / "other"
        subprocess.run(
            ["git", "clone", origin.as_posix(), str(other)], check=True, capture_output=True
        )
        _git(other, "checkout", "-b", "feature/x")
        (other / "o.txt").write_text("o")
        _git(other, "add", "-A")
        _git(other, "commit", "-m", "other work")
        _git(other, "push", "origin", "feature/x")

        git_ops.create_branch(repo, "feature/x")
        (repo / "mine.txt").write_text("m")
        git_ops.stage_and_commit(repo, "mine")
        with pytest.raises(GitError) as exc:
            git_ops.push_branch(repo, "feature/x")
        assert exc.value.reason.value == "push_failed"
        # The other developer's commit is untouched.
        assert "other work" in _git(origin, "log", "feature/x", "--format=%s")

    def test_rebase_conflict_returns_false_and_leaves_clean_tree(
        self, repo: Path, tmp_path: Path
    ) -> None:
        origin = tmp_path / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
        )
        _git(repo, "remote", "add", "origin", origin.as_posix())
        _git(repo, "push", "origin", "main")

        git_ops.create_branch(repo, "work")
        (repo / "a.txt").write_text("mine\n")
        git_ops.stage_and_commit(repo, "mine")

        other = tmp_path / "other"
        subprocess.run(
            ["git", "clone", origin.as_posix(), str(other)], check=True, capture_output=True
        )
        (other / "a.txt").write_text("theirs\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-m", "theirs")
        _git(other, "push", "origin", "main")

        assert git_ops.rebase_on_base(repo, "main") is False
        assert git_ops.has_changes(repo) is False
        assert (repo / "a.txt").read_text() == "mine\n"

    def test_rebase_success(self, repo: Path, tmp_path: Path) -> None:
        origin = tmp_path / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
        )
        _git(repo, "remote", "add", "origin", origin.as_posix())
        _git(repo, "push", "origin", "main")
        git_ops.create_branch(repo, "work")
        (repo / "mine.txt").write_text("m")
        git_ops.stage_and_commit(repo, "mine")

        other = tmp_path / "other"
        subprocess.run(
            ["git", "clone", origin.as_posix(), str(other)], check=True, capture_output=True
        )
        (other / "theirs.txt").write_text("t")
        _git(other, "add", "-A")
        _git(other, "commit", "-m", "theirs")
        _git(other, "push", "origin", "main")

        assert git_ops.rebase_on_base(repo, "main") is True
        assert (repo / "theirs.txt").exists()
        assert (repo / "mine.txt").exists()

    def test_exclude_generated_files(self, repo: Path) -> None:
        (repo / "build_output").mkdir()
        (repo / "build_output" / "x.o").write_text("obj")
        (repo / "__pycache__").mkdir()
        (repo / "__pycache__" / "m.pyc").write_bytes(b"\x00\x01")
        git_ops.exclude_generated_files(repo)
        # A file the agent creates afterwards is still picked up.
        (repo / "feature.py").write_text("x = 1\n")
        git_ops.stage_all(repo)
        assert git_ops.staged_changes(repo).files == ["feature.py"]

    def test_make_branch_name_with_empty_slug(self) -> None:
        assert git_ops.make_branch_name(5, "!!!") == "devpilot/issue-5"
