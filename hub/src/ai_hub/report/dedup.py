"""Finding deduplication and conflict detection."""

from __future__ import annotations

from ai_hub.models import Finding, FindingStatus


def deduplicate_findings(findings: list[Finding]) -> list[Finding]:
    """Deduplicate findings by fingerprint.

    When multiple sources report the same issue (same fingerprint),
    keep all of them but mark subsequent ones as 'conflict'.
    """
    seen: dict[str, Finding] = {}
    result: list[Finding] = []

    for f in findings:
        fp = f.fingerprint
        if fp in seen:
            conflict_finding = f.model_copy(update={"status": FindingStatus.CONFLICT})
            result.append(conflict_finding)
        else:
            seen[fp] = f
            result.append(f)

    return result


def merge_with_previous(
    current: list[Finding],
    previous_fingerprints: set[str],
) -> list[Finding]:
    """Mark findings that existed in a previous run as 'persisting'."""
    result: list[Finding] = []
    for f in current:
        if f.fingerprint in previous_fingerprints:
            result.append(f.model_copy(update={"status": FindingStatus.PERSISTING}))
        else:
            result.append(f)
    return result
