"""Review run and finding schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ReviewRunResponse(BaseModel):
    id: int
    repo_id: int
    pr_number: int
    head_sha: str
    status: str
    risk_score: float | None
    quality_score: float | None
    gate_result: str | None
    started_at: datetime
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class FindingResponse(BaseModel):
    id: int
    source: str
    tool: str
    rule_id: str | None
    category: str
    severity: str
    file: str
    line_start: int | None
    title: str
    explanation: str | None
    fingerprint: str
    resolution: str

    model_config = {"from_attributes": True}


class FindingUpdate(BaseModel):
    resolution: str


class CheckResultResponse(BaseModel):
    id: int
    name: str
    status: str
    summary: str | None
    duration_ms: int | None

    model_config = {"from_attributes": True}


class ReviewRunDetail(ReviewRunResponse):
    checks: list[CheckResultResponse]
    findings: list[FindingResponse]
    gate_reasons: dict | None
