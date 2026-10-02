"""Guardrails — validates agent output before push."""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("ai_hub.devpilot")

_FORBIDDEN_PATHS = [
    "CODEOWNERS",
    ".github/workflows/",
]


@dataclass
class GuardrailCheck:
    passed: bool
    violations: list[str]


def check_diff_size(
    changed_files: list[str],
    changed_lines: int,
    *,
    max_files: int = 20,
    max_lines: int = 800,
) -> GuardrailCheck:
    """Check if the diff exceeds size limits."""
    violations: list[str] = []
    if len(changed_files) > max_files:
        violations.append(f"Changed {len(changed_files)} files (max: {max_files})")
    if changed_lines > max_lines:
        violations.append(f"Changed {changed_lines} lines (max: {max_lines})")
    return GuardrailCheck(passed=len(violations) == 0, violations=violations)


def check_forbidden_changes(
    changed_files: list[str],
    *,
    allow_workflow_changes: bool = False,
) -> GuardrailCheck:
    """Check for changes to forbidden paths."""
    violations: list[str] = []
    for f in changed_files:
        for forbidden in _FORBIDDEN_PATHS:
            if forbidden == ".github/workflows/" and allow_workflow_changes:
                continue
            if f.startswith(forbidden) or f == forbidden:
                violations.append(f"Forbidden change: {f}")
    return GuardrailCheck(passed=len(violations) == 0, violations=violations)


def check_unrelated_changes(
    changed_files: list[str],
    planned_files: list[str],
) -> GuardrailCheck:
    """Check if changes are related to the planned files."""
    planned_set = set(planned_files)
    unrelated = [f for f in changed_files if f not in planned_set]

    if len(unrelated) > len(planned_files):
        return GuardrailCheck(
            passed=False,
            violations=[f"Too many unrelated files: {unrelated[:5]}"],
        )
    return GuardrailCheck(passed=True, violations=[])


def run_all_guardrails(
    changed_files: list[str],
    changed_lines: int,
    planned_files: list[str],
    *,
    max_files: int = 20,
    max_lines: int = 800,
    allow_workflow_changes: bool = False,
) -> GuardrailCheck:
    """Run all guardrail checks and aggregate results."""
    all_violations: list[str] = []

    size_check = check_diff_size(
        changed_files, changed_lines, max_files=max_files, max_lines=max_lines
    )
    all_violations.extend(size_check.violations)

    forbidden_check = check_forbidden_changes(
        changed_files, allow_workflow_changes=allow_workflow_changes
    )
    all_violations.extend(forbidden_check.violations)

    unrelated_check = check_unrelated_changes(changed_files, planned_files)
    all_violations.extend(unrelated_check.violations)

    return GuardrailCheck(passed=len(all_violations) == 0, violations=all_violations)
