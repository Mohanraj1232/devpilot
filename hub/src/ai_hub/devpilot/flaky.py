"""Flaky test detection via rerun analysis."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ai_hub.models import CheckStatus

logger = logging.getLogger("ai_hub.devpilot")


@dataclass
class FlakyReport:
    is_flaky: bool
    evidence: str
    rerun_count: int


def detect_flaky_from_results(
    results: list[CheckStatus],
) -> FlakyReport:
    """Detect flaky tests from a sequence of test run results.

    If the same codebase produces both pass and fail results,
    there's likely a flaky test.
    """
    has_pass = CheckStatus.SUCCESS in results
    has_fail = CheckStatus.FAILED in results

    if has_pass and has_fail:
        passes = sum(1 for r in results if r == CheckStatus.SUCCESS)
        fails = sum(1 for r in results if r == CheckStatus.FAILED)
        return FlakyReport(
            is_flaky=True,
            evidence=f"Inconsistent results: {passes} pass, {fails} fail in {len(results)} runs",
            rerun_count=len(results),
        )

    return FlakyReport(
        is_flaky=False,
        evidence="Consistent results across runs",
        rerun_count=len(results),
    )


def detect_flaky_from_hashes(
    result_hashes: list[tuple[str, CheckStatus]],
) -> FlakyReport:
    """Detect flaky tests by comparing diff hashes with results.

    If the same diff hash produces different results, the test is flaky.
    """
    hash_results: dict[str, set[CheckStatus]] = {}

    for diff_hash, status in result_hashes:
        if diff_hash not in hash_results:
            hash_results[diff_hash] = set()
        hash_results[diff_hash].add(status)

    for diff_hash, statuses in hash_results.items():
        if len(statuses) > 1:
            return FlakyReport(
                is_flaky=True,
                evidence=f"Diff {diff_hash} produced both {statuses}",
                rerun_count=len(result_hashes),
            )

    return FlakyReport(
        is_flaky=False,
        evidence="Each diff hash produced consistent results",
        rerun_count=len(result_hashes),
    )
