"""Ruff adapter — Python linting via JSON output."""

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
    "E": Severity.MEDIUM,
    "W": Severity.LOW,
    "F": Severity.HIGH,
    "I": Severity.INFO,
    "B": Severity.MEDIUM,
    "S": Severity.HIGH,
    "C": Severity.LOW,
    "UP": Severity.LOW,
    "SIM": Severity.LOW,
}

_CATEGORY_MAP: dict[str, FindingCategory] = {
    "E": FindingCategory.STYLE,
    "W": FindingCategory.STYLE,
    "F": FindingCategory.BUG,
    "I": FindingCategory.STYLE,
    "B": FindingCategory.BUG,
    "S": FindingCategory.SECURITY,
    "C": FindingCategory.MAINTAINABILITY,
    "UP": FindingCategory.MAINTAINABILITY,
    "SIM": FindingCategory.MAINTAINABILITY,
}


def _classify_rule(code: str) -> tuple[Severity, FindingCategory]:
    for prefix in sorted(_SEVERITY_MAP, key=len, reverse=True):
        if code.startswith(prefix):
            return _SEVERITY_MAP[prefix], _CATEGORY_MAP[prefix]
    return Severity.MEDIUM, FindingCategory.STYLE


_PY_EXTENSIONS = (".py", ".pyi")


class RuffAdapter(ToolAdapter):
    name = "ruff"
    tool_cmd = "ruff"

    def applicable(
        self, repo_path: Path, changed_files: list[str] | None = None
    ) -> tuple[bool, str]:
        files = select_files(changed_files, _PY_EXTENSIONS)
        if files is not None and not files:
            return False, "No Python files changed"
        return True, ""

    def run(self, repo_path: Path, changed_files: list[str] | None = None) -> list[Finding]:
        cmd = ["ruff", "check", "--output-format=json", "--no-fix", "--no-cache"]
        files = select_files(changed_files, _PY_EXTENSIONS)
        cmd.extend(files if files else ["."])

        # Exit 0 = clean, 1 = findings, 2 = ruff itself failed (bad config, crash).
        result = run_tool_subprocess(cmd, cwd=repo_path, ok_returncodes=(0, 1))
        raw_findings = parse_json_output(result, "ruff")

        findings: list[Finding] = []
        for item in raw_findings:
            code = item.get("code", "")
            severity, category = _classify_rule(code)
            loc = item.get("location", {})
            end_loc = item.get("end_location", {})

            findings.append(
                Finding(
                    source=FindingSource.STATIC,
                    tool="ruff",
                    rule_id=code,
                    category=category,
                    severity=severity,
                    file=item.get("filename", ""),
                    line_start=loc.get("row", 0),
                    line_end=end_loc.get("row"),
                    title=f"{code}: {item.get('message', '')}",
                    explanation=item.get("message", ""),
                    suggested_fix=item.get("fix", {}).get("message") if item.get("fix") else None,
                )
            )
        return findings
