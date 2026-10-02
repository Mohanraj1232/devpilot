"""Dependency audit adapters — pip-audit and npm audit via JSON output."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ai_hub.analysis.base import ToolAdapter, run_tool_subprocess
from ai_hub.models import Finding, FindingCategory, FindingSource, Severity

if TYPE_CHECKING:
    from pathlib import Path

_SEVERITY_MAP: dict[str, Severity] = {
    "low": Severity.LOW,
    "moderate": Severity.MEDIUM,
    "high": Severity.HIGH,
    "critical": Severity.CRITICAL,
}


class PipAuditAdapter(ToolAdapter):
    name = "pip-audit"
    tool_cmd = "pip-audit"

    def run(self, repo_path: Path, changed_files: list[str] | None = None) -> list[Finding]:
        cmd = ["pip-audit", "--format=json", "--desc"]

        requirements = repo_path / "requirements.txt"
        if requirements.is_file():
            cmd.extend(["--requirement", str(requirements)])
        else:
            cmd.append("--local")

        result = run_tool_subprocess(cmd, cwd=repo_path)

        if not result.stdout.strip():
            return []

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []

        findings: list[Finding] = []
        for vuln in data.get("dependencies", []):
            for v in vuln.get("vulns", []):
                vuln_id = v.get("id", "")
                severity = Severity.HIGH
                desc = v.get("description", "")
                fix_ver = v.get("fix_versions", [])

                findings.append(
                    Finding(
                        source=FindingSource.SECURITY,
                        tool="pip-audit",
                        rule_id=vuln_id,
                        category=FindingCategory.SECURITY,
                        severity=severity,
                        file="requirements.txt",
                        line_start=0,
                        title=f"{vuln_id}: {vuln.get('name', '')} {vuln.get('version', '')}",
                        explanation=desc[:500],
                        suggested_fix=(f"Upgrade to {', '.join(fix_ver)}" if fix_ver else None),
                    )
                )
        return findings


class NpmAuditAdapter(ToolAdapter):
    name = "npm-audit"
    tool_cmd = "npm"

    def run(self, repo_path: Path, changed_files: list[str] | None = None) -> list[Finding]:
        package_json = repo_path / "package.json"
        if not package_json.is_file():
            return []

        cmd = ["npm", "audit", "--json"]
        result = run_tool_subprocess(cmd, cwd=repo_path)

        if not result.stdout.strip():
            return []

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []

        findings: list[Finding] = []
        vulnerabilities = data.get("vulnerabilities", {})
        for pkg_name, vuln_info in vulnerabilities.items():
            severity_str = vuln_info.get("severity", "moderate")
            severity = _SEVERITY_MAP.get(severity_str, Severity.MEDIUM)

            via = vuln_info.get("via", [])
            desc_parts: list[str] = []
            for v in via:
                if isinstance(v, dict):
                    desc_parts.append(v.get("title", ""))
                elif isinstance(v, str):
                    desc_parts.append(v)
            description = "; ".join(filter(None, desc_parts))

            fix_available = vuln_info.get("fixAvailable")
            suggested_fix = None
            if isinstance(fix_available, dict):
                suggested_fix = (
                    f"Upgrade {fix_available.get('name', '')} to {fix_available.get('version', '')}"
                )
            elif fix_available is True:
                suggested_fix = "Run `npm audit fix`"

            findings.append(
                Finding(
                    source=FindingSource.SECURITY,
                    tool="npm-audit",
                    rule_id=pkg_name,
                    category=FindingCategory.SECURITY,
                    severity=severity,
                    file="package.json",
                    line_start=0,
                    title=f"Vulnerable dependency: {pkg_name}",
                    explanation=description[:500] or f"Vulnerability in {pkg_name}",
                    suggested_fix=suggested_fix,
                )
            )
        return findings
