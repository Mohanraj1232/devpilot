"""Review run monitoring routes (login required; scoped to the user's repositories)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_visible_repo, require_user, visible_repo_ids
from app.database import get_db
from app.models.tables import Finding, ReviewRun, User
from app.schemas.reviews import (
    FindingResponse,
    FindingUpdate,
    ReviewRunDetail,
    ReviewRunResponse,
)

router = APIRouter(tags=["reviews"])


@router.get("/repos/{repo_id}/review-runs", response_model=list[ReviewRunResponse])
def list_review_runs(
    repo_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
) -> list[ReviewRunResponse]:
    get_visible_repo(db, user, repo_id)
    runs = (
        db.query(ReviewRun)
        .filter(ReviewRun.repo_id == repo_id)
        .order_by(ReviewRun.started_at.desc())
        .all()
    )
    return [ReviewRunResponse.model_validate(r) for r in runs]


@router.get("/review-runs/{run_id}", response_model=ReviewRunDetail)
def get_review_run(
    run_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
) -> ReviewRunDetail:
    run = (
        db.query(ReviewRun)
        .filter(ReviewRun.id == run_id, ReviewRun.repo_id.in_(visible_repo_ids(db, user)))
        .first()
    )
    if not run:
        raise HTTPException(status_code=404, detail="Review run not found")
    return ReviewRunDetail.model_validate(run)


@router.patch("/findings/{finding_id}", response_model=FindingResponse)
def update_finding(
    finding_id: int,
    body: FindingUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
) -> FindingResponse:
    finding = (
        db.query(Finding)
        .join(ReviewRun, Finding.review_run_id == ReviewRun.id)
        .filter(Finding.id == finding_id, ReviewRun.repo_id.in_(visible_repo_ids(db, user)))
        .first()
    )
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")

    valid_resolutions = {"open", "accepted", "dismissed", "fixed"}
    if body.resolution not in valid_resolutions:
        raise HTTPException(status_code=400, detail="Invalid resolution")

    finding.resolution = body.resolution
    db.commit()
    db.refresh(finding)
    return FindingResponse.model_validate(finding)
