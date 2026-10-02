"""GitHub OAuth authentication routes."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import get_db
from app.models.tables import User
from app.schemas.auth import LoginUrl, UserResponse

if TYPE_CHECKING:
    pass

router = APIRouter(prefix="/auth", tags=["auth"])
settings = Settings()


@router.get("/login", response_model=LoginUrl)
def login(request: Request) -> LoginUrl:
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={settings.github_client_id}"
        f"&redirect_uri={settings.github_redirect_uri}"
        f"&state={state}"
        f"&scope=read:user,repo"
    )
    return LoginUrl(url=url)


@router.get("/callback")
def callback(
    code: str,
    state: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    saved_state = request.session.get("oauth_state")
    if not saved_state or saved_state != state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    import httpx

    token_resp = httpx.post(
        "https://github.com/login/oauth/access_token",
        json={
            "client_id": settings.github_client_id,
            "client_secret": settings.github_client_secret,
            "code": code,
        },
        headers={"Accept": "application/json"},
    )
    token_data = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="OAuth token exchange failed")

    user_resp = httpx.get(
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    gh_user = user_resp.json()

    user = db.query(User).filter(User.github_id == gh_user["id"]).first()
    if not user:
        user = User(
            github_id=gh_user["id"],
            login=gh_user["login"],
            avatar_url=gh_user.get("avatar_url"),
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    request.session["user_id"] = user.id
    return {"status": "ok", "login": user.login}


@router.post("/logout")
def logout(request: Request) -> dict:
    request.session.clear()
    return {"status": "ok"}


@router.get("/me", response_model=UserResponse)
def me(request: Request, db: Session = Depends(get_db)) -> UserResponse:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return UserResponse.model_validate(user)
