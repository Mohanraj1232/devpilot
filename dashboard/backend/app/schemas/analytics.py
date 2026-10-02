"""Analytics API schemas."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class TimeSeriesPoint(BaseModel):
    date: date
    count: int


class CategoryCount(BaseModel):
    category: str
    count: int


class GateFailureReason(BaseModel):
    reason: str
    count: int


class DevPilotSuccessRate(BaseModel):
    total: int
    successful: int
    rate: float


class RepoTrend(BaseModel):
    repo_id: int
    full_name: str
    total_runs: int
    pass_rate: float
