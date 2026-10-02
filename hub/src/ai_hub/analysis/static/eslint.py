"""ESLint adapter — JS/TS linting via JSON output."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ai_hub.analysis.base import ToolAdapter, run_tool_subprocess
from ai_hub.models import Finding, FindingCategory, FindingSource, Severity

if TYPE_CHECKING:
    from pathlib import Path

_SEVERITY_MAP: dict[int, Severity] = {
    0: Severity.INFO,
    1: Severity.LOW,
    2: Severity.MEDIUM,
}


class ESLintAdapter(ToolAdapter):
    name = "eslint"
    tool_cmd = "npx"

    def run(self, repo_path: Path, changed_files: list[str] | None = None) -> list[Finding]:
        cmd = ["npx", "eslint", "--format=json", "--no-error-on-unmatched-pattern"]
        if changed_files:
            cmd.extend(changed_files)
        else:
            cmd.append(".")

        result = run_tool_subprocess(cmd, cwd=repo_path)

        if not result.stdout.strip():
            return []

        try:
            raw_results = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []

        findings: list[Finding] = []
        for file_result in raw_results:
            filepath = file_result.get("filePath", "")
            for msg in file_result.get("messages", []):
                severity_num = msg.get("severity", 1)
                severity = _SEVERITY_MAP.get(severity_num, Severity.MEDIUM)
                rule_id = msg.get("ruleId") or "unknown"

                category = FindingCategory.STYLE
                if "security" in rule_id.lower() or "no-eval" in rule_id:
                    category = FindingCategory.SECURITY
                elif "no-unused" in rule_id or "prefer-" in rule_id:
                    category = FindingCategory.MAINTAINABILITY

                findings.append(
                    Finding(
                        source=FindingSource.STATIC,
                        tool="eslint",
                        rule_id=rule_id,
                        category=category,
                        severity=severity,
                        file=filepath,
                        line_start=msg.get("line", 0),
                        line_end=msg.get("endLine"),
                        title=f"{rule_id}: {msg.get('message', '')}",
                        explanation=msg.get("message", ""),
                        suggested_fix=msg.get("fix", {}).get("text") if msg.get("fix") else None,
                    )
                )
        return findings
