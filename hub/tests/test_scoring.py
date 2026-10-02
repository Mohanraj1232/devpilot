"""Tests for ai_hub.scoring.engine — risk and quality scores."""

from ai_hub.models import (
    CheckResult,
    CheckStatus,
    Finding,
    FindingCategory,
    FindingSource,
    Severity,
)
from ai_hub.scoring.engine import compute_quality_score, compute_risk_score


def _make_finding(severity: Severity = Severity.MEDIUM) -> Finding:
    return Finding(
        source=FindingSource.STATIC,
        tool="test",
        category=FindingCategory.BUG,
        severity=severity,
        file="app.py",
        line_start=1,
        title="Test finding",
        explanation="Test",
    )


class TestRiskScore:
    def test_no_findings(self) -> None:
        score = compute_risk_score([], changed_lines=10)
        assert 0 < score < 10

    def test_critical_finding(self) -> None:
        score = compute_risk_score([_make_finding(Severity.CRITICAL)], changed_lines=10)
        assert score >= 40

    def test_capped_at_100(self) -> None:
        findings = [_make_finding(Severity.CRITICAL) for _ in range(10)]
        score = compute_risk_score(findings, changed_lines=1000)
        assert score == 100.0

    def test_sensitive_paths(self) -> None:
        score_normal = compute_risk_score([], changed_lines=10, file_paths=["utils.py"])
        score_sensitive = compute_risk_score([], changed_lines=10, file_paths=["auth/login.py"])
        assert score_sensitive > score_normal

    def test_coverage_unknown(self) -> None:
        score = compute_risk_score([], changed_lines=10, coverage_pct=None)
        score_known = compute_risk_score([], changed_lines=10, coverage_pct=90)
        assert score > score_known

    def test_coverage_below_threshold(self) -> None:
        score = compute_risk_score([], changed_lines=10, coverage_pct=50, coverage_threshold=80)
        score_above = compute_risk_score(
            [], changed_lines=10, coverage_pct=90, coverage_threshold=80
        )
        assert score > score_above

    def test_size_factor(self) -> None:
        small = compute_risk_score([], changed_lines=5)
        large = compute_risk_score([], changed_lines=500)
        assert large > small


class TestQualityScore:
    def test_clean_project(self) -> None:
        checks = [
            CheckResult(name="static", status=CheckStatus.SUCCESS, summary="ok"),
            CheckResult(name="tests", status=CheckStatus.SUCCESS, summary="ok"),
        ]
        score = compute_quality_score([], checks, coverage_pct=95)
        assert score == 100.0

    def test_findings_reduce_score(self) -> None:
        findings = [_make_finding() for _ in range(5)]
        checks = [CheckResult(name="static", status=CheckStatus.SUCCESS, summary="ok")]
        score = compute_quality_score(findings, checks, coverage_pct=95)
        assert score is not None
        assert score < 100.0

    def test_error_check_returns_none(self) -> None:
        checks = [CheckResult(name="static", status=CheckStatus.ERROR, summary="crashed")]
        score = compute_quality_score([], checks)
        assert score is None

    def test_test_failure_penalty(self) -> None:
        checks = [CheckResult(name="tests", status=CheckStatus.FAILED, summary="3 failed")]
        score_fail = compute_quality_score([], checks, coverage_pct=95)
        checks_pass = [CheckResult(name="tests", status=CheckStatus.SUCCESS, summary="ok")]
        score_pass = compute_quality_score([], checks_pass, coverage_pct=95)
        assert score_fail is not None
        assert score_pass is not None
        assert score_fail < score_pass

    def test_coverage_gap_penalty(self) -> None:
        checks = [CheckResult(name="static", status=CheckStatus.SUCCESS, summary="ok")]
        score_good = compute_quality_score([], checks, coverage_pct=90, coverage_threshold=80)
        score_bad = compute_quality_score([], checks, coverage_pct=50, coverage_threshold=80)
        assert score_good is not None
        assert score_bad is not None
        assert score_bad < score_good

    def test_min_zero(self) -> None:
        findings = [_make_finding() for _ in range(100)]
        checks = [CheckResult(name="tests", status=CheckStatus.FAILED, summary="fail")]
        score = compute_quality_score(findings, checks, coverage_pct=0, coverage_threshold=80)
        assert score == 0.0
