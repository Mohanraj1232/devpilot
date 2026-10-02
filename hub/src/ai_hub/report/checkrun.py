"""GitHub Check Run payload builder for the quality gate."""

from __future__ import annotations

from ai_hub.models import GateResult


def build_check_run_payload(
    gate_result: GateResult,
    gate_reasons: list[str],
    *,
    head_sha: str,
    summary: str = "",
    details_url: str | None = None,
) -> dict[str, object]:
    """Build the payload for creating/updating the Check Run."""
    conclusion = "success" if gate_result == GateResult.PASS else "failure"

    output_summary = summary or "\n".join(f"- {r}" for r in gate_reasons)

    payload: dict[str, object] = {
        "name": "AI Hub / Quality Gate",
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": conclusion,
        "output": {
            "title": f"Quality Gate: {gate_result.value.upper()}",
            "summary": output_summary,
        },
    }

    if details_url:
        payload["details_url"] = details_url

    return payload
