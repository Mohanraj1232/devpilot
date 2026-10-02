"""Typed failure taxonomy and safe-stop handling.

Every failure mode in the system maps to a FailureReason code. The safe_stop()
function ensures that on any unexpected condition the system preserves state,
redacts sensitive data, reports the failure, and exits cleanly.
"""

from __future__ import annotations

import logging
import sys
from enum import StrEnum
from typing import Any

logger = logging.getLogger("ai_hub")


class FailureReason(StrEnum):
    # Config / setup
    CONFIG_INVALID = "config_invalid"
    REPO_NOT_REGISTERED = "repo_not_registered"
    REPO_DISABLED = "repo_disabled"
    BOT_NOT_COLLABORATOR = "bot_not_collaborator"
    BRANCH_PROTECTION_MISSING = "branch_protection_missing"

    # Auth
    AUTH_FAILURE = "auth_failure"
    AWS_OIDC_FAILURE = "aws_oidc_failure"
    TOKEN_MISSING = "token_missing"
    PERMISSION_DENIED = "permission_denied"

    # Issue / PR triggers
    ISSUE_CLOSED = "issue_closed"
    ISSUE_DELETED = "issue_deleted"
    ISSUE_EDITED = "issue_edited"
    ISSUE_NOT_ACTIONABLE = "issue_not_actionable"
    DUPLICATE_EXECUTION = "duplicate_execution"
    EXISTING_PR = "existing_pr"

    # Repository / workspace
    REPO_EMPTY = "repo_empty"
    REPO_INACCESSIBLE = "repo_inaccessible"
    UNSUPPORTED_PROJECT = "unsupported_project"
    SUBMODULE_FAILURE = "submodule_failure"
    CLONE_FAILURE = "clone_failure"

    # AI / LLM
    MODEL_UNAVAILABLE = "model_unavailable"
    INVALID_RESPONSE = "invalid_response"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANNOT_SOLVE = "cannot_solve"

    # Git operations
    REBASE_CONFLICT = "rebase_conflict"
    BASE_MOVED = "base_moved"
    PUSH_FAILED = "push_failed"
    COMMIT_FAILED = "commit_failed"
    PR_CREATION_FAILED = "pr_creation_failed"
    FOREIGN_COMMITS = "foreign_commits"

    # Tests / analysis
    TESTS_FAILED = "tests_failed"
    TEST_TIMEOUT = "test_timeout"
    DEPENDENCY_INSTALL_FAILED = "dependency_install_failed"
    TOOL_CRASH = "tool_crash"
    NO_CHANGES = "no_changes"

    # Guardrails
    DIFF_TOO_LARGE = "diff_too_large"
    UNRELATED_CHANGES = "unrelated_changes"
    SECRET_DETECTED = "secret_detected"
    FORBIDDEN_CHANGE = "forbidden_change"
    DESTRUCTIVE_OPERATION = "destructive_operation"

    # Dashboard
    DASHBOARD_UNREACHABLE = "dashboard_unreachable"
    INGEST_FAILED = "ingest_failed"

    # General
    INTERNAL_ERROR = "internal_error"
    TIMEOUT = "timeout"


class HubError(Exception):
    """Base exception for all AI Hub errors."""

    def __init__(self, reason: FailureReason, message: str, details: dict[str, Any] | None = None):
        self.reason = reason
        self.message = message
        self.details = details or {}
        super().__init__(f"[{reason}] {message}")


class ConfigError(HubError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(FailureReason.CONFIG_INVALID, message, details)


class AuthError(HubError):
    def __init__(self, reason: FailureReason, message: str):
        super().__init__(reason, message)


class LLMError(HubError):
    def __init__(self, reason: FailureReason, message: str, details: dict[str, Any] | None = None):
        super().__init__(reason, message, details)


class GitError(HubError):
    def __init__(self, reason: FailureReason, message: str, details: dict[str, Any] | None = None):
        super().__init__(reason, message, details)


class GuardrailError(HubError):
    def __init__(self, reason: FailureReason, message: str, details: dict[str, Any] | None = None):
        super().__init__(reason, message, details)


class AnalysisError(HubError):
    def __init__(self, reason: FailureReason, message: str, details: dict[str, Any] | None = None):
        super().__init__(reason, message, details)


def safe_stop(
    error: HubError,
    *,
    exit_code: int = 1,
    exit_process: bool = True,
) -> None:
    """Uniform failure handler: log, preserve state, and exit cleanly.

    Called from any point in the pipeline when an unrecoverable error occurs.
    The caller is responsible for reporting to the issue/PR and dashboard
    before calling this function.
    """
    logger.error("Safe stop: [%s] %s", error.reason, error.message)
    if error.details:
        logger.error("Details: %s", error.details)

    if exit_process:
        sys.exit(exit_code)
