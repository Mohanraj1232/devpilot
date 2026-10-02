"""Scoring engine — computes risk and quality scores from findings and coverage."""

from __future__ import annotations

from ai_hub.models import (
    SEVERITY_WEIGHTS,
    CheckResult,
    CheckStatus,
    Finding,
)

_SENSITIVE_PATHS = {"auth", "crypto", "security", "migration", "secret", "key", "infra"}
_SIZE_CAP = 15
_SENSITIVE_BONUS = 10
_COVERAGE_BELOW_PENALTY = 10
_COVERAGE_UNKNOWN_PENALTY = 5

_QUALITY_WEIGHTS: dict[str, float] = {
    "static_penalty_per_finding": 2.0,
    "coverage_gap_multiplier": 0.3,
    "test_failure_penalty": 20.0,
}


def compute_risk_score(
    findings: list[Finding],
    changed_lines: int,
    *,
    coverage_pct: float | None = None,
    coverage_threshold: int = 80,
    file_paths: list[str] | None = None,
) -> float:
    """Risk score 0-100 (higher is riskier).

    Components:
    - Severity-weighted sum of findings on changed lines
    - Size factor (changed_lines / 50, capped at 15)
    - Sensitive-path factor (+10 if auth/crypto/infra/migration files touched)
    - Coverage factor (+10 if below threshold, +5 if unknown)
    """
    severity_sum = sum(SEVERITY_WEIGHTS.get(f.severity, 0) for f in findings)

    size_factor = min(changed_lines / 50, _SIZE_CAP)

    sensitive_factor = 0.0
    if file_paths:
        for path in file_paths:
            path_lower = path.lower()
            if any(s in path_lower for s in _SENSITIVE_PATHS):
                sensitive_factor = _SENSITIVE_BONUS
                break

    coverage_factor = 0.0
    if coverage_pct is None:
        coverage_factor = _COVERAGE_UNKNOWN_PENALTY
    elif coverage_pct < coverage_threshold:
        coverage_factor = _COVERAGE_BELOW_PENALTY

    return min(100.0, severity_sum + size_factor + sensitive_factor + coverage_factor)


def compute_quality_score(
    findings: list[Finding],
    checks: list[CheckResult],
    *,
    coverage_pct: float | None = None,
    coverage_threshold: int = 80,
) -> float | None:
    """Quality score 0-100 (higher is better).

    Returns None if any required input has status 'error'.
    """
    for check in checks:
        if check.status == CheckStatus.ERROR:
            return None

    penalty = 0.0

    static_findings = [f for f in findings if f.source.value in ("static", "ai")]
    penalty += len(static_findings) * _QUALITY_WEIGHTS["static_penalty_per_finding"]

    if coverage_pct is not None and coverage_pct < coverage_threshold:
        gap = coverage_threshold - coverage_pct
        penalty += gap * _QUALITY_WEIGHTS["coverage_gap_multiplier"]

    for check in checks:
        if check.name == "tests" and check.status == CheckStatus.FAILED:
            penalty += _QUALITY_WEIGHTS["test_failure_penalty"]
            break

    return max(0.0, 100.0 - penalty)
