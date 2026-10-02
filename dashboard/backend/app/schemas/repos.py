"""Repository API schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class RepoCreate(BaseModel):
    github_repo_id: int
    owner: str
    name: str
    full_name: str


class RepoUpdate(BaseModel):
    review_enabled: bool | None = None
    devpilot_enabled: bool | None = None


class RepoResponse(BaseModel):
    id: int
    github_repo_id: int
    owner: str
    name: str
    full_name: str
    review_enabled: bool
    devpilot_enabled: bool
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class RepoVerification(BaseModel):
    bot_collaborator: bool
    workflows_present: bool
    branch_protection: bool
    all_passed: bool
