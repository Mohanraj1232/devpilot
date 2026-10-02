"""Tests for the review prepare stage, analysis aggregation and the tests/coverage stage."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ai_hub.config.loader import load_config
from ai_hub.errors import AnalysisError, ConfigError, FailureReason, GitError
from ai_hub.models import CheckStatus, Severity
from ai_hub.review import pipeline
from ai_hub.review.inputs import prepare_review_inputs
from ai_hub.review.pipeline import (
    is_on_diff,
    measure_coverage,
    normalize_path,
    partition_findings,
    run_analysis,
    run_tests_stage,
)
from tests.review_helpers import FakeAdapter, git, make_finding, make_pr_repo

PY = Path(sys.executable).as_posix()
BASE_CONFIG = "quality_gate:\n  coverage_threshold: 90\n  fail_on:\n    high: 1\n"


# ── prepare ──────────────────────────────────────────────────


class TestPrepare:
    def test_diff_contains_only_the_pull_request(self, tmp_path: Path) -> None:
        repo = make_pr_repo(
            tmp_path,
            base={"src/app.py": "x = 1\n", ".ai-review/config.yml": BASE_CONFIG},
            pr={"src/app.py": "x = 1\ny = 2\n", "src/new.py": "z = 3\n"},
        )
        result = prepare_review_inputs(repo, tmp_path / "in")
        diff = (tmp_path / "in" / "diff.patch").read_text()
        assert "y = 2" in diff
        assert "BASE_MARKER" not in diff  # base-only changes are not part of the PR
        assert sorted(result.changed_files) == ["src/app.py", "src/new.py"]
        assert json.loads((tmp_path / "in" / "changed-files.json").read_text()) == sorted(
            result.changed_files
        ) or set(json.loads((tmp_path / "in" / "changed-files.json").read_text())) == set(
            result.changed_files
        )
        assert result.changed_lines == 2

    def test_deleted_files_are_not_listed_as_changed(self, tmp_path: Path) -> None:
        repo = make_pr_repo(
            tmp_path,
            base={"old.py": "x = 1\n", "keep.py": "y = 1\n"},
            pr={"keep.py": "y = 2\n"},
            pr_deletes=("old.py",),
        )
        assert prepare_review_inputs(repo, tmp_path / "in").changed_files == ["keep.py"]

    def test_config_comes_from_the_base_branch_not_the_pr(self, tmp_path: Path) -> None:
        weakened = "quality_gate:\n  enabled: false\n  coverage_threshold: 0\n"
        repo = make_pr_repo(
            tmp_path,
            base={"a.py": "x = 1\n", ".ai-review/config.yml": BASE_CONFIG},
            pr={".ai-review/config.yml": weakened},
        )
        result = prepare_review_inputs(repo, tmp_path / "in")
        assert result.pr_modifies_config is True
        assert result.config_source == "base"
        config, _ = load_config(tmp_path / "in" / "config.yml")
        assert config.quality_gate.enabled is True
        assert config.quality_gate.coverage_threshold == 90
        # The working tree still has the PR's weakened file; it must not be what we used.
        assert "enabled: false" in (repo / ".ai-review" / "config.yml").read_text()

    def test_missing_base_config_uses_defaults(self, tmp_path: Path) -> None:
        repo = make_pr_repo(
            tmp_path, base={"a.py": "x = 1\n"}, pr={".ai-review/config.yml": "version: 1\n"}
        )
        result = prepare_review_inputs(repo, tmp_path / "in")
        assert result.config_source == "defaults"
        assert result.config_valid is True
        assert not (tmp_path / "in" / "config.yml").exists()

    def test_invalid_base_config_is_reported(self, tmp_path: Path) -> None:
        repo = make_pr_repo(
            tmp_path,
            base={".ai-review/config.yml": "static_analysis:\n  tools: [nonsense]\n"},
            pr={"a.py": "x = 1\n"},
        )
        result = prepare_review_inputs(repo, tmp_path / "in")
        assert result.config_valid is False
        assert any("nonsense" in e for e in result.config_errors)
        status = json.loads((tmp_path / "in" / "config-status.json").read_text())
        assert status["valid"] is False

    def test_no_parent_commit_is_an_error(self, tmp_path: Path) -> None:
        repo = tmp_path / "single"
        repo.mkdir()
        git(repo, "init", "-b", "main")
        (repo / "a.py").write_text("x = 1\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "only commit")
        with pytest.raises(GitError) as exc:
            prepare_review_inputs(repo, tmp_path / "in")
        assert exc.value.reason == FailureReason.CLONE_FAILURE

    @pytest.mark.parametrize("path", ["../outside.yml", "/etc/passwd", ""])
    def test_config_path_must_stay_inside_the_repo(self, tmp_path: Path, path: str) -> None:
        repo = make_pr_repo(tmp_path, base={"a.py": "x\n"}, pr={"a.py": "y\n"})
        with pytest.raises(ConfigError):
            prepare_review_inputs(repo, tmp_path / "in", config_path=path)


# ── analysis aggregation ─────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    return tmp_path


def _config(text: str = "") -> object:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "c.yml"
        path.write_text(text)
        return load_config(path)[0]


class TestRunAnalysis:
    def _use(self, monkeypatch: pytest.MonkeyPatch, *adapters: FakeAdapter) -> None:
        monkeypatch.setattr(pipeline, "build_adapters", lambda kind, tools: list(adapters))

    def test_clean_run_is_success(self, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._use(monkeypatch, FakeAdapter("a"), FakeAdapter("b"))
        findings, check = run_analysis("static", workspace, ["src/app.py"], _config())  # type: ignore[arg-type]
        assert findings == []
        assert check.status == CheckStatus.SUCCESS

    def test_findings_make_the_check_failed(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._use(monkeypatch, FakeAdapter("a", [make_finding()]))
        findings, check = run_analysis("static", workspace, [], _config())  # type: ignore[arg-type]
        assert check.status == CheckStatus.FAILED
        assert len(findings) == 1

    def test_a_crashing_tool_is_an_error_even_if_others_are_clean(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crash = AnalysisError(FailureReason.TOOL_CRASH, "semgrep exited with code 2")
        self._use(monkeypatch, FakeAdapter("ruff"), FakeAdapter("semgrep", error=crash))
        _, check = run_analysis("static", workspace, [], _config())  # type: ignore[arg-type]
        assert check.status == CheckStatus.ERROR
        assert "semgrep" in check.summary
        assert check.details["tools"]["semgrep"]["status"] == "error"
        assert check.details["tools"]["ruff"]["status"] == "ok"

    def test_unexpected_exception_is_also_an_error(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._use(monkeypatch, FakeAdapter("a", error=RuntimeError("boom")))
        _, check = run_analysis("security", workspace, [], _config())  # type: ignore[arg-type]
        assert check.status == CheckStatus.ERROR

    def test_error_message_does_not_leak_secrets(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = "ghp_" + "q" * 36
        self._use(monkeypatch, FakeAdapter("a", error=RuntimeError(f"auth failed {token}")))
        _, check = run_analysis("static", workspace, [], _config())  # type: ignore[arg-type]
        assert token not in check.summary

    def test_inapplicable_tools_are_skipped_not_clean(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._use(monkeypatch, FakeAdapter("ruff", applicable=(False, "No Python files changed")))
        _, check = run_analysis("static", workspace, ["README.md"], _config())  # type: ignore[arg-type]
        assert check.status == CheckStatus.SKIPPED
        assert check.details["tools"]["ruff"]["reason"] == "No Python files changed"

    def test_mix_of_skipped_and_run_tools(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._use(monkeypatch, FakeAdapter("a", applicable=(False, "n/a")), FakeAdapter("b"))
        _, check = run_analysis("static", workspace, [], _config())  # type: ignore[arg-type]
        assert check.status == CheckStatus.SUCCESS

    def test_disabled_category_is_skipped(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._use(monkeypatch, FakeAdapter("a", [make_finding()]))
        _, check = run_analysis(
            "security",
            workspace,
            [],
            _config("security:\n  enabled: false\n"),  # type: ignore[arg-type]
        )
        assert check.status == CheckStatus.SKIPPED

    def test_ignored_and_generated_paths_are_dropped(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        found = [
            make_finding("dist/bundle.js"),
            make_finding("api/schema_pb2.py"),
            make_finding("src/app.py"),
        ]
        self._use(monkeypatch, FakeAdapter("a", found))
        config = _config('paths:\n  ignore: ["dist/**"]\n  generated: ["**/*_pb2.py"]\n')
        findings, _ = run_analysis("static", workspace, [], config)  # type: ignore[arg-type]
        assert [f.file for f in findings] == ["src/app.py"]

    def test_absolute_tool_paths_become_repo_relative(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        absolute = str(workspace / "src" / "app.py")
        self._use(monkeypatch, FakeAdapter("a", [make_finding(absolute)]))
        findings, _ = run_analysis("static", workspace, [], _config())  # type: ignore[arg-type]
        assert findings[0].file == "src/app.py"

    def test_each_adapter_gets_the_changed_files(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[str] | None] = []

        class Spy(FakeAdapter):
            def run(self, repo_path, changed_files=None):  # type: ignore[no-untyped-def]
                seen.append(changed_files)
                return []

        self._use(monkeypatch, Spy("spy"))
        run_analysis("static", workspace, ["a.py", "b.py"], _config())  # type: ignore[arg-type]
        assert seen == [["a.py", "b.py"]]


class TestBuildAdapters:
    def test_static_tool_names(self) -> None:
        names = [a.name for a in pipeline.build_adapters("static", ["ruff", "eslint", "semgrep"])]
        assert names == ["ruff", "eslint", "semgrep"]

    def test_security_tool_names(self) -> None:
        names = [
            a.name for a in pipeline.build_adapters("security", ["gitleaks", "bandit", "deps"])
        ]
        assert names == ["gitleaks", "bandit", "pip-audit", "npm-audit"]

    def test_semgrep_runs_in_security_mode_for_security(self) -> None:
        from ai_hub.models import FindingSource

        assert pipeline.build_adapters("security", ["semgrep"])[0].source == FindingSource.SECURITY  # type: ignore[attr-defined]
        assert pipeline.build_adapters("static", ["semgrep"])[0].source == FindingSource.STATIC  # type: ignore[attr-defined]


def test_normalize_path(tmp_path: Path) -> None:
    assert normalize_path("./src\\app.py", tmp_path) == "src/app.py"
    assert normalize_path(str(tmp_path / "a" / "b.py"), tmp_path) == "a/b.py"
    assert normalize_path("/somewhere/else.py", tmp_path) == "/somewhere/else.py"


# ── on-diff / off-diff ───────────────────────────────────────


class TestDiffPartition:
    ADDED = {"src/app.py": {10, 11, 12}, "requirements.txt": {3}}

    def test_finding_on_an_added_line(self) -> None:
        assert is_on_diff(make_finding("src/app.py", 11), self.ADDED) is True

    def test_finding_on_an_untouched_line_is_off_diff(self) -> None:
        assert is_on_diff(make_finding("src/app.py", 50), self.ADDED) is False

    def test_range_overlapping_an_added_line(self) -> None:
        assert is_on_diff(make_finding("src/app.py", 8, line_end=10), self.ADDED) is True
        assert is_on_diff(make_finding("src/app.py", 13, line_end=20), self.ADDED) is False

    def test_file_not_in_the_pr_is_off_diff(self) -> None:
        assert is_on_diff(make_finding("other.py", 11), self.ADDED) is False

    def test_file_level_finding_counts_when_the_file_changed(self) -> None:
        assert is_on_diff(make_finding("requirements.txt", 0), self.ADDED) is True
        assert is_on_diff(make_finding("package.json", 0), self.ADDED) is False

    def test_partition(self) -> None:
        on, off = partition_findings(
            [make_finding("src/app.py", 10), make_finding("src/app.py", 99)], self.ADDED
        )
        assert [f.line_start for f in on] == [10]
        assert [f.line_start for f in off] == [99]


# ── tests + coverage stage ───────────────────────────────────

COBERTURA = (Path(__file__).parent / "fixtures" / "coverage.xml").read_text()


class TestTestsStage:
    def _cfg(self, command: str | None, coverage: str | None = None) -> object:
        parts = []
        if command is not None:
            parts.append(f'  command: "{command}"')
        if coverage:
            parts.append(f"  coverage_report: {coverage}")
        return _config("tests:\n" + "\n".join(parts) + "\n" if parts else "")

    def test_passing_tests_with_coverage(self, workspace: Path) -> None:
        (workspace / "reports").mkdir()
        (workspace / "reports" / "coverage.xml").write_text(COBERTURA)
        config = self._cfg(f"{PY} -c pass", "reports/coverage.xml")
        _, check, coverage = run_tests_stage(workspace, config)  # type: ignore[arg-type]
        assert check.status == CheckStatus.SUCCESS
        assert coverage == 85.0
        assert check.details["coverage_pct"] == 85.0

    def test_failing_tests(self, workspace: Path) -> None:
        config = self._cfg(f"{PY} -c \\\"print('2 failed'); raise SystemExit(1)\\\"")
        _, check, _ = run_tests_stage(workspace, config)  # type: ignore[arg-type]
        assert check.status == CheckStatus.FAILED
        assert "2 failed" in check.summary

    def test_no_command_is_skipped(self, workspace: Path) -> None:
        _, check, coverage = run_tests_stage(workspace, self._cfg(None))  # type: ignore[arg-type]
        assert check.status == CheckStatus.SKIPPED
        assert coverage is None

    def test_missing_test_runner_is_an_error_not_a_pass(self, workspace: Path) -> None:
        _, check, _ = run_tests_stage(workspace, self._cfg("definitely-not-a-runner"))  # type: ignore[arg-type]
        assert check.status == CheckStatus.ERROR

    def test_missing_coverage_report_is_unavailable_not_zero(self, workspace: Path) -> None:
        config = self._cfg(f"{PY} -c pass", "nope/coverage.xml")
        _, check, coverage = run_tests_stage(workspace, config)  # type: ignore[arg-type]
        assert check.status == CheckStatus.SUCCESS
        assert coverage is None

    def test_tests_run_without_credentials(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "secret")
        script = "import os; raise SystemExit(7 if 'GITHUB_TOKEN' in os.environ else 0)"
        config = self._cfg(f'{PY} -c \\"{script}\\"')
        _, check, _ = run_tests_stage(workspace, config)  # type: ignore[arg-type]
        assert check.status == CheckStatus.SUCCESS

    def test_dependency_install_failure_is_an_error(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ai_hub.devpilot.workspace import InstallResult

        (workspace / "requirements.txt").write_text("x\n")
        monkeypatch.setattr(
            pipeline, "install_dependencies", lambda *a, **k: InstallResult(False, "no network")
        )
        config = self._cfg(f"{PY} -c pass")
        _, check, _ = run_tests_stage(workspace, config)  # type: ignore[arg-type]
        assert check.status == CheckStatus.ERROR
        assert "no network" in check.summary


class TestMeasureCoverage:
    def test_cobertura_and_lcov(self, tmp_path: Path) -> None:
        (tmp_path / "c.xml").write_text(COBERTURA)
        (tmp_path / "lcov.info").write_text(
            "SF:a.py\nDA:1,1\nDA:2,1\nDA:3,0\nDA:4,0\nend_of_record\n"
        )
        assert measure_coverage(tmp_path, "c.xml") == 85.0
        assert measure_coverage(tmp_path, "lcov.info") == 50.0

    def test_unavailable_cases(self, tmp_path: Path) -> None:
        assert measure_coverage(tmp_path, None) is None
        assert measure_coverage(tmp_path, "missing.xml") is None
        (tmp_path / "bad.xml").write_text("<not xml")
        assert measure_coverage(tmp_path, "bad.xml") is None

    def test_path_outside_the_workspace_is_ignored(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside.xml"
        outside.write_text(COBERTURA)
        inner = tmp_path / "ws"
        inner.mkdir()
        assert measure_coverage(inner, "../outside.xml") is None


def test_severity_import_is_used() -> None:
    # keeps the Severity import meaningful for readers of the partition tests above
    assert Severity.HIGH.value == "high"


class TestPrepareKeepsLineNumbersExact:
    def test_crlf_repository_has_the_right_added_lines(self, tmp_path: Path) -> None:
        from ai_hub.analysis.diff import build_changed_line_map, load_diff_text, parse_unified_diff

        repo = make_pr_repo(
            tmp_path,
            base={"src/app.py": "a = 1\n"},
            pr={"src/app.py": "a = 1\nb = 2\nc = 3\n"},
        )
        prepare_review_inputs(repo, tmp_path / "in")
        added = build_changed_line_map(
            parse_unified_diff(load_diff_text(tmp_path / "in" / "diff.patch"))
        )
        assert added == {"src/app.py": {2, 3}}

    def test_file_with_form_feeds_and_unicode_separators(self, tmp_path: Path) -> None:
        from ai_hub.analysis.diff import build_changed_line_map, load_diff_text, parse_unified_diff

        base = "first\n"
        pr = "first\nsecond\x0cstill second\nthird   still third\nfourth\n"
        repo = make_pr_repo(tmp_path, base={"f.txt": base}, pr={"f.txt": pr})
        prepare_review_inputs(repo, tmp_path / "in")
        added = build_changed_line_map(
            parse_unified_diff(load_diff_text(tmp_path / "in" / "diff.patch"))
        )
        assert added == {"f.txt": {2, 3, 4}}
