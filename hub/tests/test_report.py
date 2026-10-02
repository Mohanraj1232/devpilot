"""Tests for ai_hub.report — markdown, inline comments, check run, SARIF, dedup."""

from ai_hub.models import (
    CheckResult,
    CheckStatus,
    Finding,
    FindingCategory,
    FindingSource,
    FindingStatus,
    GateResult,
    Severity,
)
from ai_hub.report.checkrun import build_check_run_payload
from ai_hub.report.dedup import deduplicate_findings, merge_with_previous
from ai_hub.report.inline import build_inline_comments
from ai_hub.report.markdown import generate_summary_comment
from ai_hub.report.sarif import generate_sarif


def _finding(
    severity: Severity = Severity.MEDIUM,
    file: str = "app.py",
    line: int = 10,
    tool: str = "ruff",
) -> Finding:
    return Finding(
        source=FindingSource.STATIC,
        tool=tool,
        rule_id="TEST001",
        category=FindingCategory.BUG,
        severity=severity,
        file=file,
        line_start=line,
        title="Test finding",
        explanation="A test explanation",
        suggested_fix="Fix it",
    )


class TestMarkdownReport:
    def test_pass_report(self) -> None:
        checks = [CheckResult(name="static", status=CheckStatus.SUCCESS, summary="ok")]
        comment = generate_summary_comment([], checks, GateResult.PASS, ["All checks passed"])
        assert "<!-- ai-hub:summary -->" in comment
        assert "PASS" in comment
        assert "✅" in comment

    def test_fail_report(self) -> None:
        findings = [_finding(Severity.CRITICAL)]
        checks = [CheckResult(name="static", status=CheckStatus.FAILED, summary="1 finding")]
        comment = generate_summary_comment(
            findings,
            checks,
            GateResult.FAIL,
            ["1 critical finding"],
            risk_score=85.0,
            quality_score=40.0,
        )
        assert "FAIL" in comment
        assert "85" in comment
        assert "40" in comment
        assert "critical" in comment.lower()

    def test_no_findings(self) -> None:
        comment = generate_summary_comment([], [], GateResult.PASS, ["All clean"])
        assert "No issues found" in comment

    def test_scores_section(self) -> None:
        comment = generate_summary_comment(
            [], [], GateResult.PASS, ["ok"], risk_score=15.0, quality_score=None
        )
        assert "Risk score" in comment
        assert "unavailable" in comment


class TestInlineComments:
    def test_basic_comments(self) -> None:
        findings = [_finding(Severity.HIGH), _finding(Severity.LOW)]
        comments = build_inline_comments(findings, min_severity="medium")
        assert len(comments) == 1

    def test_dedup_against_existing(self) -> None:
        f = _finding()
        comments = build_inline_comments([f], existing_fingerprints={f.fingerprint})
        assert len(comments) == 0

    def test_max_cap(self) -> None:
        findings = [_finding(line=i) for i in range(50)]
        comments = build_inline_comments(findings, max_comments=5)
        assert len(comments) == 5

    def test_suggested_fix_included(self) -> None:
        comments = build_inline_comments([_finding(Severity.HIGH)])
        assert "Suggested fix" in comments[0]["body"]

    def test_multiline_range(self) -> None:
        f = Finding(
            source=FindingSource.STATIC,
            tool="test",
            category=FindingCategory.BUG,
            severity=Severity.HIGH,
            file="app.py",
            line_start=5,
            line_end=10,
            title="Range",
            explanation="Spans lines",
        )
        comments = build_inline_comments([f])
        assert comments[0]["start_line"] == 5
        assert comments[0]["line"] == 10


class TestCheckRun:
    def test_pass_payload(self) -> None:
        payload = build_check_run_payload(GateResult.PASS, ["All clean"], head_sha="abc123")
        assert payload["conclusion"] == "success"
        assert payload["name"] == "AI Hub / Quality Gate"

    def test_fail_payload(self) -> None:
        payload = build_check_run_payload(GateResult.FAIL, ["Critical finding"], head_sha="abc123")
        assert payload["conclusion"] == "failure"


class TestSARIF:
    def test_basic_sarif(self) -> None:
        findings = [_finding(Severity.HIGH), _finding(Severity.LOW, line=20)]
        sarif = generate_sarif(findings)
        assert sarif["version"] == "2.1.0"
        assert len(sarif["runs"]) == 1
        assert len(sarif["runs"][0]["results"]) == 2

    def test_empty_findings(self) -> None:
        sarif = generate_sarif([])
        assert sarif["runs"][0]["results"] == []

    def test_severity_mapping(self) -> None:
        sarif = generate_sarif([_finding(Severity.CRITICAL)])
        assert sarif["runs"][0]["results"][0]["level"] == "error"


class TestDedup:
    def test_no_duplicates(self) -> None:
        findings = [_finding(line=1), _finding(line=2)]
        result = deduplicate_findings(findings)
        assert len(result) == 2
        assert all(f.status == FindingStatus.NEW for f in result)

    def test_duplicate_marked_conflict(self) -> None:
        f1 = _finding(line=10)
        f2 = _finding(line=10)
        result = deduplicate_findings([f1, f2])
        assert len(result) == 2
        assert result[0].status == FindingStatus.NEW
        assert result[1].status == FindingStatus.CONFLICT

    def test_merge_with_previous(self) -> None:
        f = _finding()
        result = merge_with_previous([f], {f.fingerprint})
        assert result[0].status == FindingStatus.PERSISTING

    def test_merge_new_finding(self) -> None:
        f = _finding()
        result = merge_with_previous([f], set())
        assert result[0].status == FindingStatus.NEW
