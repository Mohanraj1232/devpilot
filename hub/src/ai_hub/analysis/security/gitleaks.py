"""Gitleaks adapter — secret detection via JSON output."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ai_hub.analysis.base import ToolAdapter, run_tool_subprocess
from ai_hub.models import Finding, FindingCategory, FindingSource, Severity

if TYPE_CHECKING:
    from pathlib import Path


class GitleaksAdapter(ToolAdapter):
    name = "gitleaks"
    tool_cmd = "gitleaks"

    def run(self, repo_path: Path, changed_files: list[str] | None = None) -> list[Finding]:
        cmd = [
            "gitleaks",
            "detect",
            "--source",
            str(repo_path),
            "--report-format",
            "json",
            "--report-path",
            "/dev/stdout",
            "--no-git",
        ]

        result = run_tool_subprocess(cmd, cwd=repo_path)

        if not result.stdout.strip() or result.stdout.strip() == "null":
            return []

        try:
            raw_findings = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []

        if not isinstance(raw_findings, list):
            return []

        findings: list[Finding] = []
        for item in raw_findings:
            findings.append(
                Finding(
                    source=FindingSource.SECURITY,
                    tool="gitleaks",
                    rule_id=item.get("RuleID", ""),
                    category=FindingCategory.SECURITY,
                    severity=Severity.CRITICAL,
                    file=item.get("File", ""),
                    line_start=item.get("StartLine", 0),
                    line_end=item.get("EndLine"),
                    title=f"Secret detected: {item.get('Description', '')}",
                    explanation=(
                        f"Rule: {item.get('RuleID', '')}. Match: {item.get('Match', '')[:50]}..."
                    ),
                    confidence=0.9,
                )
            )
        return findings


def scan_diff_for_secrets(diff_text: str, repo_path: Path) -> list[Finding]:
    """Run gitleaks on a diff string (pre-commit check)."""
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".diff", delete=False) as f:
        f.write(diff_text)
        diff_path = f.name

    try:
        result = subprocess.run(
            [
                "gitleaks",
                "detect",
                "--pipe",
                "--report-format",
                "json",
                "--report-path",
                "/dev/stdout",
            ],
            input=diff_text,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if not result.stdout.strip() or result.stdout.strip() == "null":
            return []

        raw = json.loads(result.stdout)
        if not isinstance(raw, list):
            return []

        return [
            Finding(
                source=FindingSource.SECURITY,
                tool="gitleaks",
                rule_id=item.get("RuleID", ""),
                category=FindingCategory.SECURITY,
                severity=Severity.CRITICAL,
                file=item.get("File", ""),
                line_start=item.get("StartLine", 0),
                line_end=item.get("EndLine"),
                title=f"Secret in diff: {item.get('Description', '')}",
                explanation=f"Rule: {item.get('RuleID', '')}",
                confidence=0.95,
            )
            for item in raw
        ]
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return []
    finally:
        import os

        os.unlink(diff_path)
