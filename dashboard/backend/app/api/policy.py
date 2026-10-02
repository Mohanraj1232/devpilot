"""Policy management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import ensure_token_repo, get_ingest_token
from app.database import get_db
from app.models.tables import IngestToken, RepoPolicy, Repository
from app.schemas.policy import PolicyCreate, PolicyResponse, WorkflowPolicyResponse

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


@router.get("/policy/{owner}/{repo}", response_model=WorkflowPolicyResponse)
def get_latest_policy(
    owner: str,
    repo: str,
    db: Session = Depends(get_db),
    token: IngestToken = Depends(get_ingest_token),
) -> WorkflowPolicyResponse:
    full_name = f"{owner}/{repo}"
    repo_obj = (
        db.query(Repository).filter(Repository.full_name == full_name).first()
    )
    if not repo_obj or repo_obj.status != "active":
        raise HTTPException(status_code=404, detail="Repository not found or inactive")
    # Workflow-facing: only the repository's own token may read its policy.
    ensure_token_repo(token, repo_obj.id)

    policy = (
        db.query(RepoPolicy)
        .filter(RepoPolicy.repo_id == repo_obj.id)
        .order_by(RepoPolicy.version.desc())
        .first()
    )
    # A registered repository without a custom policy is still registered.
    return WorkflowPolicyResponse(
        repo_id=repo_obj.id,
        version=policy.version if policy else 0,
        policy_json=policy.policy_json if policy else {},
        review_enabled=repo_obj.review_enabled,
        devpilot_enabled=repo_obj.devpilot_enabled,
    )
