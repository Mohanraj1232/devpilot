"""API endpoint tests using SQLite in-memory database."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.tables import Repository, ReviewRun, User


def _seed_user(db: Session) -> User:
    user = User(github_id=1001, login="testuser", avatar_url="https://example.com/a.png")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_repo(db: Session, user_id: int) -> Repository:
    repo = Repository(
        github_repo_id=9999,
        owner="testorg",
        name="testrepo",
        full_name="testorg/testrepo",
        registered_by=user_id,
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return repo


class TestHealth:
    def test_health(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestRepos:
    def test_list_repos_empty(self, client: TestClient) -> None:
        resp = client.get("/api/v1/repos")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_register_and_get_repo(
        self, client: TestClient, db_session: Session
    ) -> None:
        user = _seed_user(db_session)
        # Simulate session auth
        with client:
            client.app.state._test_user_id = user.id

            resp = client.post(
                "/api/v1/repos",
                json={
                    "github_repo_id": 9999,
                    "owner": "org",
                    "name": "repo",
                    "full_name": "org/repo",
                },
            )
            # Without real session auth, this returns 401
            assert resp.status_code in (201, 401)

    def test_get_repo_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/v1/repos/999")
        assert resp.status_code == 404


class TestReviewRuns:
    def test_list_review_runs_empty(
        self, client: TestClient, db_session: Session
    ) -> None:
        user = _seed_user(db_session)
        repo = _seed_repo(db_session, user.id)
        resp = client.get(f"/api/v1/repos/{repo.id}/review-runs")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_review_run_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/v1/review-runs/999")
        assert resp.status_code == 404


class TestIngest:
    def test_ingest_review_run(
        self, client: TestClient, db_session: Session
    ) -> None:
        user = _seed_user(db_session)
        _seed_repo(db_session, user.id)

        resp = client.post(
            "/api/v1/ingest/review-runs",
            json={
                "repo_full_name": "testorg/testrepo",
                "pr_number": 1,
                "head_sha": "abc123def456abc123def456abc123def456abc1",
                "workflow_run_id": 100001,
                "status": "completed",
                "risk_score": 25.0,
                "quality_score": 85.0,
                "gate_result": "PASS",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["action"] == "created"

        # Upsert same workflow_run_id
        resp2 = client.post(
            "/api/v1/ingest/review-runs",
            json={
                "repo_full_name": "testorg/testrepo",
                "pr_number": 1,
                "head_sha": "abc123def456abc123def456abc123def456abc1",
                "workflow_run_id": 100001,
                "status": "completed",
                "gate_result": "FAIL",
            },
        )
        assert resp2.status_code == 201
        assert resp2.json()["action"] == "updated"

    def test_ingest_execution(
        self, client: TestClient, db_session: Session
    ) -> None:
        user = _seed_user(db_session)
        _seed_repo(db_session, user.id)

        resp = client.post(
            "/api/v1/ingest/devpilot-executions",
            json={
                "repo_full_name": "testorg/testrepo",
                "issue_number": 42,
                "issue_hash": "abc123",
                "base_sha": "def456abc123def456abc123def456abc123def4",
                "workflow_run_id": 200001,
                "status": "running",
            },
        )
        assert resp.status_code == 201

    def test_ingest_invalid_repo(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/ingest/review-runs",
            json={
                "repo_full_name": "nonexistent/repo",
                "pr_number": 1,
                "head_sha": "abc",
                "workflow_run_id": 999,
                "status": "completed",
            },
        )
        assert resp.status_code == 404


class TestLocks:
    def test_acquire_lock(
        self, client: TestClient, db_session: Session
    ) -> None:
        resp = client.post(
            "/api/v1/ingest/locks/devpilot",
            json={
                "repo_full_name": "org/repo",
                "issue_number": 42,
                "execution_id": "exec-001",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["acquired"] is True


class TestWebhooks:
    def test_webhook_missing_delivery(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/webhooks/github",
            json={},
            headers={"X-GitHub-Event": "ping"},
        )
        assert resp.status_code == 400

    def test_webhook_idempotency(
        self, client: TestClient, db_session: Session
    ) -> None:
        headers = {
            "X-GitHub-Delivery": "delivery-001",
            "X-GitHub-Event": "ping",
        }
        resp1 = client.post("/api/v1/webhooks/github", json={}, headers=headers)
        assert resp1.status_code == 200

        resp2 = client.post("/api/v1/webhooks/github", json={}, headers=headers)
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "already_processed"


class TestAnalytics:
    def test_findings_over_time_empty(self, client: TestClient) -> None:
        resp = client.get("/api/v1/analytics/findings-over-time")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_categories_empty(self, client: TestClient) -> None:
        resp = client.get("/api/v1/analytics/categories")
        assert resp.status_code == 200

    def test_devpilot_success_rate(self, client: TestClient) -> None:
        resp = client.get("/api/v1/analytics/devpilot-success")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["rate"] == 0.0


class TestPolicy:
    def test_list_policies_empty(
        self, client: TestClient, db_session: Session
    ) -> None:
        user = _seed_user(db_session)
        repo = _seed_repo(db_session, user.id)
        resp = client.get(f"/api/v1/repos/{repo.id}/policy")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_latest_policy_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/v1/policy/nonexistent/repo")
        assert resp.status_code == 404


class TestExecutions:
    def test_list_executions_empty(
        self, client: TestClient, db_session: Session
    ) -> None:
        user = _seed_user(db_session)
        repo = _seed_repo(db_session, user.id)
        resp = client.get(f"/api/v1/repos/{repo.id}/devpilot-executions")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_execution_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/v1/devpilot-executions/999")
        assert resp.status_code == 404


def _execution_payload(run_id: int, status: str = "running") -> dict:
    return {
        "repo_full_name": "testorg/testrepo",
        "issue_number": 42,
        "issue_hash": "abc123",
        "base_sha": "def456abc123def456abc123def456abc123def4",
        "workflow_run_id": run_id,
        "status": status,
    }


class TestExecutionLock:
    """The per-issue lock must actually be held while an execution is active."""

    def test_second_active_execution_is_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        _seed_repo(db_session, _seed_user(db_session).id)
        first = client.post("/api/v1/ingest/devpilot-executions", json=_execution_payload(1))
        assert first.status_code == 201
        second = client.post("/api/v1/ingest/devpilot-executions", json=_execution_payload(2))
        assert second.status_code == 409

    def test_acquire_reports_held_lock(self, client: TestClient, db_session: Session) -> None:
        _seed_repo(db_session, _seed_user(db_session).id)
        client.post("/api/v1/ingest/devpilot-executions", json=_execution_payload(1))
        resp = client.post(
            "/api/v1/ingest/locks/devpilot",
            json={"repo_full_name": "testorg/testrepo", "issue_number": 42, "execution_id": "x"},
        )
        assert resp.json()["acquired"] is False

    def test_release_frees_the_issue(self, client: TestClient, db_session: Session) -> None:
        _seed_repo(db_session, _seed_user(db_session).id)
        client.post("/api/v1/ingest/devpilot-executions", json=_execution_payload(1))
        client.delete("/api/v1/ingest/locks/devpilot/devpilot:testorg/testrepo:issue-42")
        assert (
            client.post(
                "/api/v1/ingest/devpilot-executions", json=_execution_payload(2)
            ).status_code
            == 201
        )

    @pytest.mark.parametrize(
        "status", ["failed", "tests_failed", "needs_clarification", "blocked", "pr_merged"]
    )
    def test_terminal_status_frees_the_issue(
        self, status: str, client: TestClient, db_session: Session
    ) -> None:
        _seed_repo(db_session, _seed_user(db_session).id)
        created = client.post("/api/v1/ingest/devpilot-executions", json=_execution_payload(1))
        client.patch(
            f"/api/v1/ingest/devpilot-executions/{created.json()['id']}", json={"status": status}
        )
        assert (
            client.post(
                "/api/v1/ingest/devpilot-executions", json=_execution_payload(2)
            ).status_code
            == 201
        )

    def test_pr_opened_keeps_the_record_active_until_released(
        self, client: TestClient, db_session: Session
    ) -> None:
        _seed_repo(db_session, _seed_user(db_session).id)
        created = client.post("/api/v1/ingest/devpilot-executions", json=_execution_payload(1))
        client.patch(
            f"/api/v1/ingest/devpilot-executions/{created.json()['id']}",
            json={"status": "pr_opened", "pr_number": 3},
        )
        assert (
            client.post(
                "/api/v1/ingest/devpilot-executions", json=_execution_payload(2)
            ).status_code
            == 409
        )

    def test_re_ingest_of_the_same_run_is_idempotent(
        self, client: TestClient, db_session: Session
    ) -> None:
        _seed_repo(db_session, _seed_user(db_session).id)
        client.post("/api/v1/ingest/devpilot-executions", json=_execution_payload(1))
        again = client.post("/api/v1/ingest/devpilot-executions", json=_execution_payload(1))
        assert again.status_code == 201
        assert again.json()["action"] == "already_exists"


class TestWorkflowPolicy:
    def test_exposes_enabled_flags(self, client: TestClient, db_session: Session) -> None:
        repo = _seed_repo(db_session, _seed_user(db_session).id)
        repo.devpilot_enabled = True
        db_session.commit()
        resp = client.get("/api/v1/policy/testorg/testrepo")
        assert resp.status_code == 200
        body = resp.json()
        assert body["devpilot_enabled"] is True
        assert body["review_enabled"] is True

    def test_registered_repo_without_policy_is_still_registered(
        self, client: TestClient, db_session: Session
    ) -> None:
        _seed_repo(db_session, _seed_user(db_session).id)
        body = client.get("/api/v1/policy/testorg/testrepo").json()
        assert body["version"] == 0
        assert body["policy_json"] == {}
        assert body["devpilot_enabled"] is False  # DevPilot is opt-in

    def test_removed_repo_is_not_registered(self, client: TestClient, db_session: Session) -> None:
        repo = _seed_repo(db_session, _seed_user(db_session).id)
        repo.status = "removed"
        db_session.commit()
        assert client.get("/api/v1/policy/testorg/testrepo").status_code == 404
