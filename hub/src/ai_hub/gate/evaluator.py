"""Quality gate evaluator — ordered rule evaluation producing PASS/FAIL."""

from __future__ import annotations

from ai_hub.models import (
    CheckResult,
    CheckStatus,
    Finding,
    GateResult,
    Severity,
)


def evaluate_gate(
    findings: list[Finding],
    checks: list[CheckResult],
    *,
    coverage_pct: float | None = None,
    coverage_threshold: int = 80,
    fail_on_critical: int = 1,
    fail_on_high: int = 1,
    require_tests_pass: bool = True,
    required_checks: list[str] | None = None,
    config_valid: bool = True,
) -> tuple[GateResult, list[str]]:
    """Evaluate the quality gate in order. Returns (result, reasons).

    Rule order (§7 of the plan):
    1. Config invalid -> FAIL
    2. Any required check error or missing -> FAIL
    3. Critical findings >= threshold -> FAIL
    4. High findings >= threshold -> FAIL
    5. Tests failed and require_tests_pass -> FAIL
    6. Coverage below threshold -> FAIL
    7. Otherwise -> PASS
    """
    reasons: list[str] = []

    if not config_valid:
        reasons.append("Configuration is invalid")
        return GateResult.FAIL, reasons

    if required_checks:
        check_map = {c.name: c for c in checks}
        for name in required_checks:
            if name not in check_map:
                reasons.append(f"Required check '{name}' is missing")
            elif check_map[name].status == CheckStatus.ERROR:
                reasons.append(f"Required check '{name}' has error status")
        if reasons:
            return GateResult.FAIL, reasons

    critical_count = sum(1 for f in findings if f.severity == Severity.CRITICAL)
    if critical_count >= fail_on_critical:
        reasons.append(f"{critical_count} critical finding(s) (threshold: {fail_on_critical})")
        return GateResult.FAIL, reasons

    high_count = sum(1 for f in findings if f.severity == Severity.HIGH)
    if high_count >= fail_on_high:
        reasons.append(f"{high_count} high-severity finding(s) (threshold: {fail_on_high})")
        return GateResult.FAIL, reasons

    if require_tests_pass:
        test_check = next((c for c in checks if c.name == "tests"), None)
        if test_check and test_check.status in (CheckStatus.FAILED, CheckStatus.ERROR):
            reasons.append("Tests did not pass")
            return GateResult.FAIL, reasons

    if coverage_threshold > 0:
        if coverage_pct is None:
            reasons.append("Coverage data unavailable (threshold is set)")
            return GateResult.FAIL, reasons
        if coverage_pct < coverage_threshold:
            reasons.append(f"Coverage {coverage_pct:.1f}% below threshold {coverage_threshold}%")
            return GateResult.FAIL, reasons

    reasons.append("All checks passed")
    return GateResult.PASS, reasons
