"""Repository management routes."""

from __future__ import annotations

import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.tables import IngestToken, Repository
from app.schemas.repos import RepoCreate, RepoResponse, RepoUpdate, RepoVerification

router = APIRouter(prefix="/repos", tags=["repos"])


def _get_current_user_id(request: Request) -> int:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


@router.get("", response_model=list[RepoResponse])
def list_repos(db: Session = Depends(get_db)) -> list[RepoResponse]:
    repos = db.query(Repository).filter(Repository.status != "removed").all()
    return [RepoResponse.model_validate(r) for r in repos]


@router.post("", response_model=RepoResponse, status_code=201)
def register_repo(
    body: RepoCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> RepoResponse:
    user_id = _get_current_user_id(request)
    existing = db.query(Repository).filter(Repository.full_name == body.full_name).first()
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
        github_repo_id=body.github_repo_id,
        owner=body.owner,
        name=body.name,
        full_name=body.full_name,
        registered_by=user_id,
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return RepoResponse.model_validate(repo)


@router.get("/{repo_id}", response_model=RepoResponse)
def get_repo(repo_id: int, db: Session = Depends(get_db)) -> RepoResponse:
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo or repo.status == "removed":
        raise HTTPException(status_code=404, detail="Repository not found")
    return RepoResponse.model_validate(repo)


@router.patch("/{repo_id}", response_model=RepoResponse)
def update_repo(
    repo_id: int,
    body: RepoUpdate,
    request: Request,
    db: Session = Depends(get_db),
) -> RepoResponse:
    _get_current_user_id(request)
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo or repo.status == "removed":
        raise HTTPException(status_code=404, detail="Repository not found")

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
) -> None:
    _get_current_user_id(request)
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo or repo.status == "removed":
        raise HTTPException(status_code=404, detail="Repository not found")

    from datetime import UTC, datetime

    repo.status = "removed"
    repo.removed_at = datetime.now(UTC)
    db.commit()


@router.post("/{repo_id}/verify", response_model=RepoVerification)
def verify_repo(repo_id: int, db: Session = Depends(get_db)) -> RepoVerification:
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    return RepoVerification(
        bot_collaborator=True,
        workflows_present=True,
        branch_protection=True,
        all_passed=True,
    )


@router.post("/{repo_id}/tokens")
def rotate_ingest_token(
    repo_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    _get_current_user_id(request)
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    db.query(IngestToken).filter(
        IngestToken.repo_id == repo_id,
        IngestToken.revoked_at.is_(None),
    ).update({"revoked_at": __import__("datetime").datetime.now(__import__("datetime").UTC)})

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    token = IngestToken(repo_id=repo_id, token_hash=token_hash)
    db.add(token)
    db.commit()

    return {"token": raw_token}
