"""Policy management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.tables import RepoPolicy, Repository
from app.schemas.policy import PolicyCreate, PolicyResponse

router = APIRouter(tags=["policy"])


def _get_current_user_id(request: Request) -> int:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


@router.get("/repos/{repo_id}/policy", response_model=list[PolicyResponse])
def list_policies(repo_id: int, db: Session = Depends(get_db)) -> list[PolicyResponse]:
    policies = (
        db.query(RepoPolicy)
        .filter(RepoPolicy.repo_id == repo_id)
        .order_by(RepoPolicy.version.desc())
        .all()
    )
    return [PolicyResponse.model_validate(p) for p in policies]


@router.put("/repos/{repo_id}/policy", response_model=PolicyResponse)
def create_policy(
    repo_id: int,
    body: PolicyCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> PolicyResponse:
    user_id = _get_current_user_id(request)
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo or repo.status == "removed":
        raise HTTPException(status_code=404, detail="Repository not found")

    max_version = (
        db.query(func.max(RepoPolicy.version))
        .filter(RepoPolicy.repo_id == repo_id)
        .scalar()
    )
    next_version = (max_version or 0) + 1

    policy = RepoPolicy(
        repo_id=repo_id,
        version=next_version,
        policy_json=body.policy_json,
        created_by=user_id,
    )
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return PolicyResponse.model_validate(policy)


@router.get("/policy/{owner}/{repo}", response_model=PolicyResponse)
def get_latest_policy(
    owner: str,
    repo: str,
    db: Session = Depends(get_db),
) -> PolicyResponse:
    full_name = f"{owner}/{repo}"
    repo_obj = (
        db.query(Repository).filter(Repository.full_name == full_name).first()
    )
    if not repo_obj or repo_obj.status != "active":
        raise HTTPException(status_code=404, detail="Repository not found or inactive")

    policy = (
        db.query(RepoPolicy)
        .filter(RepoPolicy.repo_id == repo_obj.id)
        .order_by(RepoPolicy.version.desc())
        .first()
    )
    if not policy:
        raise HTTPException(status_code=404, detail="No policy found")

    return PolicyResponse.model_validate(policy)
