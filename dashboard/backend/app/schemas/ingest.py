"""Ingest API schemas for workflow data."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ReviewRunIngest(BaseModel):
    repo_full_name: str
    pr_number: int
    head_sha: str
    workflow_run_id: int
    config_version: str | None = None
    status: str
    risk_score: float | None = None
    quality_score: float | None = None
    gate_result: str | None = None
    gate_reasons: dict | None = None
    checks: list[CheckIngest] | None = None
    findings: list[FindingIngest] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class CheckIngest(BaseModel):
    name: str
    status: str
    summary: str | None = None
    duration_ms: int | None = None


class FindingIngest(BaseModel):
    source: str
    tool: str
    rule_id: str | None = None
    category: str
    severity: str
    file: str
    line_start: int | None = None
    line_end: int | None = None
    title: str
    explanation: str | None = None
    suggested_fix: str | None = None
    fingerprint: str
    resolution: str = "open"


class ExecutionIngest(BaseModel):
    repo_full_name: str
    issue_number: int
    issue_hash: str
    base_sha: str
    workflow_run_id: int
    config_version: str | None = None
    status: str
    branch: str | None = None
    pr_number: int | None = None
    attempts: int = 0
    test_status: str | None = None
    failure_reason: str | None = None


class ExecutionUpdate(BaseModel):
    status: str
    branch: str | None = None
    pr_number: int | None = None
    attempts: int | None = None
    test_status: str | None = None
    failure_reason: str | None = None


class LockRequest(BaseModel):
    repo_full_name: str
    issue_number: int
    execution_id: str


class LockResponse(BaseModel):
    lock_key: str
    acquired: bool


# Forward references need model_rebuild
ReviewRunIngest.model_rebuild()
