"""Tests for ai_hub.gate.evaluator — quality gate rule evaluation."""

from ai_hub.gate.evaluator import evaluate_gate
from ai_hub.models import (
    CheckResult,
    CheckStatus,
    Finding,
    FindingCategory,
    FindingSource,
    GateResult,
    Severity,
)


def _finding(severity: Severity) -> Finding:
    return Finding(
        source=FindingSource.STATIC,
        tool="test",
        category=FindingCategory.BUG,
        severity=severity,
        file="x.py",
        line_start=1,
        title="t",
        explanation="e",
    )


class TestGateEvaluator:
    def test_pass_no_findings(self) -> None:
        checks = [
            CheckResult(name="static", status=CheckStatus.SUCCESS, summary="ok"),
            CheckResult(name="security", status=CheckStatus.SUCCESS, summary="ok"),
            CheckResult(name="ai_review", status=CheckStatus.SUCCESS, summary="ok"),
        ]
        result, reasons = evaluate_gate(
            [], checks, coverage_pct=90, required_checks=["static", "security", "ai_review"]
        )
        assert result == GateResult.PASS

    def test_fail_invalid_config(self) -> None:
        result, reasons = evaluate_gate([], [], config_valid=False)
        assert result == GateResult.FAIL
        assert "invalid" in reasons[0].lower()

    def test_fail_missing_required_check(self) -> None:
        checks = [CheckResult(name="static", status=CheckStatus.SUCCESS, summary="ok")]
        result, reasons = evaluate_gate(
            [], checks, coverage_pct=90, required_checks=["static", "security"]
        )
        assert result == GateResult.FAIL
        assert any("missing" in r.lower() for r in reasons)

    def test_fail_error_required_check(self) -> None:
        checks = [
            CheckResult(name="static", status=CheckStatus.ERROR, summary="crashed"),
        ]
        result, reasons = evaluate_gate([], checks, coverage_pct=90, required_checks=["static"])
        assert result == GateResult.FAIL
        assert any("error" in r.lower() for r in reasons)

    def test_fail_critical_findings(self) -> None:
        findings = [_finding(Severity.CRITICAL)]
        result, reasons = evaluate_gate(findings, [], coverage_pct=90)
        assert result == GateResult.FAIL
        assert "critical" in reasons[0].lower()

    def test_fail_high_findings(self) -> None:
        findings = [_finding(Severity.HIGH)]
        result, reasons = evaluate_gate(findings, [], coverage_pct=90)
        assert result == GateResult.FAIL
        assert "high" in reasons[0].lower()

    def test_pass_below_threshold(self) -> None:
        findings = [_finding(Severity.HIGH)]
        result, _ = evaluate_gate(findings, [], coverage_pct=90, fail_on_high=2)
        assert result == GateResult.PASS

    def test_fail_tests_failed(self) -> None:
        checks = [CheckResult(name="tests", status=CheckStatus.FAILED, summary="fail")]
        result, reasons = evaluate_gate([], checks, coverage_pct=90, require_tests_pass=True)
        assert result == GateResult.FAIL
        assert any("test" in r.lower() for r in reasons)

    def test_pass_tests_not_required(self) -> None:
        checks = [CheckResult(name="tests", status=CheckStatus.FAILED, summary="fail")]
        result, _ = evaluate_gate([], checks, coverage_pct=90, require_tests_pass=False)
        assert result == GateResult.PASS

    def test_fail_coverage_below(self) -> None:
        result, reasons = evaluate_gate([], [], coverage_pct=50, coverage_threshold=80)
        assert result == GateResult.FAIL
        assert any("coverage" in r.lower() for r in reasons)

    def test_fail_coverage_unavailable(self) -> None:
        result, reasons = evaluate_gate([], [], coverage_pct=None, coverage_threshold=80)
        assert result == GateResult.FAIL
        assert any("unavailable" in r.lower() for r in reasons)

    def test_pass_coverage_zero_threshold(self) -> None:
        result, _ = evaluate_gate([], [], coverage_pct=None, coverage_threshold=0)
        assert result == GateResult.PASS

    def test_rule_order_config_first(self) -> None:
        findings = [_finding(Severity.CRITICAL)]
        result, reasons = evaluate_gate(findings, [], config_valid=False)
        assert result == GateResult.FAIL
        assert "config" in reasons[0].lower()
