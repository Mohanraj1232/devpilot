"""Test runner with repair retry loop and flaky detection."""

from __future__ import annotations

import hashlib
import logging
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ai_hub.models import CheckStatus

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("ai_hub.devpilot")


@dataclass
class TestRunResult:
    status: CheckStatus
    output: str
    returncode: int
    diff_hash: str | None = None


def _compute_diff_hash(repo_path: Path) -> str:
    """Compute a hash of the current git diff to detect repeated fixes."""
    try:
        result = subprocess.run(
            ["git", "diff", "--staged", "--", "."],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return hashlib.sha256(result.stdout.encode()).hexdigest()[:12]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "unknown"


def run_tests(
    repo_path: Path,
    test_command: str,
    *,
    timeout_seconds: int = 900,
) -> TestRunResult:
    """Run the test command and return the result."""
    try:
        result = subprocess.run(
            test_command.split(),
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        status = CheckStatus.SUCCESS if result.returncode == 0 else CheckStatus.FAILED
        output = result.stdout[-5000:] + "\n" + result.stderr[-2000:]
        return TestRunResult(
            status=status,
            output=output,
            returncode=result.returncode,
            diff_hash=_compute_diff_hash(repo_path),
        )
    except subprocess.TimeoutExpired:
        return TestRunResult(
            status=CheckStatus.ERROR,
            output=f"Test command timed out after {timeout_seconds}s",
            returncode=-1,
        )
    except FileNotFoundError:
        return TestRunResult(
            status=CheckStatus.ERROR,
            output=f"Test command not found: {test_command.split()[0]}",
            returncode=-1,
        )


@dataclass
class RepairResult:
    final_status: CheckStatus
    attempts: int
    test_output: str
    is_flaky: bool = False


def run_test_repair_loop(
    repo_path: Path,
    test_command: str,
    *,
    max_attempts: int = 3,
    timeout_seconds: int = 900,
) -> RepairResult:
    """Run tests, and on failure return results for the agent to fix.

    The actual repair (sending failure back to Claude) is handled by
    the caller (agent.py). This just runs and tracks results.
    """
    seen_hashes: set[str] = set()
    last_run: TestRunResult | None = None

    for attempt in range(max_attempts):
        run = run_tests(repo_path, test_command, timeout_seconds=timeout_seconds)
        last_run = run

        if run.status == CheckStatus.SUCCESS:
            if attempt > 0 and run.diff_hash and run.diff_hash in seen_hashes:
                return RepairResult(
                    final_status=CheckStatus.SUCCESS,
                    attempts=attempt + 1,
                    test_output=run.output,
                    is_flaky=True,
                )
            return RepairResult(
                final_status=CheckStatus.SUCCESS,
                attempts=attempt + 1,
                test_output=run.output,
            )

        if run.status == CheckStatus.ERROR:
            return RepairResult(
                final_status=CheckStatus.ERROR,
                attempts=attempt + 1,
                test_output=run.output,
            )

        if run.diff_hash:
            if run.diff_hash in seen_hashes:
                logger.warning("Same diff hash seen again — stopping repair loop")
                return RepairResult(
                    final_status=CheckStatus.FAILED,
                    attempts=attempt + 1,
                    test_output=run.output,
                )
            seen_hashes.add(run.diff_hash)

    return RepairResult(
        final_status=CheckStatus.FAILED,
        attempts=max_attempts,
        test_output=last_run.output if last_run else "",
    )
