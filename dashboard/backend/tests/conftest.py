"""Test fixtures using SQLite in-memory database."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from collections.abc import Generator
from typing import Any

# The application refuses to start with the placeholder session key outside development.
os.environ.setdefault("DEVPILOT_ENVIRONMENT", "development")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.api.deps import get_github  # noqa: E402
from app.config import Settings  # noqa: E402
from app.database import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.base import Base  # noqa: E402


def login(client: TestClient, user_id: int, github_token: str = "gho_test") -> None:
    """Log ``client`` in by sending a correctly signed session cookie (what Starlette issues)."""
    payload = base64.b64encode(
        json.dumps({"user_id": user_id, "github_token": github_token}).encode()
    )
    signed = TimestampSigner(Settings().secret_key).sign(payload).decode()
    client.cookies.set("session", signed)


class FakeGitHubUser:
    """Stands in for GitHubUserClient: a tiny in-memory GitHub as seen by the logged-in user."""

    def __init__(self) -> None:
        self.repos: dict[str, dict[str, Any]] = {}
        self.collaborators: dict[tuple[str, str], str] = {}
        self.files: set[tuple[str, str]] = set()
        self.protection: dict[str, dict[str, Any] | None] = {}
        self.rules: dict[str, list[dict[str, Any]]] = {}

    def add_repo(
        self, full_name: str, repo_id: int, *, admin: bool = True, default_branch: str = "main"
    ) -> None:
        owner, name = full_name.split("/")
        self.repos[full_name] = {
            "id": repo_id,
            "full_name": full_name,
            "name": name,
            "owner": {"login": owner},
            "default_branch": default_branch,
            "permissions": {"admin": admin, "push": admin, "pull": True},
        }

    def get_repo(self, full_name: str) -> dict[str, Any] | None:
        return self.repos.get(full_name)

    def collaborator_permission(self, full_name: str, login_name: str) -> str | None:
        return self.collaborators.get((full_name, login_name))

    def file_exists(self, full_name: str, path: str, ref: str | None = None) -> bool:
        return (full_name, path) in self.files

    def branch_protection(self, full_name: str, branch: str) -> dict[str, Any] | None:
        return self.protection.get(full_name)

    def branch_rules(self, full_name: str, branch: str) -> list[dict[str, Any]]:
        return self.rules.get(full_name, [])


@pytest.fixture
def fake_github() -> Generator[FakeGitHubUser, None, None]:
    fake = FakeGitHubUser()
    app.dependency_overrides[get_github] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_github, None)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    def _override_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as tc:
        yield tc
    app.dependency_overrides.clear()


@pytest.fixture
def authed_client(client: TestClient, db_session: Session) -> TestClient:
    from app.models.tables import User

    user = User(github_id=12345, login="testuser", avatar_url="https://example.com/avatar.png")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    with client.session_transaction() if hasattr(client, "session_transaction") else _noop():
        pass

    client.cookies.set("session", "fake")
    response = client.get("/health")
    assert response.status_code == 200

    from starlette.testclient import TestClient as _TC

    class AuthedClient(_TC):
        def __init__(self, app: Any, user_id: int, session: Session) -> None:
            super().__init__(app)
            self._user_id = user_id
            self._db = session

        def request(self, *args: Any, **kwargs: Any) -> Any:
            from starlette.middleware.sessions import SessionMiddleware
            return super().request(*args, **kwargs)

    client.app.state._test_user_id = user.id
    return client


def _noop() -> Generator[None, None, None]:
    yield


WEBHOOK_SECRET = "test-webhook-secret"


def sign_webhook(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def webhook(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Send a correctly signed GitHub webhook delivery."""
    monkeypatch.setenv("DEVPILOT_WEBHOOK_SECRET", WEBHOOK_SECRET)

    def send(
        event: str,
        payload: dict[str, Any],
        *,
        delivery_id: str = "d-1",
        signature: str | None = None,
        raw: bytes | None = None,
    ) -> Any:
        body = raw if raw is not None else json.dumps(payload).encode()
        headers = {
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": signature if signature is not None else sign_webhook(body),
            "Content-Type": "application/json",
        }
        if delivery_id:
            headers["X-GitHub-Delivery"] = delivery_id
        return client.post("/api/v1/webhooks/github", content=body, headers=headers)

    return send
