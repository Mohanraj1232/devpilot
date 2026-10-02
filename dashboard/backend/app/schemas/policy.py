"""Policy API schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class PolicyCreate(BaseModel):
    policy_json: dict


class PolicyResponse(BaseModel):
    id: int
    repo_id: int
    version: int
    policy_json: dict
    created_by: int
    created_at: datetime

    model_config = {"from_attributes": True}


class WorkflowPolicyResponse(BaseModel):
    """Policy as seen by GitHub workflows: registration state plus the latest policy."""

    repo_id: int
    version: int
    policy_json: dict
    review_enabled: bool
    devpilot_enabled: bool
