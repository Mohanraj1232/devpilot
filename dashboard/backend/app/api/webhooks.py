"""GitHub webhook receiver for status reconciliation.

Every delivery must carry a valid ``X-Hub-Signature-256`` HMAC of the raw body, computed
with the shared secret (DEVPILOT_WEBHOOK_SECRET). Without a configured secret the endpoint
refuses all traffic rather than trusting unauthenticated events.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import get_db
from app.models.tables import DevPilotExecution, Repository, WebhookDelivery

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger("dashboard.webhooks")


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


@router.post("/github")
async def github_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    secret = Settings().webhook_secret
    if not secret:
        raise HTTPException(status_code=503, detail="Webhook secret is not configured")

    raw = await request.body()
    if not verify_signature(secret, raw, request.headers.get("X-Hub-Signature-256", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    event = request.headers.get("X-GitHub-Event", "")
    if not delivery_id:
        raise HTTPException(status_code=400, detail="Missing delivery ID")

    try:
        body = json.loads(raw or b"{}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Body is not valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    # Idempotency: recorded only after the signature is verified, so a forged request can
    # never occupy a delivery ID.
    existing = db.query(WebhookDelivery).filter(WebhookDelivery.delivery_id == delivery_id).first()
    if existing:
        return {"status": "already_processed"}
    db.add(WebhookDelivery(delivery_id=delivery_id, event=event))

    repo = _registered_repo(db, body)
    if repo is None:
        db.commit()
        return {"status": "ignored"}

    if event == "pull_request":
        _handle_pr_event(body, repo, db)
    elif event == "issues":
        _handle_issue_event(body, repo, db)

    db.commit()
    return {"status": "processed"}


def _registered_repo(db: Session, body: dict[str, Any]) -> Repository | None:
    """The registered repository the event is about, or None (event is ignored)."""
    full_name = (body.get("repository") or {}).get("full_name")
    if not isinstance(full_name, str):
        return None
    return (
        db.query(Repository)
        .filter(Repository.full_name == full_name, Repository.status == "active")
        .first()
    )


def _finish(execution: DevPilotExecution, status: str, reason: str | None = None) -> None:
    execution.status = status
    if reason:
        execution.failure_reason = reason
    execution.finished_at = datetime.now(UTC)
    execution.lock_key = None


def _handle_pr_event(body: dict, repo: Repository, db: Session) -> None:
    action = body.get("action", "")
    pr = body.get("pull_request") or {}
    head = pr.get("head") or {}
    branch = str(head.get("ref", ""))

    # Only branches DevPilot pushed to this repository itself (not a fork's branch that
    # merely carries a devpilot/ name).
    if not branch.startswith("devpilot/"):
        return
    if (head.get("repo") or {}).get("full_name") != repo.full_name:
        return

    if action == "closed":
        status = "pr_merged" if pr.get("merged") else "pr_closed"
        execution = (
            db.query(DevPilotExecution)
            .filter(
                DevPilotExecution.repo_id == repo.id,
                DevPilotExecution.pr_number == pr.get("number"),
            )
            .first()
        )
        if execution:
            _finish(execution, status)


def _handle_issue_event(body: dict, repo: Repository, db: Session) -> None:
    action = body.get("action", "")
    issue = body.get("issue") or {}

    if action in ("closed", "deleted"):
        executions = (
            db.query(DevPilotExecution)
            .filter(
                DevPilotExecution.repo_id == repo.id,
                DevPilotExecution.issue_number == issue.get("number"),
                DevPilotExecution.status.in_(["queued", "running"]),
            )
            .all()
        )
        for execution in executions:
            _finish(execution, "cancelled", f"issue_{action}")
