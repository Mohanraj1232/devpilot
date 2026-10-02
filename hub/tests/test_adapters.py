"""Tests for analysis tool adapters — parsing known fixture outputs."""

from pathlib import Path
from unittest.mock import patch

import pytest

from ai_hub.analysis.base import ToolAdapter, run_tool_subprocess
from ai_hub.analysis.security.bandit import BanditAdapter
from ai_hub.analysis.security.deps import NpmAuditAdapter, PipAuditAdapter
from ai_hub.analysis.security.gitleaks import GitleaksAdapter
from ai_hub.analysis.static.eslint import ESLintAdapter
from ai_hub.analysis.static.ruff import RuffAdapter
from ai_hub.analysis.static.semgrep import SemgrepAdapter
from ai_hub.errors import AnalysisError
from ai_hub.models import FindingCategory, FindingSource, Severity

FIXTURES = Path(__file__).parent / "fixtures"


def _mock_subprocess(fixture_name: str, returncode: int = 0):
    """Create a mock for run_tool_subprocess that returns fixture data."""
    fixture_path = FIXTURES / fixture_name
    stdout = fixture_path.read_text()

    class FakeResult:
        def __init__(self) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    return FakeResult()


class TestRuffAdapter:
    def test_parse_findings(self, tmp_path: Path) -> None:
        with patch(
            "ai_hub.analysis.static.ruff.run_tool_subprocess",
            return_value=_mock_subprocess("ruff_output.json"),
        ):
            adapter = RuffAdapter()
            findings = adapter.run(tmp_path)

        assert len(findings) == 3
        assert findings[0].tool == "ruff"
        assert findings[0].rule_id == "F401"
        assert findings[0].severity == Severity.HIGH
        assert findings[0].source == FindingSource.STATIC

    def test_clean_run_prints_an_empty_list(self, tmp_path: Path) -> None:
        with patch(
            "ai_hub.analysis.static.ruff.run_tool_subprocess",
            return_value=type("R", (), {"stdout": "[]", "stderr": "", "returncode": 0})(),
        ):
            assert RuffAdapter().run(tmp_path) == []

    def test_empty_output_is_an_error_not_a_clean_result(self, tmp_path: Path) -> None:
        # A linter that prints nothing did not run properly; "clean" would hide that.
        with (
            patch(
                "ai_hub.analysis.static.ruff.run_tool_subprocess",
                return_value=type("R", (), {"stdout": "", "stderr": "", "returncode": 0})(),
            ),
            pytest.raises(AnalysisError, match="no output"),
        ):
            RuffAdapter().run(tmp_path)

    def test_fix_suggestion_captured(self, tmp_path: Path) -> None:
        with patch(
            "ai_hub.analysis.static.ruff.run_tool_subprocess",
            return_value=_mock_subprocess("ruff_output.json"),
        ):
            adapter = RuffAdapter()
            findings = adapter.run(tmp_path)
        b006 = next(f for f in findings if f.rule_id == "B006")
        assert b006.suggested_fix is not None


class TestESLintAdapter:
    def test_parse_findings(self, tmp_path: Path) -> None:
        with patch(
            "ai_hub.analysis.static.eslint.run_tool_subprocess",
            return_value=_mock_subprocess("eslint_output.json"),
        ):
            adapter = ESLintAdapter()
            findings = adapter.run(tmp_path)

        assert len(findings) == 3
        assert findings[0].tool == "eslint"

    def test_security_category_for_eval(self, tmp_path: Path) -> None:
        with patch(
            "ai_hub.analysis.static.eslint.run_tool_subprocess",
            return_value=_mock_subprocess("eslint_output.json"),
        ):
            findings = ESLintAdapter().run(tmp_path)
        eval_finding = next(f for f in findings if f.rule_id == "no-eval")
        assert eval_finding.category == FindingCategory.SECURITY

    def test_fix_captured(self, tmp_path: Path) -> None:
        with patch(
            "ai_hub.analysis.static.eslint.run_tool_subprocess",
            return_value=_mock_subprocess("eslint_output.json"),
        ):
            findings = ESLintAdapter().run(tmp_path)
        const_finding = next(f for f in findings if f.rule_id == "prefer-const")
        assert const_finding.suggested_fix == "const"


class TestSemgrepAdapter:
    def test_parse_findings(self, tmp_path: Path) -> None:
        with patch(
            "ai_hub.analysis.static.semgrep.run_tool_subprocess",
            return_value=_mock_subprocess("semgrep_output.json"),
        ):
            adapter = SemgrepAdapter()
            findings = adapter.run(tmp_path)

        assert len(findings) == 2
        assert findings[0].severity == Severity.HIGH
        assert findings[0].category == FindingCategory.SECURITY

    def test_security_mode(self, tmp_path: Path) -> None:
        with patch(
            "ai_hub.analysis.static.semgrep.run_tool_subprocess",
            return_value=_mock_subprocess("semgrep_output.json"),
        ):
            adapter = SemgrepAdapter(security_mode=True)
            findings = adapter.run(tmp_path)
        assert all(f.source == FindingSource.SECURITY for f in findings)

    def test_correctness_maps_to_bug(self, tmp_path: Path) -> None:
        with patch(
            "ai_hub.analysis.static.semgrep.run_tool_subprocess",
            return_value=_mock_subprocess("semgrep_output.json"),
        ):
            findings = SemgrepAdapter().run(tmp_path)
        open_finding = next(f for f in findings if "open-never-closed" in (f.rule_id or ""))
        assert open_finding.category == FindingCategory.BUG


class TestBanditAdapter:
    def test_parse_findings(self, tmp_path: Path) -> None:
        with patch(
            "ai_hub.analysis.security.bandit.run_tool_subprocess",
            return_value=_mock_subprocess("bandit_output.json"),
        ):
            findings = BanditAdapter().run(tmp_path)

        assert len(findings) == 3
        assert all(f.source == FindingSource.SECURITY for f in findings)
        assert all(f.category == FindingCategory.SECURITY for f in findings)

    def test_severity_mapping(self, tmp_path: Path) -> None:
        with patch(
            "ai_hub.analysis.security.bandit.run_tool_subprocess",
            return_value=_mock_subprocess("bandit_output.json"),
        ):
            findings = BanditAdapter().run(tmp_path)
        severities = {f.rule_id: f.severity for f in findings}
        assert severities["B105"] == Severity.LOW
        assert severities["B608"] == Severity.MEDIUM
        assert severities["B301"] == Severity.HIGH

    def test_confidence_mapping(self, tmp_path: Path) -> None:
        with patch(
            "ai_hub.analysis.security.bandit.run_tool_subprocess",
            return_value=_mock_subprocess("bandit_output.json"),
        ):
            findings = BanditAdapter().run(tmp_path)
        b301 = next(f for f in findings if f.rule_id == "B301")
        assert b301.confidence == 0.9


def _gitleaks_run(report_text: str | None, returncode: int = 1):
    """Fake run_tool_subprocess that writes the report file the way gitleaks does."""

    def fake(cmd, cwd, **kwargs):
        if report_text is not None:
            Path(cmd[cmd.index("--report-path") + 1]).write_text(report_text)
        return type("R", (), {"stdout": "", "stderr": "", "returncode": returncode})()

    return fake


class TestGitleaksAdapter:
    def test_parse_findings(self, tmp_path: Path) -> None:
        report = (FIXTURES / "gitleaks_output.json").read_text()
        with patch(
            "ai_hub.analysis.security.gitleaks.run_tool_subprocess",
            side_effect=_gitleaks_run(report),
        ):
            findings = GitleaksAdapter().run(tmp_path)

        assert len(findings) == 2
        assert all(f.severity == Severity.CRITICAL for f in findings)
        assert findings[0].rule_id == "aws-access-key-id"
        assert findings[1].rule_id == "github-pat"

    def test_secret_values_never_appear_in_findings(self, tmp_path: Path) -> None:
        report = (
            '[{"RuleID": "github-pat", "File": "a.py", "StartLine": 1, "EndLine": 1,'
            ' "Description": "token", "Match": "ghp_SHOULDNOTLEAK", "Secret": "ghp_SHOULDNOTLEAK"}]'
        )
        with patch(
            "ai_hub.analysis.security.gitleaks.run_tool_subprocess",
            side_effect=_gitleaks_run(report),
        ):
            findings = GitleaksAdapter().run(tmp_path)
        assert "SHOULDNOTLEAK" not in findings[0].model_dump_json()

    def test_redaction_flag_is_always_passed(self, tmp_path: Path) -> None:
        seen: list[list[str]] = []

        def fake(cmd, cwd, **kwargs):
            seen.append(cmd)
            return _gitleaks_run("[]", 0)(cmd, cwd)

        with patch("ai_hub.analysis.security.gitleaks.run_tool_subprocess", side_effect=fake):
            GitleaksAdapter().run(tmp_path)
        assert "--redact" in seen[0]
        # `--no-git --pipe` silently scans nothing, and this is a directory scan anyway.
        assert "--pipe" not in seen[0]

    def test_clean_scan(self, tmp_path: Path) -> None:
        with patch(
            "ai_hub.analysis.security.gitleaks.run_tool_subprocess",
            side_effect=_gitleaks_run("[]", 0),
        ):
            assert GitleaksAdapter().run(tmp_path) == []

    def test_null_report_is_clean(self, tmp_path: Path) -> None:
        with patch(
            "ai_hub.analysis.security.gitleaks.run_tool_subprocess",
            side_effect=_gitleaks_run("null", 0),
        ):
            assert GitleaksAdapter().run(tmp_path) == []

    def test_exit_1_without_a_report_is_a_failed_scan_not_a_clean_one(self, tmp_path: Path) -> None:
        # gitleaks exits 1 both for "leaks found" and for an unreadable source.
        with (
            patch(
                "ai_hub.analysis.security.gitleaks.run_tool_subprocess",
                side_effect=_gitleaks_run(None, 1),
            ),
            pytest.raises(AnalysisError, match="did not produce a report"),
        ):
            GitleaksAdapter().run(tmp_path)

    def test_corrupt_report_is_an_error(self, tmp_path: Path) -> None:
        with (
            patch(
                "ai_hub.analysis.security.gitleaks.run_tool_subprocess",
                side_effect=_gitleaks_run("{not json", 1),
            ),
            pytest.raises(AnalysisError, match="not valid JSON"),
        ):
            GitleaksAdapter().run(tmp_path)

    def test_check_reports_error_status(self, tmp_path: Path) -> None:
        with patch(
            "ai_hub.analysis.security.gitleaks.run_tool_subprocess",
            side_effect=_gitleaks_run(None, 1),
        ):
            assert GitleaksAdapter().check(tmp_path).status.value == "error"


class TestPipAuditAdapter:
    def test_parse_findings(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("requests==2.25.0\nflask==2.3.2\n")
        with patch(
            "ai_hub.analysis.security.deps.run_tool_subprocess",
            return_value=_mock_subprocess("pip_audit_output.json"),
        ):
            findings = PipAuditAdapter().run(tmp_path)

        assert len(findings) == 1
        assert "PYSEC-2023-74" in findings[0].rule_id
        assert findings[0].suggested_fix is not None
        assert "2.31.0" in (findings[0].suggested_fix or "")

    def test_no_vulns(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
        with patch(
            "ai_hub.analysis.security.deps.run_tool_subprocess",
            return_value=type(
                "R", (), {"stdout": '{"dependencies": []}', "stderr": "", "returncode": 0}
            )(),
        ):
            assert PipAuditAdapter().run(tmp_path) == []


class TestNpmAuditAdapter:
    def test_parse_findings(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        with patch(
            "ai_hub.analysis.security.deps.run_tool_subprocess",
            return_value=_mock_subprocess("npm_audit_output.json"),
        ):
            findings = NpmAuditAdapter().run(tmp_path)

        assert len(findings) == 2
        lodash = next(f for f in findings if f.rule_id == "lodash")
        assert lodash.severity == Severity.HIGH
        assert "4.17.21" in (lodash.suggested_fix or "")

        minimist = next(f for f in findings if f.rule_id == "minimist")
        assert minimist.severity == Severity.CRITICAL

    def test_no_package_json(self, tmp_path: Path) -> None:
        assert NpmAuditAdapter().run(tmp_path) == []


class TestToolAdapterBase:
    def test_check_wraps_error(self, tmp_path: Path) -> None:
        class CrashAdapter(ToolAdapter):
            name = "crash"
            tool_cmd = "crash"

            def run(self, repo_path, changed_files=None):
                raise RuntimeError("segfault")

        result = CrashAdapter().check(tmp_path)
        assert result.status.value == "error"
        assert "crashed" in result.summary.lower()

    def test_check_success(self, tmp_path: Path) -> None:
        class CleanAdapter(ToolAdapter):
            name = "clean"
            tool_cmd = "clean"

            def run(self, repo_path, changed_files=None):
                return []

        result = CleanAdapter().check(tmp_path)
        assert result.status.value == "success"


class TestRunToolSubprocess:
    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(AnalysisError, match="not found"):
            run_tool_subprocess(["nonexistent_tool_xyz_12345", "--version"], cwd=tmp_path)
