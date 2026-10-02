"""Test runner with a repair retry loop and flaky detection.

Test commands run in a scrubbed environment (no bot token, cloud credentials or
dashboard tokens) because they execute code written by the AI or the repository.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ai_hub.devpilot.flaky import detect_flaky_from_results
from ai_hub.devpilot.git_ops import working_tree_hash
from ai_hub.models import CheckStatus
from ai_hub.safety.env import scrubbed_env
from ai_hub.safety.redact import redact

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

logger = logging.getLogger("ai_hub.devpilot")

_PYTEST_NO_TESTS_RC = 5
_NPM_NO_TEST_MARKER = "no test specified"


@dataclass
class TestRunResult:
    __test__ = False  # not a pytest class

    status: CheckStatus
    output: str
    returncode: int
    diff_hash: str | None = None
    no_tests: bool = False


def _is_no_tests(command: list[str], returncode: int, output: str) -> bool:
    joined = " ".join(command).lower()
    if returncode == _PYTEST_NO_TESTS_RC and "pytest" in joined:
        return True
    return _NPM_NO_TEST_MARKER in output.lower()


def run_tests(
    repo_path: Path,
    test_command: str,
    *,
    timeout_seconds: int = 900,
    env: Mapping[str, str] | None = None,
) -> TestRunResult:
    """Run the test command (no shell) and return the result."""
    try:
        command = shlex.split(test_command)
    except ValueError as exc:
        return TestRunResult(CheckStatus.ERROR, f"Invalid test command: {exc}", -1)
    if not command:
        return TestRunResult(CheckStatus.ERROR, "Empty test command", -1)

    try:
        result = subprocess.run(
            command,
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            env=dict(env) if env is not None else scrubbed_env(),
        )
    except subprocess.TimeoutExpired:
        return TestRunResult(
            status=CheckStatus.ERROR,
            output=f"Test command timed out after {timeout_seconds}s",
            returncode=-1,
        )
    except (FileNotFoundError, PermissionError):
        return TestRunResult(
            status=CheckStatus.ERROR,
            output=f"Test command not found: {command[0]}",
            returncode=-1,
        )

    output = redact((result.stdout or "")[-5000:] + "\n" + (result.stderr or "")[-2000:])
    diff_hash = working_tree_hash(repo_path)

    if result.returncode != 0 and _is_no_tests(command, result.returncode, output):
        return TestRunResult(
            status=CheckStatus.SKIPPED,
            output=output,
            returncode=result.returncode,
            diff_hash=diff_hash,
            no_tests=True,
        )

    status = CheckStatus.SUCCESS if result.returncode == 0 else CheckStatus.FAILED
    return TestRunResult(
        status=status, output=output, returncode=result.returncode, diff_hash=diff_hash
    )


@dataclass
class RepairResult:
    final_status: CheckStatus
    attempts: int
    test_output: str
    is_flaky: bool = False
    flaky_evidence: str | None = None
    no_tests: bool = False
    stop_reason: str | None = None
    history: list[tuple[str, CheckStatus]] = field(default_factory=list)


def run_test_repair_loop(
    repo_path: Path,
    test_command: str,
    *,
    max_attempts: int = 3,
    timeout_seconds: int = 900,
    repair: Callable[[TestRunResult, int], bool] | None = None,
    flaky_reruns: int = 2,
    env: Mapping[str, str] | None = None,
) -> RepairResult:
    """Run tests; on failure rerun to rule out flakiness, then ask ``repair`` for a fix.

    ``max_attempts`` is the maximum number of distinct test runs against changed code.
    The loop stops early if the model reproduces a previously failed working tree.
    """
    failed_hashes: set[str] = set()
    history: list[tuple[str, CheckStatus]] = []
    last: TestRunResult | None = None

    for attempt in range(1, max_attempts + 1):
        run = run_tests(repo_path, test_command, timeout_seconds=timeout_seconds, env=env)
        last = run
        history.append((run.diff_hash or "", run.status))

        if run.status == CheckStatus.SUCCESS:
            return RepairResult(CheckStatus.SUCCESS, attempt, run.output, history=history)

        if run.status in (CheckStatus.ERROR, CheckStatus.SKIPPED):
            return RepairResult(
                run.status, attempt, run.output, no_tests=run.no_tests, history=history
            )

        # FAILED
        if run.diff_hash and run.diff_hash in failed_hashes:
            logger.warning("Same working tree failed again — stopping repair loop")
            return RepairResult(
                CheckStatus.FAILED,
                attempt,
                run.output,
                stop_reason="repeated_failed_state",
                history=history,
            )
        if run.diff_hash:
            failed_hashes.add(run.diff_hash)

        # Rerun the same code to separate real failures from flaky ones.
        if flaky_reruns > 0:
            outcomes = [CheckStatus.FAILED]
            for _ in range(flaky_reruns):
                rerun = run_tests(repo_path, test_command, timeout_seconds=timeout_seconds, env=env)
                outcomes.append(rerun.status)
                if rerun.status == CheckStatus.SUCCESS:
                    report = detect_flaky_from_results(outcomes)
                    return RepairResult(
                        CheckStatus.SUCCESS,
                        attempt,
                        rerun.output,
                        is_flaky=True,
                        flaky_evidence=report.evidence,
                        history=history,
                    )
                if rerun.status != CheckStatus.FAILED:
                    break

        if repair is None or attempt == max_attempts:
            break
        if not repair(run, attempt):
            return RepairResult(
                CheckStatus.FAILED,
                attempt,
                run.output,
                stop_reason="repair_failed",
                history=history,
            )

    return RepairResult(
        CheckStatus.FAILED,
        len(history),
        last.output if last else "",
        stop_reason="attempts_exhausted",
        history=history,
    )
