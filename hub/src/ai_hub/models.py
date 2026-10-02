"""Core domain models for the AI Hub engine."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, computed_field


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


SEVERITY_ORDER: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}

SEVERITY_WEIGHTS: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.LOW: 2,
    Severity.MEDIUM: 8,
    Severity.HIGH: 20,
    Severity.CRITICAL: 40,
}


class FindingSource(StrEnum):
    AI = "ai"
    STATIC = "static"
    SECURITY = "security"
    COVERAGE = "coverage"


class FindingCategory(StrEnum):
    BUG = "bug"
    SECURITY = "security"
    PERFORMANCE = "performance"
    MAINTAINABILITY = "maintainability"
    STYLE = "style"
    TEST = "test"


class FindingStatus(StrEnum):
    NEW = "new"
    PERSISTING = "persisting"
    CONFLICT = "conflict"


class FindingResolution(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"
    FIXED = "fixed"


class CheckStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class ExecutionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_CLARIFICATION = "needs_clarification"
    BLOCKED = "blocked"
    TESTS_FAILED = "tests_failed"
    FAILED = "failed"
    PR_OPENED = "pr_opened"
    PR_UPDATED = "pr_updated"
    PR_MERGED = "pr_merged"
    PR_CLOSED = "pr_closed"
    CANCELLED = "cancelled"


class GateResult(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class Finding(BaseModel):
    """A single code finding from any analysis source."""

    source: FindingSource
    tool: str
    rule_id: str | None = None
    category: FindingCategory
    severity: Severity
    file: str
    line_start: int
    line_end: int | None = None
    title: str
    explanation: str
    suggested_fix: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    status: FindingStatus = FindingStatus.NEW

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fingerprint(self) -> str:
        raw = f"{self.tool}:{self.rule_id or ''}:{self.file}:{self.line_start}:{self.title}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


class CheckResult(BaseModel):
    """Result of a single analysis check (static, security, ai_review, tests)."""

    name: str
    status: CheckStatus
    summary: str
    duration_ms: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class RunContext(BaseModel):
    """Contextual metadata for a review or DevPilot run."""

    repo_full_name: str
    pr_number: int | None = None
    issue_number: int | None = None
    head_sha: str
    base_sha: str | None = None
    workflow_run_id: int | None = None
    config_version: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ReviewResult(BaseModel):
    """Aggregated result of a full review pipeline run."""

    context: RunContext
    checks: list[CheckResult] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    risk_score: float | None = None
    quality_score: float | None = None
    gate_result: GateResult | None = None
    gate_reasons: list[str] = Field(default_factory=list)


class DevPilotExecution(BaseModel):
    """State of a DevPilot agent execution."""

    execution_id: str
    repo_full_name: str
    issue_number: int
    issue_hash: str
    base_sha: str
    branch: str | None = None
    pr_number: int | None = None
    config_version: str
    status: ExecutionStatus = ExecutionStatus.QUEUED
    attempts: int = 0
    test_status: CheckStatus | None = None
    failure_reason: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
