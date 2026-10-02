"""Inline PR review comments for individual findings."""

from __future__ import annotations

from ai_hub.models import SEVERITY_ORDER, Finding, Severity


def build_inline_comments(
    findings: list[Finding],
    *,
    min_severity: str = "medium",
    max_comments: int = 25,
    existing_fingerprints: set[str] | None = None,
) -> list[dict[str, object]]:
    """Build a list of inline review comment payloads from findings.

    Filters by minimum severity, deduplicates against prior runs,
    and caps the total count.
    """
    try:
        min_sev = Severity(min_severity)
    except ValueError:
        min_sev = Severity.MEDIUM

    min_order = SEVERITY_ORDER[min_sev]
    existing = existing_fingerprints or set()

    eligible = [
        f
        for f in findings
        if SEVERITY_ORDER.get(f.severity, 0) >= min_order and f.fingerprint not in existing
    ]

    sorted_findings = sorted(
        eligible, key=lambda f: SEVERITY_ORDER.get(f.severity, 0), reverse=True
    )[:max_comments]

    comments: list[dict[str, object]] = []
    for f in sorted_findings:
        body = f"**{f.severity.value.upper()}** — {f.title}\n\n{f.explanation}"
        if f.suggested_fix:
            body += f"\n\n**Suggested fix:**\n```\n{f.suggested_fix}\n```"

        comment: dict[str, object] = {
            "path": f.file,
            "line": f.line_start,
            "body": body,
        }
        if f.line_end and f.line_end != f.line_start:
            comment["start_line"] = f.line_start
            comment["line"] = f.line_end

        comments.append(comment)

    return comments
