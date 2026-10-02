"""Dashboard API client for workflow-side data ingestion."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("ai_hub.telemetry")


class DashboardClient:
    """Client for posting data to the DevPilot dashboard."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._timeout = timeout

    def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = httpx.post(
                f"{self._base}/api/v1{path}",
                json=data,
                headers=self._headers,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
        except httpx.HTTPError as exc:
            logger.warning("Dashboard ingest failed: %s", exc)
            return {"error": str(exc)}

    def _patch(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = httpx.patch(
                f"{self._base}/api/v1{path}",
                json=data,
                headers=self._headers,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
        except httpx.HTTPError as exc:
            logger.warning("Dashboard update failed: %s", exc)
            return {"error": str(exc)}

    def ingest_review_run(self, data: dict[str, Any]) -> dict[str, Any]:
        return self._post("/ingest/review-runs", data)

    def ingest_execution(self, data: dict[str, Any]) -> dict[str, Any]:
        return self._post("/ingest/devpilot-executions", data)

    def update_execution(self, execution_id: int, data: dict[str, Any]) -> dict[str, Any]:
        return self._patch(f"/ingest/devpilot-executions/{execution_id}", data)

    def acquire_lock(self, repo_full_name: str, issue_number: int, execution_id: str) -> bool:
        result = self._post(
            "/ingest/locks/devpilot",
            {
                "repo_full_name": repo_full_name,
                "issue_number": issue_number,
                "execution_id": execution_id,
            },
        )
        acquired: bool = result.get("acquired", False)
        return acquired

    def release_lock(self, lock_key: str) -> None:
        try:
            httpx.delete(
                f"{self._base}/api/v1/ingest/locks/devpilot/{lock_key}",
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("Lock release failed: %s", exc)

    def get_policy(self, owner: str, repo: str) -> dict[str, Any] | None:
        try:
            resp = httpx.get(
                f"{self._base}/api/v1/policy/{owner}/{repo}",
                headers=self._headers,
                timeout=self._timeout,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
        except httpx.HTTPError as exc:
            logger.warning("Policy fetch failed: %s", exc)
            return None

    def is_reachable(self) -> bool:
        try:
            resp = httpx.get(
                f"{self._base}/health",
                timeout=5.0,
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
