"""Shared request dependencies."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from typing import Any

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import get_db
from app.github_client import GitHubUserClient
from app.models.tables import IngestToken


def get_github(request: Request) -> GitHubUserClient:
    """GitHub client acting as the logged-in user (401 if not logged in)."""
    token = request.session.get("github_token")
    if not request.session.get("user_id") or not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return GitHubUserClient(token, Settings().github_api_url)


def require_repo_admin(gh: GitHubUserClient, full_name: str) -> dict[str, Any]:
    """The user must have admin rights on the GitHub repository. Returns GitHub's repo data."""
    data = gh.get_repo(full_name)
    if data is None:
        raise HTTPException(status_code=404, detail="Repository not found or not accessible")
    if not (data.get("permissions") or {}).get("admin"):
        raise HTTPException(
            status_code=403, detail="You need admin access to this repository on GitHub"
        )
    return data


def hash_ingest_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def get_ingest_token(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> IngestToken:
    """Authenticate a workflow request with its per-repository bearer token.

    Tokens are stored only as SHA-256 hashes; revoked tokens are rejected.
    """
    scheme, _, value = (authorization or "").partition(" ")
    raw_token = value.strip()
    if scheme.lower() != "bearer" or not raw_token:
        raise HTTPException(
            status_code=401,
            detail="Missing ingest token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = (
        db.query(IngestToken)
        .filter(
            IngestToken.token_hash == hash_ingest_token(raw_token),
            IngestToken.revoked_at.is_(None),
        )
        .first()
    )
    if token is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or revoked ingest token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token.last_used_at = datetime.now(UTC)
    db.commit()
    return token


def ensure_token_repo(token: IngestToken, repo_id: int) -> None:
    """A token may only act on the repository it was issued for."""
    if token.repo_id != repo_id:
        raise HTTPException(status_code=403, detail="Token is not valid for this repository")
