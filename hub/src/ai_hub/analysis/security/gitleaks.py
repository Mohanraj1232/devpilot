"""Gitleaks adapter — secret detection via a JSON report file."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from ai_hub.analysis.base import ToolAdapter, run_tool_subprocess
from ai_hub.errors import AnalysisError, FailureReason
from ai_hub.models import Finding, FindingCategory, FindingSource, Severity


class GitleaksAdapter(ToolAdapter):
    name = "gitleaks"
    tool_cmd = "gitleaks"

    def run(self, repo_path: Path, changed_files: list[str] | None = None) -> list[Finding]:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "gitleaks-report.json"
            cmd = [
                "gitleaks",
                "detect",
                "--source",
                str(repo_path),
                "--no-git",
                "--no-banner",
                # Matched values are masked, so secrets never reach logs, comments or artifacts.
                "--redact",
                "--report-format",
                "json",
                "--report-path",
                str(report),
                "--exit-code",
                "1",
            ]
            # Exit 1 means "leaks found" but is also what a failed run (e.g. unreadable
            # source) returns, so the report file is the real proof that the scan completed.
            result = run_tool_subprocess(cmd, cwd=repo_path, ok_returncodes=(0, 1))

            if not report.is_file():
                raise AnalysisError(
                    FailureReason.TOOL_CRASH,
                    f"gitleaks did not produce a report (exit code {result.returncode})",
                )
            text = report.read_text(encoding="utf-8").strip()

        try:
            raw_findings = json.loads(text) if text else []
        except json.JSONDecodeError as exc:
            raise AnalysisError(
                FailureReason.TOOL_CRASH, "gitleaks report is not valid JSON"
            ) from exc
        if raw_findings is None:
            raw_findings = []
        if not isinstance(raw_findings, list):
            raise AnalysisError(FailureReason.TOOL_CRASH, "gitleaks report has an unexpected shape")

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
                        f"Rule '{item.get('RuleID', '')}' matched. The value is redacted; "
                        "rotate the credential if it is real and remove it from history."
                    ),
                    confidence=0.9,
                )
            )
        return findings
