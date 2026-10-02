"""Base interface for all analysis tool adapters.

Adapters must fail closed: if a tool crashes, times out, is missing, or prints something
that cannot be parsed, that is an ERROR, never an empty (clean) result. The quality gate
treats an ERROR in a required check as a failure.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from ai_hub.errors import AnalysisError, FailureReason
from ai_hub.models import CheckResult, CheckStatus, Finding
from ai_hub.safety.env import scrubbed_env
from ai_hub.safety.redact import redact

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path

logger = logging.getLogger("ai_hub.analysis")


class ToolAdapter(ABC):
    """Abstract base for static/security analysis tool adapters.

    Subclasses implement `run()` to invoke the tool and parse its output
    into normalized Finding objects.
    """

    name: str
    tool_cmd: str

    @abstractmethod
    def run(self, repo_path: Path, changed_files: list[str] | None = None) -> list[Finding]:
        """Run the tool and return normalized findings."""

    def applicable(
        self, repo_path: Path, changed_files: list[str] | None = None
    ) -> tuple[bool, str]:
        """Whether the tool has anything to analyse here. Returns (applicable, reason_if_not)."""
        return True, ""

    def check(self, repo_path: Path, changed_files: list[str] | None = None) -> CheckResult:
        """Run the adapter and wrap the result in a CheckResult with error handling."""
        start = time.monotonic()
        try:
            findings = self.run(repo_path, changed_files)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            status = CheckStatus.FAILED if findings else CheckStatus.SUCCESS
            return CheckResult(
                name=self.name,
                status=status,
                summary=f"{len(findings)} finding(s)" if findings else "No issues found",
                duration_ms=elapsed_ms,
                details={"finding_count": len(findings)},
            )
        except AnalysisError as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return CheckResult(
                name=self.name,
                status=CheckStatus.ERROR,
                summary=f"Tool failed: {exc.message}",
                duration_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.exception("Tool %s crashed", self.name)
            return CheckResult(
                name=self.name,
                status=CheckStatus.ERROR,
                summary=f"Tool crashed: {exc}",
                duration_ms=elapsed_ms,
            )

    def is_available(self) -> bool:
        """Check if the tool binary is available on PATH."""
        try:
            subprocess.run(
                [self.tool_cmd, "--version"],
                capture_output=True,
                timeout=10,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False


def run_tool_subprocess(
    cmd: list[str],
    cwd: Path,
    *,
    timeout: int = 300,
    allow_nonzero: bool = True,
    ok_returncodes: Collection[int] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a tool with a timeout and a scrubbed environment (no tokens or cloud credentials).

    ``ok_returncodes`` lists the exit codes that mean "the tool ran" (many linters exit 1
    when they find issues but 2+ when they crash). Anything else raises AnalysisError.
    """
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=scrubbed_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise AnalysisError(
            FailureReason.TOOL_CRASH,
            f"Tool {cmd[0]} timed out after {timeout}s",
        ) from exc
    except FileNotFoundError as exc:
        raise AnalysisError(
            FailureReason.TOOL_CRASH,
            f"Tool {cmd[0]} not found on PATH",
        ) from exc
    except OSError as exc:  # e.g. NotADirectoryError / PermissionError for cwd or the binary
        raise AnalysisError(FailureReason.TOOL_CRASH, f"Cannot run {cmd[0]}: {exc}") from exc

    failed = (
        result.returncode not in ok_returncodes
        if ok_returncodes is not None
        else (not allow_nonzero and result.returncode != 0)
    )
    if failed:
        raise AnalysisError(
            FailureReason.TOOL_CRASH,
            f"Tool {cmd[0]} exited with code {result.returncode}: "
            f"{redact((result.stderr or result.stdout or '').strip())[:300]}",
            details={"stderr": redact(result.stderr or "")[:2000]},
        )
    return result


def parse_json_output(result: Any, tool: str) -> Any:
    """Parse a tool's JSON stdout. Empty or malformed output is an error, never "clean"."""
    text = (getattr(result, "stdout", "") or "").strip()
    if not text:
        raise AnalysisError(
            FailureReason.TOOL_CRASH,
            f"{tool} produced no output (exit code {getattr(result, 'returncode', '?')})",
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AnalysisError(
            FailureReason.TOOL_CRASH, f"{tool} produced output that is not valid JSON"
        ) from exc


def select_files(changed_files: list[str] | None, extensions: tuple[str, ...]) -> list[str] | None:
    """Keep only changed files with the given extensions. None means "scan everything"."""
    if changed_files is None:
        return None
    return [f for f in changed_files if f.lower().endswith(extensions)]
