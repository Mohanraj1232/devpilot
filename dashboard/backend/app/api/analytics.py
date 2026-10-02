"""Analytics query routes (login required; computed only over the user's repositories)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_visible_repo, require_user, visible_repo_ids
from app.database import get_db
from app.models.tables import DevPilotExecution, Finding, Repository, ReviewRun, User
from app.schemas.analytics import (
    CategoryCount,
    DevPilotSuccessRate,
    GateFailureReason,
    RepoTrend,
    TimeSeriesPoint,
)

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _scope(db: Session, user: User, repo_id: int | None) -> list[int]:
    """The repositories to aggregate over: one the user owns, or all of theirs."""
    if repo_id:
        return [get_visible_repo(db, user, repo_id).id]
    return visible_repo_ids(db, user)


@router.get("/findings-over-time", response_model=list[TimeSeriesPoint])
def findings_over_time(
    repo_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
) -> list[TimeSeriesPoint]:
    repo_ids = _scope(db, user, repo_id)
    results = (
        db.query(
            func.date(ReviewRun.started_at).label("date"),
            func.count(Finding.id).label("count"),
        )
        .join(Finding, Finding.review_run_id == ReviewRun.id)
        .filter(ReviewRun.repo_id.in_(repo_ids))
        .group_by(func.date(ReviewRun.started_at))
        .all()
    )
    return [TimeSeriesPoint(date=r.date, count=r.count) for r in results]


@router.get("/categories", response_model=list[CategoryCount])
def finding_categories(
    repo_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
) -> list[CategoryCount]:
    repo_ids = _scope(db, user, repo_id)
    results = (
        db.query(Finding.category, func.count(Finding.id).label("count"))
        .join(ReviewRun, Finding.review_run_id == ReviewRun.id)
        .filter(ReviewRun.repo_id.in_(repo_ids))
        .group_by(Finding.category)
        .all()
    )
    return [CategoryCount(category=r.category, count=r.count) for r in results]


@router.get("/gate-failures", response_model=list[GateFailureReason])
def gate_failures(
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
) -> list[GateFailureReason]:
    runs = (
        db.query(ReviewRun)
        .filter(ReviewRun.gate_result == "FAIL", ReviewRun.repo_id.in_(visible_repo_ids(db, user)))
        .all()
    )

    reason_counts: dict[str, int] = {}
    for run in runs:
        reasons = run.gate_reasons or {}
        for reason in reasons.get("reasons", []):
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    return [
        GateFailureReason(reason=r, count=c)
        for r, c in sorted(reason_counts.items(), key=lambda x: -x[1])
    ]


@router.get("/devpilot-success", response_model=DevPilotSuccessRate)
def devpilot_success_rate(
    repo_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
) -> DevPilotSuccessRate:
    query = db.query(DevPilotExecution).filter(
        DevPilotExecution.repo_id.in_(_scope(db, user, repo_id))
    )

    total = query.count()
    successful = query.filter(DevPilotExecution.status.in_(["pr_opened", "pr_merged"])).count()

    rate = (successful / total * 100) if total > 0 else 0.0
    return DevPilotSuccessRate(total=total, successful=successful, rate=round(rate, 1))


@router.get("/repo-trends", response_model=list[RepoTrend])
def repo_trends(
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
) -> list[RepoTrend]:
    repos = db.query(Repository).filter(Repository.id.in_(visible_repo_ids(db, user))).all()
    trends = []

    for repo in repos:
        total = db.query(ReviewRun).filter(ReviewRun.repo_id == repo.id).count()
        passed = (
            db.query(ReviewRun)
            .filter(ReviewRun.repo_id == repo.id, ReviewRun.gate_result == "PASS")
            .count()
        )
        rate = (passed / total * 100) if total > 0 else 0.0
        trends.append(
            RepoTrend(
                repo_id=repo.id,
                full_name=repo.full_name,
                total_runs=total,
                pass_rate=round(rate, 1),
            )
        )

    return trends
