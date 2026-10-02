"""Dashboard API client for workflow-side data ingestion."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("ai_hub.telemetry")


@dataclass
class RepoLookup:
    """Result of asking the dashboard about a repository.

    ``state`` is ``ok`` (registered), ``not_registered`` or ``unreachable``. Only ``ok``
    carries a policy; callers must treat anything else as "do not run" (fail closed).
    """

    state: str
    policy: dict[str, Any] | None = None

    @property
    def review_enabled(self) -> bool:
        return bool(self.policy and self.policy.get("review_enabled", True))

    @property
    def devpilot_enabled(self) -> bool:
        return bool(self.policy and self.policy.get("devpilot_enabled", False))

    @property
    def policy_json(self) -> dict[str, Any]:
        value = (self.policy or {}).get("policy_json")
        return value if isinstance(value, dict) else {}


def _error_result(exc: httpx.HTTPError) -> dict[str, Any]:
    result: dict[str, Any] = {"error": str(exc)}
    if isinstance(exc, httpx.HTTPStatusError):
        result["status_code"] = exc.response.status_code
    return result


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
            return _error_result(exc)

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
            return _error_result(exc)

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

    def lookup_repository(self, owner: str, repo: str) -> RepoLookup:
        """Look up registration and policy. Distinguishes "not registered" from "unreachable"."""
        try:
            resp = httpx.get(
                f"{self._base}/api/v1/policy/{owner}/{repo}",
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("Policy lookup failed: %s", exc)
            return RepoLookup("unreachable")
        if resp.status_code == 404:
            return RepoLookup("not_registered")
        if resp.status_code >= 400:
            logger.warning("Policy lookup returned HTTP %s", resp.status_code)
            return RepoLookup("unreachable")
        try:
            return RepoLookup("ok", resp.json())
        except ValueError:
            return RepoLookup("unreachable")

    def is_reachable(self) -> bool:
        try:
            resp = httpx.get(
                f"{self._base}/health",
                timeout=5.0,
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
