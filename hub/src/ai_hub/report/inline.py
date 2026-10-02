"""Inline PR review comments for individual findings."""

from __future__ import annotations

import re

from ai_hub.models import SEVERITY_ORDER, Finding, Severity
from ai_hub.safety.redact import redact
from ai_hub.safety.untrusted import neutralize_mentions

_FP_MARKER = "<!-- ai-hub:fp:{fp} -->"
_FP_PATTERN = re.compile(r"<!-- ai-hub:fp:([0-9a-f]{8,64}) -->")


def fingerprints_in(comment_bodies: list[str]) -> set[str]:
    """Fingerprints already posted by earlier runs (so a re-run does not repeat a comment)."""
    found: set[str] = set()
    for body in comment_bodies:
        found.update(_FP_PATTERN.findall(body or ""))
    return found


def _clean(text: str) -> str:
    return neutralize_mentions(redact(text or "")).replace("```", "'''")


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
        # Inline comments must anchor to a real line.
        if f.line_start > 0
        and SEVERITY_ORDER.get(f.severity, 0) >= min_order
        and f.fingerprint not in existing
    ]

    sorted_findings = sorted(
        eligible, key=lambda f: SEVERITY_ORDER.get(f.severity, 0), reverse=True
    )[:max_comments]

    comments: list[dict[str, object]] = []
    for f in sorted_findings:
        body = f"**{f.severity.value.upper()}** — {_clean(f.title)}\n\n{_clean(f.explanation)}"
        if f.suggested_fix:
            body += f"\n\n**Suggested fix:**\n```\n{_clean(f.suggested_fix)}\n```"
        body += f"\n\n{_FP_MARKER.format(fp=f.fingerprint)}"

        comment: dict[str, object] = {
            "path": f.file,
            "line": f.line_start,
            "side": "RIGHT",
            "body": body,
        }
        if f.line_end and f.line_end > f.line_start:
            comment["start_line"] = f.line_start
            comment["start_side"] = "RIGHT"
            comment["line"] = f.line_end

        comments.append(comment)

    return comments
