"""SARIF report generator for uploading findings to GitHub Code Scanning."""

from __future__ import annotations

from typing import Any

from ai_hub.models import Finding, Severity

_SARIF_SEVERITY: dict[Severity, str] = {
    Severity.CRITICAL: "error",
    Severity.HIGH: "error",
    Severity.MEDIUM: "warning",
    Severity.LOW: "note",
    Severity.INFO: "note",
}


def generate_sarif(findings: list[Finding], tool_name: str = "ai-hub") -> dict[str, Any]:
    """Generate a SARIF 2.1.0 report from findings."""
    rules: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    rule_ids_seen: set[str] = set()

    for f in findings:
        rule_id = f"{f.tool}/{f.rule_id or f.fingerprint}"

        if rule_id not in rule_ids_seen:
            rule_ids_seen.add(rule_id)
            rules.append(
                {
                    "id": rule_id,
                    "shortDescription": {"text": f.title},
                    "defaultConfiguration": {
                        "level": _SARIF_SEVERITY.get(f.severity, "warning"),
                    },
                }
            )

        result: dict[str, Any] = {
            "ruleId": rule_id,
            "level": _SARIF_SEVERITY.get(f.severity, "warning"),
            "message": {"text": f.explanation},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": f.file},
                        "region": {
                            "startLine": max(1, f.line_start),
                        },
                    }
                }
            ],
        }
        if f.line_end:
            result["locations"][0]["physicalLocation"]["region"]["endLine"] = f.line_end

        results.append(result)

    return {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": tool_name,
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }
