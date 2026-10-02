"""Repository management routes.

Every write (register, update, remove, verify, issue tokens) requires the logged-in user
to hold admin rights on the GitHub repository, checked live with their own OAuth token.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import get_github, hash_ingest_token, require_repo_admin
from app.config import Settings
from app.database import get_db
from app.github_client import GitHubUserClient
from app.models.tables import IngestToken, Repository
from app.schemas.repos import RepoCreate, RepoResponse, RepoUpdate, RepoVerification
from app.verification import verify_repository

router = APIRouter(prefix="/repos", tags=["repos"])


def _get_current_user_id(request: Request) -> int:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


def _get_active_repo(db: Session, repo_id: int) -> Repository:
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo or repo.status == "removed":
        raise HTTPException(status_code=404, detail="Repository not found")
    return repo


@router.get("", response_model=list[RepoResponse])
def list_repos(db: Session = Depends(get_db)) -> list[RepoResponse]:
    repos = db.query(Repository).filter(Repository.status != "removed").all()
    return [RepoResponse.model_validate(r) for r in repos]


@router.post("", response_model=RepoResponse, status_code=201)
def register_repo(
    body: RepoCreate,
    request: Request,
    db: Session = Depends(get_db),
    gh: GitHubUserClient = Depends(get_github),
) -> RepoResponse:
    user_id = _get_current_user_id(request)

    # Never trust client-supplied identifiers: take them from GitHub, as this user.
    github_repo = require_repo_admin(gh, body.full_name)
    if github_repo.get("id") != body.github_repo_id:
        raise HTTPException(status_code=400, detail="github_repo_id does not match the repository")
    full_name = str(github_repo["full_name"])

    existing = db.query(Repository).filter(Repository.full_name == full_name).first()
    if existing:
        if existing.status == "removed":
            existing.status = "active"
            existing.registered_by = user_id
            existing.removed_at = None
            db.commit()
            db.refresh(existing)
            return RepoResponse.model_validate(existing)
        raise HTTPException(status_code=409, detail="Repository already registered")

    repo = Repository(
        github_repo_id=int(github_repo["id"]),
        owner=str(github_repo["owner"]["login"]),
        name=str(github_repo["name"]),
        full_name=full_name,
        registered_by=user_id,
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return RepoResponse.model_validate(repo)


@router.get("/{repo_id}", response_model=RepoResponse)
def get_repo(repo_id: int, db: Session = Depends(get_db)) -> RepoResponse:
    return RepoResponse.model_validate(_get_active_repo(db, repo_id))


@router.patch("/{repo_id}", response_model=RepoResponse)
def update_repo(
    repo_id: int,
    body: RepoUpdate,
    request: Request,
    db: Session = Depends(get_db),
    gh: GitHubUserClient = Depends(get_github),
) -> RepoResponse:
    _get_current_user_id(request)
    repo = _get_active_repo(db, repo_id)
    require_repo_admin(gh, repo.full_name)

    if body.review_enabled is not None:
        repo.review_enabled = body.review_enabled
    if body.devpilot_enabled is not None:
        repo.devpilot_enabled = body.devpilot_enabled

    db.commit()
    db.refresh(repo)
    return RepoResponse.model_validate(repo)


@router.delete("/{repo_id}", status_code=204)
def remove_repo(
    repo_id: int,
    request: Request,
    db: Session = Depends(get_db),
    gh: GitHubUserClient = Depends(get_github),
) -> None:
    _get_current_user_id(request)
    repo = _get_active_repo(db, repo_id)
    require_repo_admin(gh, repo.full_name)

    repo.status = "removed"
    repo.removed_at = datetime.now(UTC)
    # Historical records stay, but the repository's tokens stop working immediately.
    db.query(IngestToken).filter(
        IngestToken.repo_id == repo.id, IngestToken.revoked_at.is_(None)
    ).update({"revoked_at": datetime.now(UTC)})
    db.commit()


@router.post("/{repo_id}/verify", response_model=RepoVerification)
def verify_repo(
    repo_id: int,
    request: Request,
    db: Session = Depends(get_db),
    gh: GitHubUserClient = Depends(get_github),
) -> RepoVerification:
    """Check, live against GitHub, that the bot, workflows and branch protection are in place."""
    _get_current_user_id(request)
    repo = _get_active_repo(db, repo_id)
    github_repo = require_repo_admin(gh, repo.full_name)
    result = verify_repository(
        gh,
        repo.full_name,
        str(github_repo.get("default_branch") or "main"),
        Settings().bot_login,
    )
    return RepoVerification(**result)


@router.post("/{repo_id}/tokens")
def rotate_ingest_token(
    repo_id: int,
    request: Request,
    db: Session = Depends(get_db),
    gh: GitHubUserClient = Depends(get_github),
) -> dict:
    """Issue a new ingest token (shown once) and revoke the previous one."""
    _get_current_user_id(request)
    repo = _get_active_repo(db, repo_id)
    require_repo_admin(gh, repo.full_name)

    db.query(IngestToken).filter(
        IngestToken.repo_id == repo_id,
        IngestToken.revoked_at.is_(None),
    ).update({"revoked_at": datetime.now(UTC)})

    raw_token = secrets.token_urlsafe(32)
    db.add(IngestToken(repo_id=repo_id, token_hash=hash_ingest_token(raw_token)))
    db.commit()

    return {"token": raw_token}
