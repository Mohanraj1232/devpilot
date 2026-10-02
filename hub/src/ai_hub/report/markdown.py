"""Markdown report generator — sticky PR comment with findings summary."""

from __future__ import annotations

from ai_hub.models import (
    SEVERITY_ORDER,
    CheckResult,
    Finding,
    GateResult,
    Severity,
)

_COMMENT_MARKER = "<!-- ai-hub:summary -->"
_SEVERITY_EMOJI = {
    Severity.CRITICAL: "🔴",
    Severity.HIGH: "🟠",
    Severity.MEDIUM: "🟡",
    Severity.LOW: "🔵",
    Severity.INFO: "⚪",
}


def generate_summary_comment(
    findings: list[Finding],
    checks: list[CheckResult],
    gate_result: GateResult,
    gate_reasons: list[str],
    *,
    risk_score: float | None = None,
    quality_score: float | None = None,
) -> str:
    """Generate the sticky PR comment markdown."""
    lines: list[str] = [_COMMENT_MARKER, "## AI Hub — Quality Gate Report", ""]

    gate_icon = "✅" if gate_result == GateResult.PASS else "❌"
    lines.append(f"**Gate: {gate_icon} {gate_result.value.upper()}**")
    lines.append("")

    for reason in gate_reasons:
        lines.append(f"- {reason}")
    lines.append("")

    if risk_score is not None or quality_score is not None:
        lines.append("### Scores")
        if risk_score is not None:
            lines.append(f"- **Risk score:** {risk_score:.0f}/100")
        if quality_score is not None:
            lines.append(f"- **Quality score:** {quality_score:.0f}/100")
        else:
            lines.append("- **Quality score:** unavailable")
        lines.append("")

    lines.append("### Check Results")
    lines.append("| Check | Status | Summary |")
    lines.append("|-------|--------|---------|")
    for check in checks:
        status_icon = {
            "success": "✅",
            "failed": "❌",
            "error": "⚠️",
            "skipped": "⏭️",
        }.get(check.status.value, "❓")
        lines.append(f"| {check.name} | {status_icon} {check.status.value} | {check.summary} |")
    lines.append("")

    if findings:
        lines.append("### Findings")
        lines.append("")

        severity_counts: dict[Severity, int] = {}
        for f in findings:
            severity_counts[f.severity] = severity_counts.get(f.severity, 0) + 1

        for sev in sorted(severity_counts, key=lambda s: SEVERITY_ORDER[s], reverse=True):
            emoji = _SEVERITY_EMOJI.get(sev, "")
            lines.append(f"- {emoji} **{sev.value}**: {severity_counts[sev]}")
        lines.append("")

        lines.append("<details>")
        lines.append("<summary>Finding details</summary>")
        lines.append("")
        lines.append("| Severity | File | Title | Source |")
        lines.append("|----------|------|-------|--------|")
        sorted_findings = sorted(
            findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 0), reverse=True
        )
        for f in sorted_findings:
            emoji = _SEVERITY_EMOJI.get(f.severity, "")
            lines.append(
                f"| {emoji} {f.severity.value} | `{f.file}:{f.line_start}` | {f.title} | {f.tool} |"
            )
        lines.append("")
        lines.append("</details>")
    else:
        lines.append("### Findings")
        lines.append("No issues found.")

    lines.append("")
    return "\n".join(lines)
