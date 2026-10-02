"""Analysis stages of the PR review: static, security and tests.

Each stage returns ``(findings, CheckResult)``. A stage never raises for a tool problem:
a missing, crashing or unparseable tool becomes an ERROR check (which fails the gate when
the check is required) — it is never reported as a clean result.
"""

from __future__ import annotations

import fnmatch
import logging
import time
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from ai_hub.analysis.coverage import parse_cobertura_xml, parse_lcov
from ai_hub.analysis.security.bandit import BanditAdapter
from ai_hub.analysis.security.deps import NpmAuditAdapter, PipAuditAdapter
from ai_hub.analysis.security.gitleaks import GitleaksAdapter
from ai_hub.analysis.static.eslint import ESLintAdapter
from ai_hub.analysis.static.ruff import RuffAdapter
from ai_hub.analysis.static.semgrep import SemgrepAdapter
from ai_hub.devpilot.tester import run_tests
from ai_hub.devpilot.workspace import detect_project_type, install_dependencies
from ai_hub.models import CheckResult, CheckStatus, Finding
from ai_hub.safety.redact import redact

if TYPE_CHECKING:
    from ai_hub.analysis.base import ToolAdapter
    from ai_hub.config.schema import HubConfig

logger = logging.getLogger("ai_hub.review")

_MAX_FINDINGS_PER_TOOL = 500


def build_adapters(kind: str, tools: list[str]) -> list[ToolAdapter]:
    """Adapters for the configured tool names (names are validated by the config schema)."""
    adapters: list[ToolAdapter] = []
    for tool in tools:
        if kind == "static":
            if tool == "ruff":
                adapters.append(RuffAdapter())
            elif tool == "eslint":
                adapters.append(ESLintAdapter())
            elif tool == "semgrep":
                adapters.append(SemgrepAdapter())
        else:
            if tool == "semgrep":
                adapters.append(SemgrepAdapter(security_mode=True))
            elif tool == "gitleaks":
                adapters.append(GitleaksAdapter())
            elif tool == "bandit":
                adapters.append(BanditAdapter())
            elif tool == "deps":
                adapters.extend([PipAuditAdapter(), NpmAuditAdapter()])
    return adapters


def normalize_path(file: str, workspace: Path) -> str:
    """Tool output may use absolute or OS-specific paths; findings use repo-relative POSIX."""
    candidate = Path(file)
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(workspace.resolve())
        except (ValueError, OSError):
            return PurePosixPath(file.replace("\\", "/")).as_posix()
    posix = PurePosixPath(str(candidate).replace("\\", "/")).as_posix()
    return posix.removeprefix("./")


def _ignored(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def run_analysis(
    kind: str,
    workspace: Path,
    changed_files: list[str],
    config: HubConfig,
) -> tuple[list[Finding], CheckResult]:
    """Run the configured tools of one category ('static' or 'security')."""
    section = config.static_analysis if kind == "static" else config.security
    if not section.enabled:
        return [], CheckResult(
            name=kind, status=CheckStatus.SKIPPED, summary=f"{kind} analysis disabled in config"
        )

    start = time.monotonic()
    findings: list[Finding] = []
    tool_details: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    ran = 0
    ignore = [*config.paths.ignore, *config.paths.generated]

    for adapter in build_adapters(kind, section.tools):
        applicable, why = adapter.applicable(workspace, changed_files)
        if not applicable:
            tool_details[adapter.name] = {"status": "skipped", "reason": why}
            continue
        ran += 1
        try:
            found = adapter.run(workspace, changed_files)
        except Exception as exc:  # AnalysisError, parse errors, anything: fail closed
            message = redact(str(getattr(exc, "message", exc)))[:300]
            logger.error("%s failed: %s", adapter.name, message)
            tool_details[adapter.name] = {"status": "error", "reason": message}
            errors.append(f"{adapter.name}: {message}")
            continue

        kept: list[Finding] = []
        for finding in found[:_MAX_FINDINGS_PER_TOOL]:
            path = normalize_path(finding.file, workspace)
            if _ignored(path, ignore):
                continue
            kept.append(finding.model_copy(update={"file": path}))
        findings.extend(kept)
        tool_details[adapter.name] = {"status": "ok", "findings": len(kept)}

    elapsed = int((time.monotonic() - start) * 1000)
    if errors:
        status, summary = CheckStatus.ERROR, "Tool failure: " + "; ".join(errors)
    elif ran == 0:
        status, summary = CheckStatus.SKIPPED, "No applicable tools for this change"
    elif findings:
        status, summary = CheckStatus.FAILED, f"{len(findings)} finding(s)"
    else:
        status, summary = CheckStatus.SUCCESS, "No issues found"

    return findings, CheckResult(
        name=kind,
        status=status,
        summary=summary,
        duration_ms=elapsed,
        details={"tools": tool_details},
    )


def measure_coverage(workspace: Path, report: str | None) -> float | None:
    """Line coverage % from a Cobertura XML or LCOV report; None when unavailable."""
    if not report:
        return None
    path = (workspace / report).resolve()
    try:
        path.relative_to(workspace.resolve())
    except ValueError:
        return None
    parsed = (
        parse_lcov(path) if path.suffix.lower() in (".lcov", ".info") else parse_cobertura_xml(path)
    )
    return parsed.line_rate_pct if parsed else None


def run_tests_stage(
    workspace: Path, config: HubConfig
) -> tuple[list[Finding], CheckResult, float | None]:
    """Install dependencies, run the configured test command and read coverage."""
    start = time.monotonic()
    project = detect_project_type(workspace)
    command = config.tests.command or (project.test_command if project else None)
    if not command:
        return (
            [],
            CheckResult(
                name="tests",
                status=CheckStatus.SKIPPED,
                summary="No test command configured or detected",
            ),
            None,
        )

    if project is not None:
        install = install_dependencies(workspace, project)
        if not install.ok:
            return (
                [],
                CheckResult(
                    name="tests",
                    status=CheckStatus.ERROR,
                    summary=f"Dependency installation failed: {install.output[-300:]}",
                ),
                None,
            )

    result = run_tests(workspace, command, timeout_seconds=config.tests.timeout_minutes * 60)
    coverage = measure_coverage(workspace, config.tests.coverage_report)
    elapsed = int((time.monotonic() - start) * 1000)

    if result.no_tests:
        status, summary = CheckStatus.SKIPPED, "No tests were found"
    elif result.status == CheckStatus.SUCCESS:
        status, summary = CheckStatus.SUCCESS, "Tests passed"
    elif result.status == CheckStatus.FAILED:
        status, summary = CheckStatus.FAILED, "Tests failed:\n" + result.output[-600:]
    else:
        status, summary = CheckStatus.ERROR, result.output[-300:]

    return (
        [],
        CheckResult(
            name="tests",
            status=status,
            summary=summary,
            duration_ms=elapsed,
            details={"command": command, "coverage_pct": coverage},
        ),
        coverage,
    )


def is_on_diff(finding: Finding, added_lines: dict[str, set[int]]) -> bool:
    """Whether a finding is in code this pull request changed.

    A finding without a line (line <= 0, e.g. a vulnerable dependency) counts when its file
    was changed; otherwise it must overlap an added line.
    """
    lines = added_lines.get(finding.file)
    if lines is None:
        return False
    if finding.line_start <= 0:
        return True
    end = min(finding.line_end or finding.line_start, finding.line_start + 200)
    return any(n in lines for n in range(finding.line_start, end + 1))


def partition_findings(
    findings: list[Finding], added_lines: dict[str, set[int]]
) -> tuple[list[Finding], list[Finding]]:
    """Split into (on the diff, off the diff). Only the former affect scores and the gate."""
    on: list[Finding] = []
    off: list[Finding] = []
    for finding in findings:
        (on if is_on_diff(finding, added_lines) else off).append(finding)
    return on, off
