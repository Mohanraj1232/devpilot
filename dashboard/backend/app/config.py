"""Application configuration via environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "mysql+pymysql://devpilot:devpilot@localhost:3306/devpilot"
    secret_key: str = "change-me-in-production"
    github_client_id: str = ""
    github_client_secret: str = ""
    github_redirect_uri: str = "http://localhost:8000/api/v1/auth/callback"
    cors_origins: list[str] = ["http://localhost:5173"]
    session_max_age: int = 86400

    model_config = {"env_prefix": "DEVPILOT_"}
