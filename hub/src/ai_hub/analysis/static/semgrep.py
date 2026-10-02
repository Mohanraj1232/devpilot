"""Semgrep adapter — multi-language static analysis via JSON output."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ai_hub.analysis.base import ToolAdapter, run_tool_subprocess
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

    def __init__(self, *, config: str = "auto", security_mode: bool = False) -> None:
        self.config = config
        self.source = FindingSource.SECURITY if security_mode else FindingSource.STATIC

    def run(self, repo_path: Path, changed_files: list[str] | None = None) -> list[Finding]:
        cmd = ["semgrep", "--json", "--config", self.config, "--no-git-ignore"]
        if changed_files:
            cmd.extend(changed_files)
        else:
            cmd.append(str(repo_path))

        result = run_tool_subprocess(cmd, cwd=repo_path)

        if not result.stdout.strip():
            return []

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []

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
