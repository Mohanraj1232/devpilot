"""Build the payload the dashboard's /ingest/review-runs endpoint expects."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ai_hub.safety.redact import redact

if TYPE_CHECKING:
    from ai_hub.review.outcome import ReviewOutcome

_MAX_FINDINGS = 500


def build_review_payload(
    outcome: ReviewOutcome, *, repo: str, pr_number: int, head_sha: str, run_id: int
) -> dict[str, Any]:
    """The dashboard stores PASS/FAIL in upper case and gate reasons as an object."""
    return {
        "repo_full_name": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "workflow_run_id": run_id,
        "config_version": outcome.config_version or None,
        "status": "completed",
        "risk_score": outcome.risk_score,
        "quality_score": outcome.quality_score,
        "gate_result": outcome.gate_result.value.upper(),
        "gate_reasons": {"reasons": outcome.gate_reasons, "notes": outcome.notes},
        "checks": [
            {
                "name": check.name,
                "status": check.status.value,
                "summary": redact(check.summary)[:500],
                "duration_ms": check.duration_ms,
            }
            for check in outcome.checks
        ],
        "findings": [
            {
                "source": f.source.value,
                "tool": f.tool,
                "rule_id": f.rule_id,
                "category": f.category.value,
                "severity": f.severity.value,
                "file": f.file,
                "line_start": f.line_start,
                "line_end": f.line_end,
                "title": redact(f.title)[:500],
                "explanation": redact(f.explanation)[:2000],
                "suggested_fix": redact(f.suggested_fix)[:2000] if f.suggested_fix else None,
                "fingerprint": f.fingerprint,
                "resolution": "open",
            }
            for f in outcome.findings[:_MAX_FINDINGS]
        ],
    }
