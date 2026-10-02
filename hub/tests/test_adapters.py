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

    def test_empty_output(self, tmp_path: Path) -> None:
        with patch(
            "ai_hub.analysis.static.ruff.run_tool_subprocess",
            return_value=type("R", (), {"stdout": "", "stderr": "", "returncode": 0})(),
        ):
            adapter = RuffAdapter()
            findings = adapter.run(tmp_path)
        assert findings == []

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


class TestGitleaksAdapter:
    def test_parse_findings(self, tmp_path: Path) -> None:
        with patch(
            "ai_hub.analysis.security.gitleaks.run_tool_subprocess",
            return_value=_mock_subprocess("gitleaks_output.json"),
        ):
            findings = GitleaksAdapter().run(tmp_path)

        assert len(findings) == 2
        assert all(f.severity == Severity.CRITICAL for f in findings)
        assert findings[0].rule_id == "aws-access-key-id"
        assert findings[1].rule_id == "github-pat"

    def test_null_output(self, tmp_path: Path) -> None:
        with patch(
            "ai_hub.analysis.security.gitleaks.run_tool_subprocess",
            return_value=type("R", (), {"stdout": "null", "stderr": "", "returncode": 0})(),
        ):
            assert GitleaksAdapter().run(tmp_path) == []


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
