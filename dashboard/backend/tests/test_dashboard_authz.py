"""Read-side authorisation (per-repository scoping) and webhook authenticity."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.tables import (
    DevPilotExecution,
    Finding,
    Repository,
    ReviewRun,
    User,
    WebhookDelivery,
)
from tests.conftest import WEBHOOK_SECRET, login, sign_webhook


def _user(db: Session, github_id: int, name: str) -> User:
    user = User(github_id=github_id, login=name)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _repo(db: Session, owner: User, full_name: str, repo_id: int) -> Repository:
    org, name = full_name.split("/")
    repo = Repository(
        github_repo_id=repo_id, owner=org, name=name, full_name=full_name, registered_by=owner.id
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return repo


def _run(db: Session, repo: Repository, run_id: int = 1) -> ReviewRun:
    run = ReviewRun(
        repo_id=repo.id,
        pr_number=1,
        head_sha="abc",
        workflow_run_id=run_id,
        status="completed",
        gate_result="FAIL",
        gate_reasons={"reasons": ["1 critical finding(s)"]},
        started_at=datetime.now(UTC),
    )
    db.add(run)
    db.flush()
    db.add(
        Finding(
            review_run_id=run.id,
            source="static",
            tool="ruff",
            category="bug",
            severity="high",
            file="secret_module.py",
            title="problem in private code",
            fingerprint="f" * 16,
            resolution="open",
        )
    )
    db.commit()
    db.refresh(run)
    return run


def _execution(db: Session, repo: Repository, run_id: int = 1, **fields: Any) -> DevPilotExecution:
    values: dict[str, Any] = {
        "repo_id": repo.id,
        "issue_number": 7,
        "issue_hash": "h",
        "base_sha": "s",
        "workflow_run_id": run_id,
        "status": "running",
        "lock_key": f"devpilot:{repo.full_name}:issue-7",
    }
    values.update(fields)
    execution = DevPilotExecution(**values)
    db.add(execution)
    db.commit()
    db.refresh(execution)
    return execution


@pytest.fixture
def two_tenants(db_session: Session) -> dict[str, Any]:
    """Alice owns org/alpha, Bob owns org/beta; each has a review run and an execution."""
    alice = _user(db_session, 1, "alice")
    bob = _user(db_session, 2, "bob")
    alpha = _repo(db_session, alice, "org/alpha", 100)
    beta = _repo(db_session, bob, "org/beta", 200)
    return {
        "alice": alice,
        "bob": bob,
        "alpha": alpha,
        "beta": beta,
        "alpha_run": _run(db_session, alpha, 1),
        "beta_run": _run(db_session, beta, 2),
        "alpha_exec": _execution(db_session, alpha, 1),
        "beta_exec": _execution(db_session, beta, 2),
    }


# ── nothing is readable without a login ──────────────────────

READ_ROUTES = [
    "/api/v1/repos",
    "/api/v1/repos/{alpha}",
    "/api/v1/repos/{alpha}/review-runs",
    "/api/v1/repos/{alpha}/devpilot-executions",
    "/api/v1/repos/{alpha}/policy",
    "/api/v1/review-runs/{run}",
    "/api/v1/devpilot-executions/{exec}",
    "/api/v1/analytics/findings-over-time",
    "/api/v1/analytics/categories",
    "/api/v1/analytics/gate-failures",
    "/api/v1/analytics/devpilot-success",
    "/api/v1/analytics/repo-trends",
]


def _fill(path: str, t: dict[str, Any]) -> str:
    return path.format(alpha=t["alpha"].id, run=t["alpha_run"].id, exec=t["alpha_exec"].id)


@pytest.mark.parametrize("route", READ_ROUTES)
def test_anonymous_cannot_read(
    route: str, client: TestClient, two_tenants: dict[str, Any]
) -> None:
    assert client.get(_fill(route, two_tenants)).status_code == 401


def test_a_session_for_a_deleted_user_is_rejected(client: TestClient, db_session: Session) -> None:
    login(client, 4242)  # signed correctly, but no such user
    assert client.get("/api/v1/repos").status_code == 401


# ── tenants cannot see each other ────────────────────────────


class TestTenantIsolation:
    def test_repo_list_only_contains_own_repositories(
        self, client: TestClient, two_tenants: dict[str, Any]
    ) -> None:
        login(client, two_tenants["alice"].id)
        names = [r["full_name"] for r in client.get("/api/v1/repos").json()]
        assert names == ["org/alpha"]

    def test_other_tenants_repo_is_a_404_not_a_403(
        self, client: TestClient, two_tenants: dict[str, Any]
    ) -> None:
        login(client, two_tenants["alice"].id)
        beta = two_tenants["beta"].id
        for path in (
            f"/api/v1/repos/{beta}",
            f"/api/v1/repos/{beta}/review-runs",
            f"/api/v1/repos/{beta}/devpilot-executions",
            f"/api/v1/repos/{beta}/policy",
            f"/api/v1/review-runs/{two_tenants['beta_run'].id}",
            f"/api/v1/devpilot-executions/{two_tenants['beta_exec'].id}",
        ):
            assert client.get(path).status_code == 404, path

    def test_own_data_is_readable(self, client: TestClient, two_tenants: dict[str, Any]) -> None:
        login(client, two_tenants["alice"].id)
        run = client.get(f"/api/v1/review-runs/{two_tenants['alpha_run'].id}").json()
        assert run["gate_result"] == "FAIL"
        assert client.get(f"/api/v1/devpilot-executions/{two_tenants['alpha_exec'].id}").status_code == 200

    def test_removed_repositories_disappear(
        self, client: TestClient, db_session: Session, two_tenants: dict[str, Any]
    ) -> None:
        two_tenants["alpha"].status = "removed"
        db_session.commit()
        login(client, two_tenants["alice"].id)
        assert client.get("/api/v1/repos").json() == []
        assert client.get(f"/api/v1/repos/{two_tenants['alpha'].id}/review-runs").status_code == 404

    def test_analytics_only_count_own_repositories(
        self, client: TestClient, two_tenants: dict[str, Any]
    ) -> None:
        login(client, two_tenants["alice"].id)
        assert [c["count"] for c in client.get("/api/v1/analytics/categories").json()] == [1]
        trends = client.get("/api/v1/analytics/repo-trends").json()
        assert [t["full_name"] for t in trends] == ["org/alpha"]
        assert client.get("/api/v1/analytics/devpilot-success").json()["total"] == 1
        assert client.get("/api/v1/analytics/gate-failures").json() == [
            {"reason": "1 critical finding(s)", "count": 1}
        ]

    def test_analytics_for_someone_elses_repo_is_a_404(
        self, client: TestClient, two_tenants: dict[str, Any]
    ) -> None:
        login(client, two_tenants["alice"].id)
        beta = two_tenants["beta"].id
        for route in ("findings-over-time", "categories", "devpilot-success"):
            assert client.get(f"/api/v1/analytics/{route}?repo_id={beta}").status_code == 404

    def test_user_with_no_repositories_sees_nothing(
        self, client: TestClient, db_session: Session, two_tenants: dict[str, Any]
    ) -> None:
        login(client, _user(db_session, 3, "carol").id)
        assert client.get("/api/v1/repos").json() == []
        assert client.get("/api/v1/analytics/devpilot-success").json()["total"] == 0
        assert client.get("/api/v1/analytics/repo-trends").json() == []


class TestFindingResolution:
    """PATCH /findings/{id} used to be a completely unauthenticated write."""

    def _finding_id(self, db: Session, run: ReviewRun) -> int:
        return db.query(Finding).filter(Finding.review_run_id == run.id).one().id

    def test_anonymous_cannot_change_a_finding(
        self, client: TestClient, db_session: Session, two_tenants: dict[str, Any]
    ) -> None:
        fid = self._finding_id(db_session, two_tenants["alpha_run"])
        assert client.patch(f"/api/v1/findings/{fid}", json={"resolution": "dismissed"}).status_code == 401
        assert db_session.get(Finding, fid).resolution == "open"

    def test_other_tenant_cannot_change_a_finding(
        self, client: TestClient, db_session: Session, two_tenants: dict[str, Any]
    ) -> None:
        fid = self._finding_id(db_session, two_tenants["alpha_run"])
        login(client, two_tenants["bob"].id)
        resp = client.patch(f"/api/v1/findings/{fid}", json={"resolution": "dismissed"})
        assert resp.status_code == 404
        assert db_session.get(Finding, fid).resolution == "open"

    def test_owner_can_change_and_invalid_values_are_rejected(
        self, client: TestClient, db_session: Session, two_tenants: dict[str, Any]
    ) -> None:
        fid = self._finding_id(db_session, two_tenants["alpha_run"])
        login(client, two_tenants["alice"].id)
        assert client.patch(f"/api/v1/findings/{fid}", json={"resolution": "accepted"}).json()["resolution"] == "accepted"
        assert client.patch(f"/api/v1/findings/{fid}", json={"resolution": "bogus"}).status_code == 400


# ── webhook authenticity ─────────────────────────────────────


def _pr_closed(repo: str, number: int, *, merged: bool, head_repo: str | None = None) -> dict[str, Any]:
    return {
        "action": "closed",
        "repository": {"full_name": repo},
        "pull_request": {
            "number": number,
            "merged": merged,
            "head": {"ref": "devpilot/issue-7-fix", "repo": {"full_name": head_repo or repo}},
        },
    }


class TestWebhookAuthenticity:
    def test_unsigned_request_is_rejected(
        self, client: TestClient, webhook: Any, db_session: Session
    ) -> None:
        resp = webhook("ping", {}, signature="")
        assert resp.status_code == 401
        assert db_session.query(WebhookDelivery).count() == 0  # nothing recorded

    def test_wrong_secret_is_rejected(self, client: TestClient, webhook: Any) -> None:
        body = json.dumps({}).encode()
        resp = webhook("ping", {}, raw=body, signature=sign_webhook(body, "other-secret"))
        assert resp.status_code == 401

    def test_signature_must_cover_the_exact_body(self, client: TestClient, webhook: Any) -> None:
        signed_for = json.dumps({"a": 1}).encode()
        resp = webhook(
            "ping", {}, raw=json.dumps({"a": 2}).encode(), signature=sign_webhook(signed_for)
        )
        assert resp.status_code == 401

    def test_missing_secret_configuration_refuses_everything(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DEVPILOT_WEBHOOK_SECRET", raising=False)
        body = b"{}"
        resp = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Delivery": "d",
                "X-GitHub-Event": "ping",
                "X-Hub-Signature-256": sign_webhook(body, ""),
            },
        )
        assert resp.status_code == 503

    def test_forged_delivery_cannot_occupy_a_delivery_id(
        self, client: TestClient, webhook: Any, db_session: Session
    ) -> None:
        webhook("ping", {}, delivery_id="victim", signature="sha256=00")
        assert db_session.query(WebhookDelivery).count() == 0
        assert webhook("ping", {}, delivery_id="victim").json()["status"] == "ignored"  # not "already_processed"

    def test_invalid_json_is_a_400(self, client: TestClient, webhook: Any) -> None:
        assert webhook("ping", {}, raw=b"{not json").status_code == 400

    def test_non_object_json_is_a_400(self, client: TestClient, webhook: Any) -> None:
        assert webhook("ping", {}, raw=b"[1, 2]").status_code == 400


class TestWebhookScoping:
    def test_merged_pr_finishes_the_execution_and_frees_the_lock(
        self, client: TestClient, webhook: Any, db_session: Session, two_tenants: dict[str, Any]
    ) -> None:
        execution = two_tenants["alpha_exec"]
        execution.pr_number = 11
        db_session.commit()
        resp = webhook("pull_request", _pr_closed("org/alpha", 11, merged=True))
        assert resp.json()["status"] == "processed"
        db_session.refresh(execution)
        assert execution.status == "pr_merged"
        assert execution.lock_key is None
        assert execution.finished_at is not None

    def test_closed_unmerged_pr(
        self, client: TestClient, webhook: Any, db_session: Session, two_tenants: dict[str, Any]
    ) -> None:
        execution = two_tenants["alpha_exec"]
        execution.pr_number = 11
        db_session.commit()
        webhook("pull_request", _pr_closed("org/alpha", 11, merged=False))
        db_session.refresh(execution)
        assert execution.status == "pr_closed"

    def test_event_for_another_repo_does_not_touch_this_repos_execution(
        self, client: TestClient, webhook: Any, db_session: Session, two_tenants: dict[str, Any]
    ) -> None:
        # Both repos have PR #11; an event for beta must only affect beta.
        for key in ("alpha_exec", "beta_exec"):
            two_tenants[key].pr_number = 11
        db_session.commit()
        webhook("pull_request", _pr_closed("org/beta", 11, merged=True))
        db_session.refresh(two_tenants["alpha_exec"])
        db_session.refresh(two_tenants["beta_exec"])
        assert two_tenants["alpha_exec"].status == "running"
        assert two_tenants["alpha_exec"].lock_key is not None
        assert two_tenants["beta_exec"].status == "pr_merged"

    def test_fork_branch_named_like_devpilot_is_ignored(
        self, client: TestClient, webhook: Any, db_session: Session, two_tenants: dict[str, Any]
    ) -> None:
        execution = two_tenants["alpha_exec"]
        execution.pr_number = 11
        db_session.commit()
        webhook("pull_request", _pr_closed("org/alpha", 11, merged=True, head_repo="evil/fork"))
        db_session.refresh(execution)
        assert execution.status == "running"

    def test_unregistered_repository_is_ignored(
        self, client: TestClient, webhook: Any, two_tenants: dict[str, Any]
    ) -> None:
        resp = webhook("pull_request", _pr_closed("stranger/repo", 11, merged=True))
        assert resp.json()["status"] == "ignored"

    def test_removed_repository_is_ignored(
        self, client: TestClient, webhook: Any, db_session: Session, two_tenants: dict[str, Any]
    ) -> None:
        two_tenants["alpha"].status = "removed"
        db_session.commit()
        assert webhook("pull_request", _pr_closed("org/alpha", 11, merged=True)).json()["status"] == "ignored"

    def test_event_without_a_repository_is_ignored(self, client: TestClient, webhook: Any) -> None:
        assert webhook("issues", {"action": "closed", "issue": {"number": 7}}).json()["status"] == "ignored"

    def test_issue_closed_cancels_only_this_repos_active_executions(
        self, client: TestClient, webhook: Any, db_session: Session, two_tenants: dict[str, Any]
    ) -> None:
        # Both repos have an active execution for issue #7.
        webhook("issues", {"action": "closed", "repository": {"full_name": "org/alpha"}, "issue": {"number": 7}})
        db_session.refresh(two_tenants["alpha_exec"])
        db_session.refresh(two_tenants["beta_exec"])
        assert two_tenants["alpha_exec"].status == "cancelled"
        assert two_tenants["alpha_exec"].failure_reason == "issue_closed"
        assert two_tenants["alpha_exec"].lock_key is None
        assert two_tenants["beta_exec"].status == "running"

    def test_finished_executions_are_not_cancelled(
        self, client: TestClient, webhook: Any, db_session: Session, two_tenants: dict[str, Any]
    ) -> None:
        two_tenants["alpha_exec"].status = "pr_opened"
        db_session.commit()
        webhook("issues", {"action": "deleted", "repository": {"full_name": "org/alpha"}, "issue": {"number": 7}})
        db_session.refresh(two_tenants["alpha_exec"])
        assert two_tenants["alpha_exec"].status == "pr_opened"

    def test_replayed_delivery_is_processed_once(
        self, client: TestClient, webhook: Any, db_session: Session, two_tenants: dict[str, Any]
    ) -> None:
        payload = {"action": "closed", "repository": {"full_name": "org/alpha"}, "issue": {"number": 7}}
        assert webhook("issues", payload, delivery_id="same").json()["status"] == "processed"
        two_tenants["alpha_exec"].status = "running"
        db_session.commit()
        assert webhook("issues", payload, delivery_id="same").json()["status"] == "already_processed"
        db_session.refresh(two_tenants["alpha_exec"])
        assert two_tenants["alpha_exec"].status == "running"  # the replay changed nothing


def test_secret_constant_is_what_the_fixture_uses() -> None:
    assert WEBHOOK_SECRET == "test-webhook-secret"
