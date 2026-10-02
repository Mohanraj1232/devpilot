"""Auth API schemas."""

from __future__ import annotations

from pydantic import BaseModel


class UserResponse(BaseModel):
    id: int
    github_id: int
    login: str
    avatar_url: str | None

    model_config = {"from_attributes": True}


class LoginUrl(BaseModel):
    url: str
