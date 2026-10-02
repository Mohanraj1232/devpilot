"""Bandit adapter — Python security analysis via JSON output."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ai_hub.analysis.base import (
    ToolAdapter,
    parse_json_output,
    run_tool_subprocess,
    select_files,
)
from ai_hub.models import Finding, FindingCategory, FindingSource, Severity

if TYPE_CHECKING:
    from pathlib import Path

_SEVERITY_MAP: dict[str, Severity] = {
    "LOW": Severity.LOW,
    "MEDIUM": Severity.MEDIUM,
    "HIGH": Severity.HIGH,
}

_CONFIDENCE_MAP: dict[str, float] = {
    "LOW": 0.3,
    "MEDIUM": 0.6,
    "HIGH": 0.9,
}


class BanditAdapter(ToolAdapter):
    name = "bandit"
    tool_cmd = "bandit"

    def applicable(
        self, repo_path: Path, changed_files: list[str] | None = None
    ) -> tuple[bool, str]:
        files = select_files(changed_files, (".py",))
        if files is not None and not files:
            return False, "No Python files changed"
        return True, ""

    def run(self, repo_path: Path, changed_files: list[str] | None = None) -> list[Finding]:
        cmd = ["bandit", "-f", "json", "-q", "-r"]
        files = select_files(changed_files, (".py",))
        cmd.extend(files if files else ["."])

        # Exit 0 = clean, 1 = issues found; anything else means Bandit itself failed.
        result = run_tool_subprocess(cmd, cwd=repo_path, ok_returncodes=(0, 1))
        data = parse_json_output(result, "bandit")

        findings: list[Finding] = []
        for item in data.get("results", []):
            severity = _SEVERITY_MAP.get(item.get("issue_severity", "MEDIUM"), Severity.MEDIUM)
            confidence = _CONFIDENCE_MAP.get(item.get("issue_confidence", "MEDIUM"), 0.6)

            findings.append(
                Finding(
                    source=FindingSource.SECURITY,
                    tool="bandit",
                    rule_id=item.get("test_id", ""),
                    category=FindingCategory.SECURITY,
                    severity=severity,
                    file=item.get("filename", ""),
                    line_start=item.get("line_number", 0),
                    line_end=max(item.get("line_range") or [item.get("line_number", 0)]),
                    title=f"{item.get('test_id', '')}: {item.get('issue_text', '')}",
                    explanation=item.get("issue_text", ""),
                    confidence=confidence,
                )
            )
        return findings
