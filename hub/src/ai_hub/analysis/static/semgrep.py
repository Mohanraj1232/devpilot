"""Semgrep adapter — multi-language static analysis via JSON output."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ai_hub.analysis.base import ToolAdapter, parse_json_output, run_tool_subprocess
from ai_hub.errors import AnalysisError, FailureReason
from ai_hub.models import Finding, FindingCategory, FindingSource, Severity

if TYPE_CHECKING:
    from pathlib import Path

_SEVERITY_MAP: dict[str, Severity] = {
    "INFO": Severity.INFO,
    "WARNING": Severity.MEDIUM,
    "ERROR": Severity.HIGH,
}

_IMPACT_MAP: dict[str, Severity] = {
    "LOW": Severity.LOW,
    "MEDIUM": Severity.MEDIUM,
    "HIGH": Severity.HIGH,
}


def _map_category(metadata: dict[str, object]) -> FindingCategory:
    cats = str(metadata.get("category", "")).lower()
    if "security" in cats:
        return FindingCategory.SECURITY
    if "performance" in cats:
        return FindingCategory.PERFORMANCE
    if "correctness" in cats or "bug" in cats:
        return FindingCategory.BUG
    return FindingCategory.MAINTAINABILITY


class SemgrepAdapter(ToolAdapter):
    name = "semgrep"
    tool_cmd = "semgrep"

    # An explicit ruleset: "auto" forces Semgrep to send usage metrics about scanned code.
    def __init__(self, *, config: str = "p/default", security_mode: bool = False) -> None:
        self.config = config
        self.source = FindingSource.SECURITY if security_mode else FindingSource.STATIC

    def run(self, repo_path: Path, changed_files: list[str] | None = None) -> list[Finding]:
        cmd = [
            "semgrep",
            "--json",
            "--metrics=off",
            "--quiet",
            "--config",
            self.config,
            "--no-git-ignore",
        ]
        if changed_files is not None:
            if not changed_files:
                return []
            cmd.extend(changed_files)
        else:
            cmd.append(str(repo_path))

        # Exit 0/1 = ran; 2+ = Semgrep failed (bad rules, registry unreachable).
        result = run_tool_subprocess(cmd, cwd=repo_path, ok_returncodes=(0, 1))
        data = parse_json_output(result, "semgrep")
        if not isinstance(data, dict):
            raise AnalysisError(FailureReason.TOOL_CRASH, "semgrep output has an unexpected shape")

        findings: list[Finding] = []
        for match in data.get("results", []):
            check_id = match.get("check_id", "")
            extra = match.get("extra", {})
            metadata = extra.get("metadata", {})

            severity_str = extra.get("severity", "WARNING")
            impact_str = str(metadata.get("impact", "")).upper()
            severity = _IMPACT_MAP.get(impact_str, _SEVERITY_MAP.get(severity_str, Severity.MEDIUM))

            findings.append(
                Finding(
                    source=self.source,
                    tool="semgrep",
                    rule_id=check_id,
                    category=_map_category(metadata),
                    severity=severity,
                    file=match.get("path", ""),
                    line_start=match.get("start", {}).get("line", 0),
                    line_end=match.get("end", {}).get("line"),
                    title=f"{check_id}: {extra.get('message', '')}",
                    explanation=extra.get("message", ""),
                    suggested_fix=extra.get("fix"),
                )
            )
        return findings
