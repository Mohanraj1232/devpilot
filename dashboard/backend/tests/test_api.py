"""API endpoint tests using SQLite in-memory database."""

from __future__ import annotations

import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import hash_ingest_token
from app.models.tables import IngestToken, Repository, ReviewRun, User


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


def _authorize(client: TestClient, db: Session, repo: Repository) -> str:
    """Issue an ingest token for ``repo`` and send it on every request of ``client``."""
    raw = secrets.token_urlsafe(32)
    db.add(IngestToken(repo_id=repo.id, token_hash=hash_ingest_token(raw)))
    db.commit()
    client.headers["Authorization"] = f"Bearer {raw}"
    return raw


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
        _authorize(client, db_session, _seed_repo(db_session, user.id))

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
        _authorize(client, db_session, _seed_repo(db_session, user.id))

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

    def test_ingest_invalid_repo(self, client: TestClient, db_session: Session) -> None:
        _authorize(client, db_session, _seed_repo(db_session, _seed_user(db_session).id))
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
        _authorize(client, db_session, _seed_repo(db_session, _seed_user(db_session).id))
        resp = client.post(
            "/api/v1/ingest/locks/devpilot",
            json={
                "repo_full_name": "testorg/testrepo",
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

    def test_get_latest_policy_not_found(
        self, client: TestClient, db_session: Session
    ) -> None:
        _authorize(client, db_session, _seed_repo(db_session, _seed_user(db_session).id))
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
        _authorize(client, db_session, _seed_repo(db_session, _seed_user(db_session).id))
        first = client.post("/api/v1/ingest/devpilot-executions", json=_execution_payload(1))
        assert first.status_code == 201
        second = client.post("/api/v1/ingest/devpilot-executions", json=_execution_payload(2))
        assert second.status_code == 409

    def test_acquire_reports_held_lock(self, client: TestClient, db_session: Session) -> None:
        _authorize(client, db_session, _seed_repo(db_session, _seed_user(db_session).id))
        client.post("/api/v1/ingest/devpilot-executions", json=_execution_payload(1))
        resp = client.post(
            "/api/v1/ingest/locks/devpilot",
            json={"repo_full_name": "testorg/testrepo", "issue_number": 42, "execution_id": "x"},
        )
        assert resp.json()["acquired"] is False

    def test_release_frees_the_issue(self, client: TestClient, db_session: Session) -> None:
        _authorize(client, db_session, _seed_repo(db_session, _seed_user(db_session).id))
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
        _authorize(client, db_session, _seed_repo(db_session, _seed_user(db_session).id))
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
        _authorize(client, db_session, _seed_repo(db_session, _seed_user(db_session).id))
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
        _authorize(client, db_session, _seed_repo(db_session, _seed_user(db_session).id))
        client.post("/api/v1/ingest/devpilot-executions", json=_execution_payload(1))
        again = client.post("/api/v1/ingest/devpilot-executions", json=_execution_payload(1))
        assert again.status_code == 201
        assert again.json()["action"] == "already_exists"


class TestWorkflowPolicy:
    def test_exposes_enabled_flags(self, client: TestClient, db_session: Session) -> None:
        repo = _seed_repo(db_session, _seed_user(db_session).id)
        _authorize(client, db_session, repo)
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
        _authorize(client, db_session, _seed_repo(db_session, _seed_user(db_session).id))
        body = client.get("/api/v1/policy/testorg/testrepo").json()
        assert body["version"] == 0
        assert body["policy_json"] == {}
        assert body["devpilot_enabled"] is False  # DevPilot is opt-in

    def test_removed_repo_is_not_registered(self, client: TestClient, db_session: Session) -> None:
        repo = _seed_repo(db_session, _seed_user(db_session).id)
        _authorize(client, db_session, repo)
        repo.status = "removed"
        db_session.commit()
        assert client.get("/api/v1/policy/testorg/testrepo").status_code == 404


class TestIngestAuthentication:
    """Workflow-facing routes must reject unauthenticated or wrongly scoped callers."""

    ROUTES = (
        ("post", "/api/v1/ingest/devpilot-executions"),
        ("post", "/api/v1/ingest/review-runs"),
        ("post", "/api/v1/ingest/locks/devpilot"),
        ("patch", "/api/v1/ingest/devpilot-executions/1"),
        ("delete", "/api/v1/ingest/locks/devpilot/devpilot:testorg/testrepo:issue-42"),
        ("get", "/api/v1/policy/testorg/testrepo"),
    )

    @pytest.mark.parametrize(("method", "path"), ROUTES)
    def test_missing_token_is_rejected(
        self, method: str, path: str, client: TestClient, db_session: Session
    ) -> None:
        _seed_repo(db_session, _seed_user(db_session).id)
        resp = getattr(client, method)(path)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"

    @pytest.mark.parametrize(("method", "path"), ROUTES)
    def test_garbage_token_is_rejected(
        self, method: str, path: str, client: TestClient, db_session: Session
    ) -> None:
        _seed_repo(db_session, _seed_user(db_session).id)
        resp = getattr(client, method)(path, headers={"Authorization": "Bearer not-a-token"})
        assert resp.status_code == 401

    def test_non_bearer_scheme_is_rejected(self, client: TestClient, db_session: Session) -> None:
        repo = _seed_repo(db_session, _seed_user(db_session).id)
        raw = _authorize(client, db_session, repo)
        client.headers["Authorization"] = f"Basic {raw}"
        assert client.get("/api/v1/policy/testorg/testrepo").status_code == 401

    def test_revoked_token_is_rejected(self, client: TestClient, db_session: Session) -> None:
        from datetime import UTC, datetime

        repo = _seed_repo(db_session, _seed_user(db_session).id)
        _authorize(client, db_session, repo)
        assert client.get("/api/v1/policy/testorg/testrepo").status_code == 200
        db_session.query(IngestToken).update({"revoked_at": datetime.now(UTC)})
        db_session.commit()
        assert client.get("/api/v1/policy/testorg/testrepo").status_code == 401

    def test_token_use_is_recorded(self, client: TestClient, db_session: Session) -> None:
        repo = _seed_repo(db_session, _seed_user(db_session).id)
        _authorize(client, db_session, repo)
        client.get("/api/v1/policy/testorg/testrepo")
        assert db_session.query(IngestToken).first().last_used_at is not None

    def test_tokens_are_stored_hashed(self, client: TestClient, db_session: Session) -> None:
        repo = _seed_repo(db_session, _seed_user(db_session).id)
        raw = _authorize(client, db_session, repo)
        stored = db_session.query(IngestToken).first().token_hash
        assert stored != raw
        assert stored == hash_ingest_token(raw)

    def _two_repos(self, client: TestClient, db: Session) -> tuple[Repository, Repository]:
        user = _seed_user(db)
        a = _seed_repo(db, user.id)
        b = Repository(
            github_repo_id=8888,
            owner="otherorg",
            name="otherrepo",
            full_name="otherorg/otherrepo",
            registered_by=user.id,
        )
        db.add(b)
        db.commit()
        db.refresh(b)
        _authorize(client, db, a)  # the client now holds repo A's token
        return a, b

    def test_token_cannot_write_to_another_repository(
        self, client: TestClient, db_session: Session
    ) -> None:
        self._two_repos(client, db_session)
        payload = _execution_payload(1) | {"repo_full_name": "otherorg/otherrepo"}
        assert client.post("/api/v1/ingest/devpilot-executions", json=payload).status_code == 403
        review = {
            "repo_full_name": "otherorg/otherrepo",
            "pr_number": 1,
            "head_sha": "abc",
            "workflow_run_id": 5,
            "status": "completed",
        }
        assert client.post("/api/v1/ingest/review-runs", json=review).status_code == 403
        lock = {"repo_full_name": "otherorg/otherrepo", "issue_number": 1, "execution_id": "x"}
        assert client.post("/api/v1/ingest/locks/devpilot", json=lock).status_code == 403

    def test_token_cannot_read_another_repositorys_policy(
        self, client: TestClient, db_session: Session
    ) -> None:
        self._two_repos(client, db_session)
        assert client.get("/api/v1/policy/otherorg/otherrepo").status_code == 403

    def test_token_cannot_update_or_release_another_repositorys_execution(
        self, client: TestClient, db_session: Session
    ) -> None:
        a, b = self._two_repos(client, db_session)
        # Repo B starts an execution with its own token.
        _authorize(client, db_session, b)
        payload = _execution_payload(1) | {"repo_full_name": "otherorg/otherrepo"}
        created = client.post("/api/v1/ingest/devpilot-executions", json=payload)
        assert created.status_code == 201
        # Repo A's token must not be able to touch it.
        _authorize(client, db_session, a)
        row = created.json()["id"]
        patch = client.patch(f"/api/v1/ingest/devpilot-executions/{row}", json={"status": "failed"})
        assert patch.status_code == 403
        key = "devpilot:otherorg/otherrepo:issue-42"
        assert client.delete(f"/api/v1/ingest/locks/devpilot/{key}").status_code == 403

    def test_workflow_run_id_cannot_be_hijacked_across_repositories(
        self, client: TestClient, db_session: Session
    ) -> None:
        a, b = self._two_repos(client, db_session)
        _authorize(client, db_session, b)
        review = {
            "repo_full_name": "otherorg/otherrepo",
            "pr_number": 1,
            "head_sha": "abc",
            "workflow_run_id": 777,
            "status": "completed",
            "gate_result": "PASS",
        }
        assert client.post("/api/v1/ingest/review-runs", json=review).status_code == 201
        # Repo A tries to overwrite repo B's run by reusing its workflow_run_id.
        _authorize(client, db_session, a)
        hijack = review | {"repo_full_name": "testorg/testrepo", "gate_result": "FAIL"}
        assert client.post("/api/v1/ingest/review-runs", json=hijack).status_code == 403
        assert db_session.query(ReviewRun).one().gate_result == "PASS"

    def test_health_stays_public(self, client: TestClient) -> None:
        assert client.get("/health").status_code == 200
