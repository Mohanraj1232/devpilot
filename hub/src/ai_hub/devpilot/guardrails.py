"""Guardrails — validates agent output before push."""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field

from ai_hub.errors import FailureReason

logger = logging.getLogger("ai_hub.devpilot")

_FORBIDDEN_PATHS = [
    "CODEOWNERS",
    ".github/CODEOWNERS",
    "docs/CODEOWNERS",
    ".github/workflows/",
    ".github/FUNDING.yml",
]

# File-name globs that must never be committed by the agent (credentials / key material).
_FORBIDDEN_NAME_GLOBS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    ".npmrc",
    ".pypirc",
    ".netrc",
]

_SECURITY_CONFIG_PATHS = [
    ".github/branch-protection",
    ".github/settings.yml",
    ".github/dependabot.yml",
]

# Files that disable or weaken security tooling.
_SECURITY_TOOLING_PATHS = [
    ".gitleaks.toml",
    ".semgrepignore",
    ".ai-review/config.yml",
]


@dataclass
class GuardrailCheck:
    passed: bool
    violations: list[str]
    reasons: list[FailureReason] = field(default_factory=list)


def _result(violations: list[str], reason: FailureReason) -> GuardrailCheck:
    return GuardrailCheck(
        passed=not violations,
        violations=violations,
        reasons=[reason] if violations else [],
    )


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
    return _result(violations, FailureReason.DIFF_TOO_LARGE)


def check_forbidden_changes(
    changed_files: list[str],
    *,
    allow_workflow_changes: bool = False,
) -> GuardrailCheck:
    """Check for changes to forbidden paths (workflows, CODEOWNERS, credential files)."""
    violations: list[str] = []
    for f in changed_files:
        normalized = f.replace("\\", "/")
        name = normalized.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatch(name.lower(), g) for g in _FORBIDDEN_NAME_GLOBS):
            violations.append(f"Forbidden change (credential/key file): {f}")
            continue
        for forbidden in _FORBIDDEN_PATHS:
            if forbidden == ".github/workflows/" and allow_workflow_changes:
                continue
            if normalized.startswith(forbidden) or normalized == forbidden:
                violations.append(f"Forbidden change: {f}")
                break
    return _result(violations, FailureReason.FORBIDDEN_CHANGE)


def check_unrelated_changes(
    changed_files: list[str],
    planned_files: list[str],
) -> GuardrailCheck:
    """Check if changes are related to the planned files."""
    planned_set = set(planned_files)
    unrelated = [f for f in changed_files if f not in planned_set]

    if len(unrelated) > len(planned_files):
        return _result(
            [f"Too many unrelated files: {unrelated[:5]}"], FailureReason.UNRELATED_CHANGES
        )
    return GuardrailCheck(passed=True, violations=[])


def check_destructive_operations(
    changed_files: list[str],
    deleted_files: list[str],
    *,
    allow_destructive: bool = False,
    max_deletions: int = 5,
) -> GuardrailCheck:
    """Check for destructive operations: mass deletions and edits to security configuration."""
    if allow_destructive:
        return GuardrailCheck(passed=True, violations=[])

    violations: list[str] = []
    if len(deleted_files) > max_deletions:
        violations.append(f"Deleted {len(deleted_files)} files (max: {max_deletions})")

    for f in changed_files:
        normalized = f.replace("\\", "/")
        for sec_path in (*_SECURITY_CONFIG_PATHS, *_SECURITY_TOOLING_PATHS):
            if normalized.startswith(sec_path) or normalized == sec_path:
                violations.append(f"Security config change: {f}")
    return _result(violations, FailureReason.DESTRUCTIVE_OPERATION)


def run_all_guardrails(
    changed_files: list[str],
    changed_lines: int,
    planned_files: list[str],
    *,
    max_files: int = 20,
    max_lines: int = 800,
    allow_workflow_changes: bool = False,
    allow_destructive: bool = False,
    deleted_files: list[str] | None = None,
) -> GuardrailCheck:
    """Run all guardrail checks and aggregate results."""
    checks = [
        check_diff_size(changed_files, changed_lines, max_files=max_files, max_lines=max_lines),
        check_forbidden_changes(changed_files, allow_workflow_changes=allow_workflow_changes),
        check_unrelated_changes(changed_files, planned_files),
        check_destructive_operations(
            changed_files, deleted_files or [], allow_destructive=allow_destructive
        ),
    ]
    violations = [v for c in checks for v in c.violations]
    reasons = [r for c in checks for r in c.reasons]
    return GuardrailCheck(passed=not violations, violations=violations, reasons=reasons)
