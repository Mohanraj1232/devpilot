"""Ownership checks, real repository verification and the startup guard."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_github, hash_ingest_token
from app.config import INSECURE_SECRET_KEY, Settings
from app.github_client import GitHubUserClient
from app.models.tables import IngestToken, Repository, User
from app.verification import GATE_CHECK, REQUIRED_WORKFLOWS, evaluate_branch_protection
from tests.conftest import FakeGitHubUser, login


def _user(db: Session, github_id: int = 1, name: str = "alice") -> User:
    user = User(github_id=github_id, login=name)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _register_body(full_name: str = "org/repo", repo_id: int = 100) -> dict[str, Any]:
    owner, name = full_name.split("/")
    return {"github_repo_id": repo_id, "owner": owner, "name": name, "full_name": full_name}


def _repo(db: Session, user: User, full_name: str = "org/repo", repo_id: int = 100) -> Repository:
    owner, name = full_name.split("/")
    repo = Repository(
        github_repo_id=repo_id, owner=owner, name=name, full_name=full_name, registered_by=user.id
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return repo


# ── registration ─────────────────────────────────────────────


class TestRegistration:
    def test_requires_login(self, client: TestClient, fake_github: FakeGitHubUser) -> None:
        fake_github.add_repo("org/repo", 100)
        assert client.post("/api/v1/repos", json=_register_body()).status_code == 401

    def test_admin_can_register_and_data_comes_from_github(
        self, client: TestClient, db_session: Session, fake_github: FakeGitHubUser
    ) -> None:
        user = _user(db_session)
        login(client, user.id)
        fake_github.add_repo("org/repo", 100)
        # The client lies about owner/name; GitHub's answer wins.
        body = _register_body() | {"owner": "evil", "name": "evil"}
        resp = client.post("/api/v1/repos", json=body)
        assert resp.status_code == 201
        assert (resp.json()["owner"], resp.json()["name"]) == ("org", "repo")
        assert resp.json()["devpilot_enabled"] is False  # opt-in

    def test_non_admin_cannot_register_someone_elses_repo(
        self, client: TestClient, db_session: Session, fake_github: FakeGitHubUser
    ) -> None:
        login(client, _user(db_session).id)
        fake_github.add_repo("victim/repo", 100, admin=False)
        resp = client.post("/api/v1/repos", json=_register_body("victim/repo"))
        assert resp.status_code == 403
        assert db_session.query(Repository).count() == 0

    def test_unknown_or_inaccessible_repo_is_404(
        self, client: TestClient, db_session: Session, fake_github: FakeGitHubUser
    ) -> None:
        login(client, _user(db_session).id)
        assert client.post("/api/v1/repos", json=_register_body("no/such")).status_code == 404

    def test_mismatched_repo_id_is_rejected(
        self, client: TestClient, db_session: Session, fake_github: FakeGitHubUser
    ) -> None:
        login(client, _user(db_session).id)
        fake_github.add_repo("org/repo", 100)
        assert client.post("/api/v1/repos", json=_register_body(repo_id=999)).status_code == 400

    def test_duplicate_registration_conflicts(
        self, client: TestClient, db_session: Session, fake_github: FakeGitHubUser
    ) -> None:
        user = _user(db_session)
        login(client, user.id)
        fake_github.add_repo("org/repo", 100)
        assert client.post("/api/v1/repos", json=_register_body()).status_code == 201
        assert client.post("/api/v1/repos", json=_register_body()).status_code == 409

    def test_get_github_requires_a_github_token_in_the_session(self, client: TestClient) -> None:
        # No override here: the real dependency must refuse an anonymous caller.
        assert client.post("/api/v1/repos", json=_register_body()).status_code == 401


# ── every management action needs admin rights ───────────────


class TestManagementRequiresAdmin:
    ACTIONS = (
        ("patch", "/api/v1/repos/{id}", {"devpilot_enabled": True}),
        ("delete", "/api/v1/repos/{id}", None),
        ("post", "/api/v1/repos/{id}/verify", None),
        ("post", "/api/v1/repos/{id}/tokens", None),
        ("put", "/api/v1/repos/{id}/policy", {"policy_json": {"devpilot": {"enabled": False}}}),
    )

    @pytest.mark.parametrize(("method", "path", "body"), ACTIONS)
    def test_anonymous_is_rejected(
        self,
        method: str,
        path: str,
        body: Any,
        client: TestClient,
        db_session: Session,
        fake_github: FakeGitHubUser,
    ) -> None:
        repo = _repo(db_session, _user(db_session))
        fake_github.add_repo("org/repo", 100)
        resp = getattr(client, method)(path.format(id=repo.id), **({"json": body} if body else {}))
        assert resp.status_code == 401

    @pytest.mark.parametrize(("method", "path", "body"), ACTIONS)
    def test_logged_in_non_admin_is_forbidden(
        self,
        method: str,
        path: str,
        body: Any,
        client: TestClient,
        db_session: Session,
        fake_github: FakeGitHubUser,
    ) -> None:
        owner = _user(db_session)
        repo = _repo(db_session, owner)
        attacker = _user(db_session, github_id=2, name="mallory")
        fake_github.add_repo("org/repo", 100, admin=False)  # mallory is not an admin
        login(client, attacker.id)
        resp = getattr(client, method)(path.format(id=repo.id), **({"json": body} if body else {}))
        assert resp.status_code == 403
        # Nothing changed.
        db_session.refresh(repo)
        assert repo.status == "active"
        assert repo.devpilot_enabled is False
        assert db_session.query(IngestToken).count() == 0

    def test_admin_can_manage(
        self, client: TestClient, db_session: Session, fake_github: FakeGitHubUser
    ) -> None:
        user = _user(db_session)
        repo = _repo(db_session, user)
        fake_github.add_repo("org/repo", 100)
        login(client, user.id)
        resp = client.patch(f"/api/v1/repos/{repo.id}", json={"devpilot_enabled": True})
        assert resp.status_code == 200
        assert resp.json()["devpilot_enabled"] is True


class TestTokens:
    def test_admin_gets_a_token_that_works_once_and_rotation_revokes_the_old_one(
        self, client: TestClient, db_session: Session, fake_github: FakeGitHubUser
    ) -> None:
        user = _user(db_session)
        repo = _repo(db_session, user)
        fake_github.add_repo("org/repo", 100)
        login(client, user.id)

        old = client.post(f"/api/v1/repos/{repo.id}/tokens").json()["token"]
        new = client.post(f"/api/v1/repos/{repo.id}/tokens").json()["token"]
        assert old != new
        # Stored hashed, never raw.
        stored = {t.token_hash for t in db_session.query(IngestToken)}
        assert hash_ingest_token(new) in stored
        assert new not in stored

        client.cookies.clear()  # act as the workflow now
        assert (
            client.get(
                "/api/v1/policy/org/repo", headers={"Authorization": f"Bearer {old}"}
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/api/v1/policy/org/repo", headers={"Authorization": f"Bearer {new}"}
            ).status_code
            == 200
        )

    def test_removing_a_repository_revokes_its_tokens(
        self, client: TestClient, db_session: Session, fake_github: FakeGitHubUser
    ) -> None:
        user = _user(db_session)
        repo = _repo(db_session, user)
        fake_github.add_repo("org/repo", 100)
        login(client, user.id)
        token = client.post(f"/api/v1/repos/{repo.id}/tokens").json()["token"]

        assert client.delete(f"/api/v1/repos/{repo.id}").status_code == 204

        client.cookies.clear()
        resp = client.get("/api/v1/policy/org/repo", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401


# ── verification ─────────────────────────────────────────────


def _good_classic(**overrides: Any) -> dict[str, Any]:
    protection: dict[str, Any] = {
        "required_pull_request_reviews": {"required_approving_review_count": 1},
        "required_status_checks": {"contexts": [GATE_CHECK]},
        "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False},
    }
    protection.update(overrides)
    return protection


class TestEvaluateBranchProtection:
    def test_good_classic_protection(self) -> None:
        assert evaluate_branch_protection(_good_classic(), []) == []

    def test_gate_check_in_checks_list_form(self) -> None:
        protection = _good_classic(
            required_status_checks={"contexts": [], "checks": [{"context": GATE_CHECK}]}
        )
        assert evaluate_branch_protection(protection, []) == []

    def test_no_protection_at_all(self) -> None:
        problems = evaluate_branch_protection(None, [])
        assert len(problems) == 1
        assert "no branch protection" in problems[0]

    @pytest.mark.parametrize(
        ("override", "expected"),
        [
            ({"required_pull_request_reviews": None}, "at least 1 approving review"),
            (
                {"required_pull_request_reviews": {"required_approving_review_count": 0}},
                "at least 1 approving review",
            ),
            ({"required_status_checks": {"contexts": ["other"]}}, GATE_CHECK),
            ({"required_status_checks": None}, GATE_CHECK),
            ({"allow_force_pushes": {"enabled": True}}, "force pushes"),
            ({"allow_deletions": {"enabled": True}}, "deletion"),
        ],
    )
    def test_each_weakness_is_reported(self, override: dict[str, Any], expected: str) -> None:
        problems = evaluate_branch_protection(_good_classic(**override), [])
        assert any(expected in p for p in problems), problems

    def test_rulesets_can_satisfy_the_requirements(self) -> None:
        rules = [
            {"type": "pull_request", "parameters": {"required_approving_review_count": 2}},
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": GATE_CHECK}]},
            },
            {"type": "non_fast_forward"},
            {"type": "deletion"},
        ]
        assert evaluate_branch_protection(None, rules) == []

    def test_classic_and_ruleset_combine(self) -> None:
        classic = _good_classic(required_status_checks=None)
        rules = [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": GATE_CHECK}]},
            }
        ]
        assert evaluate_branch_protection(classic, rules) == []


class TestVerifyEndpoint:
    def _setup(
        self,
        client: TestClient,
        db: Session,
        gh: FakeGitHubUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> Repository:
        monkeypatch.setenv("DEVPILOT_BOT_LOGIN", "devpilot-bot")
        user = _user(db)
        repo = _repo(db, user)
        gh.add_repo("org/repo", 100)
        login(client, user.id)
        return repo

    def test_fully_configured_repository_passes(
        self,
        client: TestClient,
        db_session: Session,
        fake_github: FakeGitHubUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = self._setup(client, db_session, fake_github, monkeypatch)
        fake_github.collaborators[("org/repo", "devpilot-bot")] = "write"
        fake_github.files = {("org/repo", w) for w in REQUIRED_WORKFLOWS}
        fake_github.protection["org/repo"] = _good_classic()
        body = client.post(f"/api/v1/repos/{repo.id}/verify").json()
        assert body["all_passed"] is True
        assert body["messages"] == []

    def test_unconfigured_repository_fails_with_actionable_messages(
        self,
        client: TestClient,
        db_session: Session,
        fake_github: FakeGitHubUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = self._setup(client, db_session, fake_github, monkeypatch)
        body = client.post(f"/api/v1/repos/{repo.id}/verify").json()
        assert body["all_passed"] is False
        assert body["bot_collaborator"] is False
        assert body["workflows_present"] is False
        assert body["branch_protection"] is False
        text = " ".join(body["messages"])
        assert "devpilot-bot" in text
        assert ".github/workflows/devpilot.yml" in text
        assert "branch protection" in text

    def test_read_only_bot_is_not_enough(
        self,
        client: TestClient,
        db_session: Session,
        fake_github: FakeGitHubUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = self._setup(client, db_session, fake_github, monkeypatch)
        fake_github.collaborators[("org/repo", "devpilot-bot")] = "read"
        assert client.post(f"/api/v1/repos/{repo.id}/verify").json()["bot_collaborator"] is False

    def test_unset_bot_login_cannot_pass(
        self,
        client: TestClient,
        db_session: Session,
        fake_github: FakeGitHubUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = self._setup(client, db_session, fake_github, monkeypatch)
        monkeypatch.delenv("DEVPILOT_BOT_LOGIN")
        body = client.post(f"/api/v1/repos/{repo.id}/verify").json()
        assert body["bot_collaborator"] is False
        assert "DEVPILOT_BOT_LOGIN" in " ".join(body["messages"])


# ── GitHub client (real HTTP layer, mocked transport) ────────

API = "https://api.github.test"


class TestGitHubUserClient:
    def _client(self) -> GitHubUserClient:
        return GitHubUserClient("gho_x", API)

    @respx.mock
    def test_get_repo_sends_the_users_token(self) -> None:
        route = respx.get(f"{API}/repos/org/repo").mock(
            return_value=httpx.Response(200, json={"id": 1, "permissions": {"admin": True}})
        )
        assert self._client().get_repo("org/repo") == {"id": 1, "permissions": {"admin": True}}
        assert route.calls[0].request.headers["authorization"] == "Bearer gho_x"

    @respx.mock
    @pytest.mark.parametrize("status", [403, 404])
    def test_inaccessible_repo_is_none(self, status: int) -> None:
        respx.get(f"{API}/repos/org/repo").mock(return_value=httpx.Response(status))
        assert self._client().get_repo("org/repo") is None

    @respx.mock
    def test_expired_token_asks_for_login(self) -> None:
        respx.get(f"{API}/repos/org/repo").mock(return_value=httpx.Response(401))
        with pytest.raises(HTTPException) as exc:
            self._client().get_repo("org/repo")
        assert exc.value.status_code == 401

    @respx.mock
    def test_unreachable_github_is_502(self) -> None:
        respx.get(f"{API}/repos/org/repo").mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(HTTPException) as exc:
            self._client().get_repo("org/repo")
        assert exc.value.status_code == 502

    @respx.mock
    def test_unexpected_status_is_502(self) -> None:
        respx.get(f"{API}/repos/org/repo").mock(return_value=httpx.Response(500))
        with pytest.raises(HTTPException) as exc:
            self._client().get_repo("org/repo")
        assert exc.value.status_code == 502

    @respx.mock
    def test_collaborator_permission(self) -> None:
        respx.get(f"{API}/repos/org/repo/collaborators/bot/permission").mock(
            return_value=httpx.Response(200, json={"permission": "write"})
        )
        assert self._client().collaborator_permission("org/repo", "bot") == "write"

    @respx.mock
    def test_missing_collaborator_is_none(self) -> None:
        respx.get(f"{API}/repos/org/repo/collaborators/bot/permission").mock(
            return_value=httpx.Response(404)
        )
        assert self._client().collaborator_permission("org/repo", "bot") is None

    @respx.mock
    def test_file_exists_uses_the_ref(self) -> None:
        route = respx.get(f"{API}/repos/org/repo/contents/.github/workflows/devpilot.yml").mock(
            return_value=httpx.Response(200, json={})
        )
        assert self._client().file_exists("org/repo", ".github/workflows/devpilot.yml", "main")
        assert route.calls[0].request.url.params["ref"] == "main"

    @respx.mock
    def test_protection_and_rules(self) -> None:
        respx.get(f"{API}/repos/org/repo/branches/main/protection").mock(
            return_value=httpx.Response(200, json={"a": 1})
        )
        respx.get(f"{API}/repos/org/repo/rules/branches/main").mock(
            return_value=httpx.Response(200, json=[{"type": "deletion"}])
        )
        client = self._client()
        assert client.branch_protection("org/repo", "main") == {"a": 1}
        assert client.branch_rules("org/repo", "main") == [{"type": "deletion"}]

    @respx.mock
    def test_unprotected_branch_is_none_and_no_rules(self) -> None:
        respx.get(f"{API}/repos/org/repo/branches/main/protection").mock(
            return_value=httpx.Response(404)
        )
        respx.get(f"{API}/repos/org/repo/rules/branches/main").mock(return_value=httpx.Response(404))
        client = self._client()
        assert client.branch_protection("org/repo", "main") is None
        assert client.branch_rules("org/repo", "main") == []


# ── startup guard ────────────────────────────────────────────


class TestStartupGuard:
    def test_placeholder_key_is_refused_in_production(self) -> None:
        with pytest.raises(RuntimeError, match="DEVPILOT_SECRET_KEY"):
            Settings(secret_key=INSECURE_SECRET_KEY, environment="production").validate_for_runtime()

    def test_placeholder_key_is_allowed_in_development(self) -> None:
        Settings(secret_key=INSECURE_SECRET_KEY, environment="development").validate_for_runtime()

    def test_real_key_is_allowed_in_production(self) -> None:
        Settings(secret_key="x" * 48, environment="production").validate_for_runtime()

    def test_production_is_the_default(self) -> None:
        assert Settings(secret_key="k").environment in ("production", "development")
        assert Settings.model_fields["environment"].default == "production"


def test_get_github_dependency_is_the_one_being_overridden() -> None:
    # Guards against the fixture silently overriding the wrong function.
    from app.api import repos

    assert repos.get_github is get_github
