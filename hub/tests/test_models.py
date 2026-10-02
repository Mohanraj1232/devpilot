"""Tests for ai_hub.models."""

import pytest
from pydantic import ValidationError

from ai_hub.models import (
    SEVERITY_ORDER,
    SEVERITY_WEIGHTS,
    CheckResult,
    CheckStatus,
    DevPilotExecution,
    ExecutionStatus,
    Finding,
    FindingCategory,
    FindingSource,
    FindingStatus,
    GateResult,
    ReviewResult,
    RunContext,
    Severity,
)


class TestSeverity:
    def test_all_severities_in_order(self) -> None:
        assert set(SEVERITY_ORDER.keys()) == set(Severity)

    def test_ordering(self) -> None:
        assert SEVERITY_ORDER[Severity.INFO] < SEVERITY_ORDER[Severity.CRITICAL]
        assert SEVERITY_ORDER[Severity.LOW] < SEVERITY_ORDER[Severity.HIGH]

    def test_weights_defined(self) -> None:
        assert SEVERITY_WEIGHTS[Severity.CRITICAL] == 40
        assert SEVERITY_WEIGHTS[Severity.HIGH] == 20
        assert SEVERITY_WEIGHTS[Severity.MEDIUM] == 8
        assert SEVERITY_WEIGHTS[Severity.LOW] == 2
        assert SEVERITY_WEIGHTS[Severity.INFO] == 0


class TestFinding:
    def test_fingerprint_deterministic(self) -> None:
        f1 = Finding(
            source=FindingSource.STATIC,
            tool="ruff",
            rule_id="E501",
            category=FindingCategory.STYLE,
            severity=Severity.LOW,
            file="main.py",
            line_start=10,
            title="Line too long",
            explanation="Line exceeds 100 chars",
        )
        f2 = Finding(
            source=FindingSource.STATIC,
            tool="ruff",
            rule_id="E501",
            category=FindingCategory.STYLE,
            severity=Severity.LOW,
            file="main.py",
            line_start=10,
            title="Line too long",
            explanation="Different explanation",
        )
        assert f1.fingerprint == f2.fingerprint

    def test_fingerprint_changes_on_different_input(self) -> None:
        base = dict(
            source=FindingSource.STATIC,
            tool="ruff",
            rule_id="E501",
            category=FindingCategory.STYLE,
            severity=Severity.LOW,
            file="main.py",
            line_start=10,
            title="Line too long",
            explanation="x",
        )
        f1 = Finding(**base)
        f2 = Finding(**{**base, "line_start": 20})
        assert f1.fingerprint != f2.fingerprint

    def test_default_status(self) -> None:
        f = Finding(
            source=FindingSource.AI,
            tool="claude",
            category=FindingCategory.BUG,
            severity=Severity.HIGH,
            file="app.py",
            line_start=1,
            title="Bug",
            explanation="Found a bug",
        )
        assert f.status == FindingStatus.NEW

    def test_confidence_bounds(self) -> None:
        with pytest.raises(ValidationError):
            Finding(
                source=FindingSource.AI,
                tool="claude",
                category=FindingCategory.BUG,
                severity=Severity.HIGH,
                file="app.py",
                line_start=1,
                title="Bug",
                explanation="x",
                confidence=1.5,
            )


class TestCheckResult:
    def test_creation(self) -> None:
        cr = CheckResult(name="static", status=CheckStatus.SUCCESS, summary="All clear")
        assert cr.name == "static"
        assert cr.status == CheckStatus.SUCCESS
        assert cr.duration_ms is None


class TestRunContext:
    def test_minimal(self) -> None:
        ctx = RunContext(
            repo_full_name="owner/repo",
            head_sha="abc123",
            config_version="v1hash",
        )
        assert ctx.pr_number is None
        assert ctx.started_at is not None


class TestReviewResult:
    def test_defaults(self) -> None:
        ctx = RunContext(repo_full_name="o/r", head_sha="abc", config_version="v1")
        result = ReviewResult(context=ctx)
        assert result.findings == []
        assert result.gate_result is None


class TestDevPilotExecution:
    def test_default_status(self) -> None:
        ex = DevPilotExecution(
            execution_id="exec-1",
            repo_full_name="owner/repo",
            issue_number=42,
            issue_hash="abc",
            base_sha="def",
            config_version="v1",
        )
        assert ex.status == ExecutionStatus.QUEUED
        assert ex.attempts == 0


class TestEnums:
    def test_gate_result_values(self) -> None:
        assert GateResult.PASS == "pass"
        assert GateResult.FAIL == "fail"

    def test_execution_status_values(self) -> None:
        assert ExecutionStatus.PR_OPENED == "pr_opened"
        assert ExecutionStatus.NEEDS_CLARIFICATION == "needs_clarification"
