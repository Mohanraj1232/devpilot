"""FastAPI application entry point."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.api import analytics, auth, executions, ingest, policy, repos, reviews, webhooks
from app.config import Settings

settings = Settings()

app = FastAPI(title="DevPilot Dashboard", version="1.0.0")

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=settings.session_max_age,
    https_only=False,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PREFIX = "/api/v1"
app.include_router(auth.router, prefix=PREFIX)
app.include_router(repos.router, prefix=PREFIX)
app.include_router(policy.router, prefix=PREFIX)
app.include_router(ingest.router, prefix=PREFIX)
app.include_router(reviews.router, prefix=PREFIX)
app.include_router(executions.router, prefix=PREFIX)
app.include_router(analytics.router, prefix=PREFIX)
app.include_router(webhooks.router, prefix=PREFIX)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
