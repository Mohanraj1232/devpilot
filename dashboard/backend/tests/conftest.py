"""Test fixtures using SQLite in-memory database."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import app
from app.models.base import Base


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
