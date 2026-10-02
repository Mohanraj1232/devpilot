"""GitHub webhook receiver for status reconciliation."""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import get_db
from app.models.tables import DevPilotExecution, WebhookDelivery

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger("dashboard.webhooks")
settings = Settings()


@router.post("/github")
async def github_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    event = request.headers.get("X-GitHub-Event", "")

    if not delivery_id:
        raise HTTPException(status_code=400, detail="Missing delivery ID")

    existing = (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.delivery_id == delivery_id)
        .first()
    )
    if existing:
        return {"status": "already_processed"}

    db.add(WebhookDelivery(delivery_id=delivery_id, event=event))

    body = await request.json()

    if event == "pull_request":
        _handle_pr_event(body, db)
    elif event == "issues":
        _handle_issue_event(body, db)

    db.commit()
    return {"status": "processed"}


def _handle_pr_event(body: dict, db: Session) -> None:
    action = body.get("action", "")
    pr = body.get("pull_request", {})
    branch = pr.get("head", {}).get("ref", "")

    if not branch.startswith("devpilot/"):
        return

    pr_number = pr.get("number")

    if action == "closed":
        merged = pr.get("merged", False)
        status = "pr_merged" if merged else "pr_closed"

        execution = (
            db.query(DevPilotExecution)
            .filter(DevPilotExecution.pr_number == pr_number)
            .first()
        )
        if execution:
            from datetime import UTC, datetime

            execution.status = status
            execution.finished_at = datetime.now(UTC)
            execution.lock_key = None


def _handle_issue_event(body: dict, db: Session) -> None:
    action = body.get("action", "")
    issue = body.get("issue", {})
    issue_number = issue.get("number")

    if action in ("closed", "deleted"):
        executions = (
            db.query(DevPilotExecution)
            .filter(
                DevPilotExecution.issue_number == issue_number,
                DevPilotExecution.status.in_(["queued", "running"]),
            )
            .all()
        )
        for execution in executions:
            from datetime import UTC, datetime

            execution.status = "cancelled"
            execution.failure_reason = f"issue_{action}"
            execution.finished_at = datetime.now(UTC)
            execution.lock_key = None
