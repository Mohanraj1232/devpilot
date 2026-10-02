"""DevPilot execution monitoring routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.tables import DevPilotExecution

router = APIRouter(tags=["executions"])


@router.get("/repos/{repo_id}/devpilot-executions")
def list_executions(repo_id: int, db: Session = Depends(get_db)) -> list[dict]:
    executions = (
        db.query(DevPilotExecution)
        .filter(DevPilotExecution.repo_id == repo_id)
        .order_by(DevPilotExecution.started_at.desc())
        .all()
    )
    return [
        {
            "id": e.id,
            "issue_number": e.issue_number,
            "status": e.status,
            "branch": e.branch,
            "pr_number": e.pr_number,
            "attempts": e.attempts,
            "test_status": e.test_status,
            "failure_reason": e.failure_reason,
            "started_at": e.started_at.isoformat() if e.started_at else None,
            "finished_at": e.finished_at.isoformat() if e.finished_at else None,
        }
        for e in executions
    ]


@router.get("/devpilot-executions/{execution_id}")
def get_execution(execution_id: int, db: Session = Depends(get_db)) -> dict:
    execution = (
        db.query(DevPilotExecution)
        .filter(DevPilotExecution.id == execution_id)
        .first()
    )
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")

    attempts = [
        {
            "attempt_no": a.attempt_no,
            "diff_hash": a.diff_hash,
            "test_status": a.test_status,
            "summary": a.summary,
        }
        for a in execution.attempt_list
    ]

    return {
        "id": execution.id,
        "issue_number": execution.issue_number,
        "issue_hash": execution.issue_hash,
        "base_sha": execution.base_sha,
        "status": execution.status,
        "branch": execution.branch,
        "pr_number": execution.pr_number,
        "attempts": execution.attempts,
        "attempt_list": attempts,
        "test_status": execution.test_status,
        "failure_reason": execution.failure_reason,
        "started_at": execution.started_at.isoformat() if execution.started_at else None,
        "finished_at": execution.finished_at.isoformat() if execution.finished_at else None,
    }
