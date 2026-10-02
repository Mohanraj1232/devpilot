"""Base interface for all analysis tool adapters."""

from __future__ import annotations

import logging
import subprocess
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ai_hub.errors import AnalysisError, FailureReason
from ai_hub.models import CheckResult, CheckStatus, Finding

if TYPE_CHECKING:
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
        except AnalysisError:
            raise
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
) -> subprocess.CompletedProcess[str]:
    """Run a tool subprocess with timeout and error handling."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if not allow_nonzero and result.returncode != 0:
            raise AnalysisError(
                FailureReason.TOOL_CRASH,
                f"Tool {cmd[0]} exited with code {result.returncode}",
                details={"stderr": result.stderr[:2000]},
            )
        return result
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
