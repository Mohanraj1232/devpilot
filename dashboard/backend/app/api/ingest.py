"""Ingest routes for workflow data submission."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.tables import (
    CheckResult,
    DevPilotExecution,
    Finding,
    Repository,
    ReviewRun,
)
from app.schemas.ingest import (
    ExecutionIngest,
    ExecutionUpdate,
    LockRequest,
    LockResponse,
    ReviewRunIngest,
)

router = APIRouter(prefix="/ingest", tags=["ingest"])


def _get_repo_by_name(db: Session, full_name: str) -> Repository:
    repo = db.query(Repository).filter(Repository.full_name == full_name).first()
    if not repo or repo.status != "active":
        raise HTTPException(status_code=404, detail="Repository not found or inactive")
    return repo


@router.post("/review-runs", status_code=201)
def ingest_review_run(body: ReviewRunIngest, db: Session = Depends(get_db)) -> dict:
    repo = _get_repo_by_name(db, body.repo_full_name)

    existing = (
        db.query(ReviewRun)
        .filter(ReviewRun.workflow_run_id == body.workflow_run_id)
        .first()
    )

    if existing:
        existing.status = body.status
        existing.risk_score = body.risk_score
        existing.quality_score = body.quality_score
        existing.gate_result = body.gate_result
        existing.gate_reasons = body.gate_reasons
        existing.finished_at = body.finished_at or datetime.now(UTC)
        db.commit()
        return {"id": existing.id, "action": "updated"}

    run = ReviewRun(
        repo_id=repo.id,
        pr_number=body.pr_number,
        head_sha=body.head_sha,
        workflow_run_id=body.workflow_run_id,
        config_version=body.config_version,
        status=body.status,
        risk_score=body.risk_score,
        quality_score=body.quality_score,
        gate_result=body.gate_result,
        gate_reasons=body.gate_reasons,
        started_at=body.started_at or datetime.now(UTC),
        finished_at=body.finished_at,
    )
    db.add(run)
    db.flush()

    if body.checks:
        for check in body.checks:
            db.add(
                CheckResult(
                    review_run_id=run.id,
                    name=check.name,
                    status=check.status,
                    summary=check.summary,
                    duration_ms=check.duration_ms,
                )
            )

    if body.findings:
        for finding in body.findings:
            db.add(
                Finding(
                    review_run_id=run.id,
                    source=finding.source,
                    tool=finding.tool,
                    rule_id=finding.rule_id,
                    category=finding.category,
                    severity=finding.severity,
                    file=finding.file,
                    line_start=finding.line_start,
                    line_end=finding.line_end,
                    title=finding.title,
                    explanation=finding.explanation,
                    suggested_fix=finding.suggested_fix,
                    fingerprint=finding.fingerprint,
                    resolution=finding.resolution,
                )
            )

    db.commit()
    return {"id": run.id, "action": "created"}


@router.post("/devpilot-executions", status_code=201)
def ingest_execution(body: ExecutionIngest, db: Session = Depends(get_db)) -> dict:
    repo = _get_repo_by_name(db, body.repo_full_name)

    existing = (
        db.query(DevPilotExecution)
        .filter(DevPilotExecution.workflow_run_id == body.workflow_run_id)
        .first()
    )
    if existing:
        return {"id": existing.id, "action": "already_exists"}

    execution = DevPilotExecution(
        repo_id=repo.id,
        issue_number=body.issue_number,
        issue_hash=body.issue_hash,
        base_sha=body.base_sha,
        workflow_run_id=body.workflow_run_id,
        config_version=body.config_version,
        status=body.status,
        branch=body.branch,
        pr_number=body.pr_number,
        attempts=body.attempts,
        test_status=body.test_status,
        failure_reason=body.failure_reason,
    )
    db.add(execution)
    db.commit()
    return {"id": execution.id, "action": "created"}


@router.patch("/devpilot-executions/{execution_id}")
def update_execution(
    execution_id: int,
    body: ExecutionUpdate,
    db: Session = Depends(get_db),
) -> dict:
    execution = (
        db.query(DevPilotExecution)
        .filter(DevPilotExecution.id == execution_id)
        .first()
    )
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")

    execution.status = body.status
    if body.branch is not None:
        execution.branch = body.branch
    if body.pr_number is not None:
        execution.pr_number = body.pr_number
    if body.attempts is not None:
        execution.attempts = body.attempts
    if body.test_status is not None:
        execution.test_status = body.test_status
    if body.failure_reason is not None:
        execution.failure_reason = body.failure_reason

    terminal_statuses = {"pr_merged", "pr_closed", "failed", "cancelled"}
    if body.status in terminal_statuses:
        execution.finished_at = datetime.now(UTC)
        execution.lock_key = None

    db.commit()
    return {"id": execution.id, "status": execution.status}


@router.post("/locks/devpilot", response_model=LockResponse)
def acquire_lock(body: LockRequest, db: Session = Depends(get_db)) -> LockResponse:
    lock_key = f"devpilot:{body.repo_full_name}:issue-{body.issue_number}"

    existing = (
        db.query(DevPilotExecution)
        .filter(DevPilotExecution.lock_key == lock_key)
        .first()
    )
    if existing:
        return LockResponse(lock_key=lock_key, acquired=False)

    return LockResponse(lock_key=lock_key, acquired=True)


@router.delete("/locks/devpilot/{lock_key}")
def release_lock(lock_key: str, db: Session = Depends(get_db)) -> dict:
    execution = (
        db.query(DevPilotExecution)
        .filter(DevPilotExecution.lock_key == lock_key)
        .first()
    )
    if execution:
        execution.lock_key = None
        db.commit()
    return {"released": True}
