"""Markdown report generator — sticky PR comment with findings summary."""

from __future__ import annotations

from ai_hub.models import (
    SEVERITY_ORDER,
    CheckResult,
    Finding,
    GateResult,
    Severity,
)
from ai_hub.safety.redact import redact
from ai_hub.safety.untrusted import neutralize_mentions

COMMENT_MARKER = "<!-- ai-hub:summary -->"
_COMMENT_MARKER = COMMENT_MARKER  # kept for backwards compatibility
_SEVERITY_EMOJI = {
    Severity.CRITICAL: "🔴",
    Severity.HIGH: "🟠",
    Severity.MEDIUM: "🟡",
    Severity.LOW: "🔵",
    Severity.INFO: "⚪",
}
_MAX_TABLE_ROWS = 50
_MAX_COMMENT_CHARS = 60_000


def sanitize_text(text: str, *, limit: int = 200) -> str:
    """Make tool/AI/PR-provided text safe for a markdown table cell.

    No secrets, no @-mentions that ping people, no table-breaking characters.
    """
    flat = " ".join(redact(text or "").split())
    flat = flat.replace("|", "\\|").replace("`", "'")
    if len(flat) > limit:
        flat = flat[: limit - 1] + "…"
    return neutralize_mentions(flat)


def _code_block(text: str) -> str:
    return "```\n" + redact(text).replace("```", "'''") + "\n```"


def generate_summary_comment(
    findings: list[Finding],
    checks: list[CheckResult],
    gate_result: GateResult,
    gate_reasons: list[str],
    *,
    risk_score: float | None = None,
    quality_score: float | None = None,
    notes: list[str] | None = None,
    off_diff_count: int = 0,
    coverage_pct: float | None = None,
) -> str:
    """Generate the sticky PR comment markdown."""
    lines: list[str] = [COMMENT_MARKER, "## AI Hub — Quality Gate Report", ""]

    gate_icon = "✅" if gate_result == GateResult.PASS else "❌"
    lines.append(f"**Gate: {gate_icon} {gate_result.value.upper()}**")
    lines.append("")

    for reason in gate_reasons:
        lines.append(f"- {sanitize_text(reason, limit=300)}")
    lines.append("")

    for note in notes or []:
        lines.append(f"> ℹ️ {sanitize_text(note, limit=400)}")
    if notes:
        lines.append("")

    if risk_score is not None or quality_score is not None:
        lines.append("### Scores")
        if risk_score is not None:
            lines.append(f"- **Risk score:** {risk_score:.0f}/100")
        if quality_score is not None:
            lines.append(f"- **Quality score:** {quality_score:.0f}/100")
        else:
            lines.append("- **Quality score:** unavailable")
        if coverage_pct is not None:
            lines.append(f"- **Coverage:** {coverage_pct:.1f}%")
        lines.append("")

    lines.append("### Check Results")
    lines.append("| Check | Status | Summary |")
    lines.append("|-------|--------|---------|")
    long_outputs: list[tuple[str, str]] = []
    for check in checks:
        status_icon = {
            "success": "✅",
            "failed": "❌",
            "error": "⚠️",
            "skipped": "⏭️",
        }.get(check.status.value, "❓")
        first_line = (check.summary or "").strip().splitlines()[0] if check.summary else ""
        lines.append(
            f"| {sanitize_text(check.name, limit=40)} | {status_icon} {check.status.value} "
            f"| {sanitize_text(first_line, limit=160)} |"
        )
        if check.summary and (len(check.summary.strip().splitlines()) > 1 or len(first_line) > 160):
            long_outputs.append((check.name, check.summary))
    lines.append("")

    for name, output in long_outputs:
        lines.append(f"<details><summary>{sanitize_text(name, limit=40)} output</summary>")
        lines.append("")
        lines.append(_code_block(output[:3000]))
        lines.append("")
        lines.append("</details>")
        lines.append("")

    lines.append("### Findings")
    if findings:
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
        for f in sorted_findings[:_MAX_TABLE_ROWS]:
            emoji = _SEVERITY_EMOJI.get(f.severity, "")
            location = f"{sanitize_text(f.file, limit=80)}:{f.line_start}"
            lines.append(
                f"| {emoji} {f.severity.value} | `{location}` "
                f"| {sanitize_text(f.title, limit=160)} | {sanitize_text(f.tool, limit=30)} |"
            )
        if len(sorted_findings) > _MAX_TABLE_ROWS:
            lines.append("")
            lines.append(
                f"_…and {len(sorted_findings) - _MAX_TABLE_ROWS} more (see the artifacts)._"
            )
        lines.append("")
        lines.append("</details>")
    else:
        lines.append("No issues found on the changed lines.")

    if off_diff_count:
        lines.append("")
        lines.append(
            f"_{off_diff_count} additional finding(s) are outside the lines changed in this "
            "PR; they are not part of the gate._"
        )

    lines.append("")
    text = "\n".join(lines)
    if len(text) > _MAX_COMMENT_CHARS:
        text = text[:_MAX_COMMENT_CHARS] + "\n\n_(truncated)_\n"
    return text
