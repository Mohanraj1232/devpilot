"""GitHub App: JWT signing, repository-scoped token minting, the token endpoint, verification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.orm import Session

from app.api.deps import get_github_app
from app.config import Settings
from app.github_app import (
    TOKEN_PERMISSIONS,
    GitHubApp,
    GitHubAppError,
    installation_problems,
    normalize_private_key,
)
from app.main import app as fastapi_app
from app.models.tables import AuditLog
from app.verification import verify_repository
from tests.conftest import FakeGitHubUser
from tests.test_api import _authorize, _seed_repo, _seed_user

API = "https://api.github.test"
SECRET_TOKEN = "ghs_" + "S" * 36


@pytest.fixture(scope="module")
def keys() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    return private, public


def make_app(private: str, *, now: float = 1_700_000_000.0) -> GitHubApp:
    return GitHubApp("12345", private, "devpilot-app", api_url=API, now=lambda: now)


INSTALLATION = {
    "id": 99,
    "permissions": {
        "contents": "write",
        "pull_requests": "write",
        "issues": "write",
        "metadata": "read",
    },
}


def mock_install(repo: str = "org/repo", status: int = 200, body: Any = None) -> respx.Route:
    return respx.get(f"{API}/repos/{repo}/installation").mock(
        return_value=httpx.Response(status, json=INSTALLATION if body is None else body)
    )


def mock_mint(status: int = 201) -> respx.Route:
    return respx.post(f"{API}/app/installations/99/access_tokens").mock(
        return_value=httpx.Response(
            status,
            json={
                "token": SECRET_TOKEN,
                "expires_at": "2026-10-03T12:00:00Z",
                "permissions": TOKEN_PERMISSIONS,
            },
        )
    )


def mock_bot_user(user_id: int = 4321) -> respx.Route:
    return respx.get(f"{API}/users/devpilot-app%5Bbot%5D").mock(
        return_value=httpx.Response(200, json={"id": user_id})
    )


# ── the JWT that proves we are the App ───────────────────────


class TestAppJwt:
    def test_is_rs256_with_the_app_id_and_a_short_lifetime(self, keys: tuple[str, str]) -> None:
        private, public = keys
        token = make_app(private).app_jwt()
        assert jwt.get_unverified_header(token)["alg"] == "RS256"
        claims = jwt.decode(
            token, public, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False}
        )
        assert claims["iss"] == "12345"
        assert claims["exp"] - claims["iat"] <= 600  # GitHub rejects longer-lived JWTs
        assert claims["iat"] < 1_700_000_000  # backdated for clock skew

    def test_signature_verifies_only_with_the_matching_public_key(
        self, keys: tuple[str, str]
    ) -> None:
        private, _ = keys
        other_public = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            .public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )
        with pytest.raises(Exception):  # noqa: B017, PT011 - any signature failure
            jwt.decode(
                make_app(private).app_jwt(),
                other_public,
                algorithms=["RS256"],
                options={"verify_exp": False, "verify_iat": False},
            )

    def test_invalid_key_is_a_clear_error(self) -> None:
        with pytest.raises(GitHubAppError, match="private key is invalid"):
            make_app("not a pem").app_jwt()

    def test_key_with_escaped_newlines_is_accepted(self, keys: tuple[str, str]) -> None:
        private, public = keys
        escaped = private.replace("\n", "\\n")
        assert "\n" not in escaped
        token = make_app(escaped).app_jwt()
        assert jwt.decode(
            token, public, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False}
        )

    def test_normalize_private_key(self) -> None:
        assert normalize_private_key("  a\\nb  ") == "a\nb"


class TestFromSettings:
    def test_unconfigured_is_none(self) -> None:
        assert GitHubApp.from_settings(Settings()) is None

    def test_partial_configuration_is_none(self, keys: tuple[str, str]) -> None:
        assert GitHubApp.from_settings(Settings(github_app_id="1", github_app_slug="x")) is None

    def test_configured_from_a_value(self, keys: tuple[str, str]) -> None:
        settings = Settings(
            github_app_id="1", github_app_slug="x", github_app_private_key=keys[0]
        )
        assert GitHubApp.from_settings(settings) is not None

    def test_configured_from_a_file(self, keys: tuple[str, str], tmp_path: Path) -> None:
        pem = tmp_path / "app.pem"
        pem.write_text(keys[0])
        settings = Settings(
            github_app_id="1", github_app_slug="x", github_app_private_key_path=str(pem)
        )
        assert GitHubApp.from_settings(settings) is not None

    def test_unreadable_key_file_is_none(self, tmp_path: Path) -> None:
        settings = Settings(
            github_app_id="1",
            github_app_slug="x",
            github_app_private_key_path=str(tmp_path / "missing.pem"),
        )
        assert GitHubApp.from_settings(settings) is None


# ── minting ──────────────────────────────────────────────────


class TestMinting:
    @respx.mock
    def test_token_is_scoped_to_one_repository_and_the_minimum_permissions(
        self, keys: tuple[str, str]
    ) -> None:
        mock_install()
        mint = mock_mint()
        token = make_app(keys[0]).mint_repo_token("org/repo")

        body = json.loads(mint.calls[0].request.content)
        assert body["repositories"] == ["repo"]  # just this one, not the whole installation
        assert body["permissions"] == {
            "contents": "write",
            "pull_requests": "write",
            "issues": "write",
            "metadata": "read",
        }
        assert "workflows" not in body["permissions"]
        assert "administration" not in body["permissions"]
        assert token.token == SECRET_TOKEN
        assert token.expires_at == "2026-10-03T12:00:00Z"

    @respx.mock
    def test_requests_are_authenticated_as_the_app(self, keys: tuple[str, str]) -> None:
        install = mock_install()
        mock_mint()
        make_app(keys[0]).mint_repo_token("org/repo")
        assert install.calls[0].request.headers["authorization"].startswith("Bearer ey")

    @respx.mock
    def test_not_installed(self, keys: tuple[str, str]) -> None:
        mock_install(status=404, body={})
        with pytest.raises(GitHubAppError, match="not installed") as exc:
            make_app(keys[0]).mint_repo_token("org/repo")
        assert exc.value.status_code == 404

    @respx.mock
    def test_insufficient_permissions(self, keys: tuple[str, str]) -> None:
        mock_install()
        mock_mint(status=422)
        with pytest.raises(GitHubAppError, match="does not grant") as exc:
            make_app(keys[0]).mint_repo_token("org/repo")
        assert exc.value.status_code == 422

    @respx.mock
    def test_github_errors(self, keys: tuple[str, str]) -> None:
        mock_install()
        mock_mint(status=500)
        with pytest.raises(GitHubAppError):
            make_app(keys[0]).mint_repo_token("org/repo")

    @respx.mock
    def test_unreachable_github(self, keys: tuple[str, str]) -> None:
        respx.get(f"{API}/repos/org/repo/installation").mock(side_effect=httpx.ConnectError("x"))
        with pytest.raises(GitHubAppError, match="unreachable"):
            make_app(keys[0]).mint_repo_token("org/repo")

    @respx.mock
    def test_errors_never_contain_the_token_or_the_key(self, keys: tuple[str, str]) -> None:
        mock_install()
        mock_mint(status=500)
        with pytest.raises(GitHubAppError) as exc:
            make_app(keys[0]).mint_repo_token("org/repo")
        assert SECRET_TOKEN not in exc.value.message
        assert "BEGIN" not in exc.value.message

    @respx.mock
    def test_bot_user_id_is_looked_up_once_unauthenticated(self, keys: tuple[str, str]) -> None:
        route = mock_bot_user(4321)
        app = make_app(keys[0])
        assert app.bot_user_id() == 4321
        assert app.bot_user_id() == 4321
        assert route.call_count == 1
        assert "authorization" not in route.calls[0].request.headers
        assert app.bot_login == "devpilot-app[bot]"


class TestInstallationProblems:
    def test_good_permissions(self) -> None:
        assert installation_problems(INSTALLATION) == []

    def test_missing_write_permissions(self) -> None:
        problems = installation_problems({"permissions": {"contents": "read"}})
        assert len(problems) == 3
        assert any("issues" in p for p in problems)

    def test_dangerous_permissions_are_flagged(self) -> None:
        granted = {**INSTALLATION["permissions"], "workflows": "write", "administration": "read"}
        problems = installation_problems({"permissions": granted})
        assert any("workflows" in p for p in problems)
        assert any("administration" in p for p in problems)


# ── the endpoint DevPilot runs call ──────────────────────────


@pytest.fixture
def with_app(keys: tuple[str, str]):  # type: ignore[no-untyped-def]
    fastapi_app.dependency_overrides[get_github_app] = lambda: make_app(keys[0])
    yield
    fastapi_app.dependency_overrides.pop(get_github_app, None)


def enabled_repo(db: Session) -> Any:
    repo = _seed_repo(db, _seed_user(db).id)
    repo.devpilot_enabled = True
    repo.full_name = "org/repo"
    db.commit()
    return repo


URL = "/api/v1/ingest/installation-token"
BODY = {"repo_full_name": "org/repo"}


class TestTokenEndpoint:
    @respx.mock
    def test_issues_a_scoped_token_and_audits_it(
        self, client: TestClient, db_session: Session, with_app: None
    ) -> None:
        repo = enabled_repo(db_session)
        _authorize(client, db_session, repo)
        mock_install()
        mint = mock_mint()
        mock_bot_user(4321)

        resp = client.post(URL, json=BODY)

        assert resp.status_code == 200
        assert resp.json() == {
            "token": SECRET_TOKEN,
            "expires_at": "2026-10-03T12:00:00Z",
            "bot_login": "devpilot-app[bot]",
            "bot_user_id": 4321,
        }
        assert resp.headers["cache-control"] == "no-store"
        assert json.loads(mint.calls[0].request.content)["repositories"] == ["repo"]

        row = db_session.query(AuditLog).one()
        assert (row.action, row.target) == ("installation_token_issued", "org/repo")
        assert SECRET_TOKEN not in json.dumps(row.details)  # the token is never stored

    def test_requires_an_ingest_token(
        self, client: TestClient, db_session: Session, with_app: None
    ) -> None:
        enabled_repo(db_session)
        assert client.post(URL, json=BODY).status_code == 401
        resp = client.post(URL, json=BODY, headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401

    def test_a_token_for_another_repository_cannot_mint_here(
        self, client: TestClient, db_session: Session, with_app: None
    ) -> None:
        from app.models.tables import Repository

        enabled_repo(db_session)
        other = Repository(
            github_repo_id=77, owner="x", name="y", full_name="x/y", registered_by=1
        )
        db_session.add(other)
        db_session.commit()
        _authorize(client, db_session, other)
        assert client.post(URL, json=BODY).status_code == 403

    def test_disabled_repositories_get_no_token(
        self, client: TestClient, db_session: Session, with_app: None
    ) -> None:
        repo = enabled_repo(db_session)
        repo.devpilot_enabled = False
        db_session.commit()
        _authorize(client, db_session, repo)
        assert client.post(URL, json=BODY).status_code == 403

    def test_removed_repositories_get_no_token(
        self, client: TestClient, db_session: Session, with_app: None
    ) -> None:
        repo = enabled_repo(db_session)
        _authorize(client, db_session, repo)
        repo.status = "removed"
        db_session.commit()
        assert client.post(URL, json=BODY).status_code == 404

    def test_unconfigured_app_is_a_503(self, client: TestClient, db_session: Session) -> None:
        repo = enabled_repo(db_session)
        _authorize(client, db_session, repo)
        fastapi_app.dependency_overrides[get_github_app] = lambda: None
        try:
            assert client.post(URL, json=BODY).status_code == 503
        finally:
            fastapi_app.dependency_overrides.pop(get_github_app, None)

    @respx.mock
    def test_app_not_installed_tells_the_operator_to_install_it(
        self, client: TestClient, db_session: Session, with_app: None
    ) -> None:
        repo = enabled_repo(db_session)
        _authorize(client, db_session, repo)
        mock_install(status=404, body={})
        resp = client.post(URL, json=BODY)
        assert resp.status_code == 409
        assert "not installed" in resp.json()["detail"]

    @respx.mock
    def test_missing_permissions_are_a_502_with_an_actionable_message(
        self, client: TestClient, db_session: Session, with_app: None
    ) -> None:
        repo = enabled_repo(db_session)
        _authorize(client, db_session, repo)
        mock_install()
        mock_mint(status=422)
        resp = client.post(URL, json=BODY)
        assert resp.status_code == 502
        assert "permissions" in resp.json()["detail"]
        assert db_session.query(AuditLog).count() == 0  # nothing was issued

    @respx.mock
    def test_github_outage_is_a_502(
        self, client: TestClient, db_session: Session, with_app: None
    ) -> None:
        repo = enabled_repo(db_session)
        _authorize(client, db_session, repo)
        respx.get(f"{API}/repos/org/repo/installation").mock(side_effect=httpx.ConnectError("x"))
        assert client.post(URL, json=BODY).status_code == 502


# ── verification ─────────────────────────────────────────────


class FakeApp:
    slug = "devpilot-app"

    def __init__(self, installation: dict[str, Any] | None, error: GitHubAppError | None = None):
        self._installation = installation
        self._error = error

    def installation_for_repo(self, full_name: str) -> dict[str, Any] | None:
        if self._error:
            raise self._error
        return self._installation


class TestVerifyWithTheApp:
    def _verify(self, app: FakeApp | None, bot_login: str = "") -> dict[str, Any]:
        gh = FakeGitHubUser()
        return verify_repository(gh, "org/repo", "main", bot_login, app)  # type: ignore[arg-type]

    def test_installed_with_the_right_permissions(self) -> None:
        result = self._verify(FakeApp(INSTALLATION))
        assert result["bot_collaborator"] is True
        assert not any("App" in m for m in result["messages"])

    def test_not_installed(self) -> None:
        result = self._verify(FakeApp(None))
        assert result["bot_collaborator"] is False
        assert any("Install the GitHub App 'devpilot-app'" in m for m in result["messages"])

    def test_wrong_permissions(self) -> None:
        installation = {"permissions": {**INSTALLATION["permissions"], "workflows": "write"}}
        result = self._verify(FakeApp(installation))
        assert result["bot_collaborator"] is False
        assert any("workflows" in m for m in result["messages"])

    def test_github_error_is_reported_not_raised(self) -> None:
        result = self._verify(FakeApp(None, GitHubAppError("GitHub is unreachable")))
        assert result["bot_collaborator"] is False
        assert any("unreachable" in m for m in result["messages"])

    def test_the_app_takes_precedence_over_a_bot_login(self) -> None:
        gh = FakeGitHubUser()
        gh.collaborators[("org/repo", "old-bot")] = "write"  # would pass the legacy check
        result = verify_repository(gh, "org/repo", "main", "old-bot", FakeApp(None))  # type: ignore[arg-type]
        assert result["bot_collaborator"] is False

    def test_falls_back_to_a_bot_user_when_no_app_is_configured(self) -> None:
        gh = FakeGitHubUser()
        gh.collaborators[("org/repo", "old-bot")] = "write"
        result = verify_repository(gh, "org/repo", "main", "old-bot", None)
        assert result["bot_collaborator"] is True

    def test_nothing_configured_cannot_pass(self) -> None:
        result = self._verify(None, bot_login="")
        assert result["bot_collaborator"] is False
        assert any("Neither a GitHub App" in m for m in result["messages"])
