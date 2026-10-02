"""Dependency audit adapters — pip-audit and npm audit via JSON output."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ai_hub.analysis.base import ToolAdapter, parse_json_output, run_tool_subprocess
from ai_hub.errors import AnalysisError, FailureReason
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

    def applicable(
        self, repo_path: Path, changed_files: list[str] | None = None
    ) -> tuple[bool, str]:
        # `pip-audit --local` would audit the CI runner's own packages, which says nothing
        # about the repository, so only a requirements file is audited.
        if not (repo_path / "requirements.txt").is_file():
            return False, "No requirements.txt"
        return True, ""

    def run(self, repo_path: Path, changed_files: list[str] | None = None) -> list[Finding]:
        requirements = repo_path / "requirements.txt"
        if not requirements.is_file():
            return []
        cmd = ["pip-audit", "--format=json", "--desc", "--requirement", str(requirements)]

        # Exit 0 = no vulnerabilities, 1 = vulnerabilities found; other codes = tool failure.
        result = run_tool_subprocess(cmd, cwd=repo_path, ok_returncodes=(0, 1))
        data = parse_json_output(result, "pip-audit")

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

    def applicable(
        self, repo_path: Path, changed_files: list[str] | None = None
    ) -> tuple[bool, str]:
        if not (repo_path / "package.json").is_file():
            return False, "No package.json"
        if not (repo_path / "package-lock.json").is_file():
            return False, "No package-lock.json (npm audit needs a lockfile)"
        return True, ""

    def run(self, repo_path: Path, changed_files: list[str] | None = None) -> list[Finding]:
        package_json = repo_path / "package.json"
        if not package_json.is_file():
            return []

        cmd = ["npm", "audit", "--json"]
        # Exit 0 = no vulnerabilities, 1 = vulnerabilities found; other codes = tool failure.
        result = run_tool_subprocess(cmd, cwd=repo_path, ok_returncodes=(0, 1))
        data = parse_json_output(result, "npm audit")
        if isinstance(data, dict) and data.get("error"):
            raise AnalysisError(
                FailureReason.TOOL_CRASH,
                f"npm audit failed: {str(data['error'].get('summary', ''))[:200]}",
            )

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
