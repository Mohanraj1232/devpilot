"""Tests for the dashboard telemetry client."""

from __future__ import annotations

import respx
from httpx import Response

from ai_hub.telemetry.dashboard_client import DashboardClient


class TestDashboardClient:
    def _client(self) -> DashboardClient:
        return DashboardClient("http://dashboard.test", "test-token")

    @respx.mock
    def test_ingest_review_run(self) -> None:
        respx.post("http://dashboard.test/api/v1/ingest/review-runs").mock(
            return_value=Response(201, json={"id": 1, "action": "created"})
        )
        client = self._client()
        result = client.ingest_review_run({"repo_full_name": "org/repo", "status": "completed"})
        assert result["action"] == "created"

    @respx.mock
    def test_ingest_execution(self) -> None:
        respx.post("http://dashboard.test/api/v1/ingest/devpilot-executions").mock(
            return_value=Response(201, json={"id": 1, "action": "created"})
        )
        client = self._client()
        result = client.ingest_execution({"repo_full_name": "org/repo", "status": "running"})
        assert result["action"] == "created"

    @respx.mock
    def test_update_execution(self) -> None:
        respx.patch("http://dashboard.test/api/v1/ingest/devpilot-executions/1").mock(
            return_value=Response(200, json={"id": 1, "status": "pr_opened"})
        )
        client = self._client()
        result = client.update_execution(1, {"status": "pr_opened"})
        assert result["status"] == "pr_opened"

    @respx.mock
    def test_acquire_lock_success(self) -> None:
        respx.post("http://dashboard.test/api/v1/ingest/locks/devpilot").mock(
            return_value=Response(
                200, json={"lock_key": "devpilot:org/repo:issue-42", "acquired": True}
            )
        )
        client = self._client()
        assert client.acquire_lock("org/repo", 42, "exec-001") is True

    @respx.mock
    def test_acquire_lock_denied(self) -> None:
        respx.post("http://dashboard.test/api/v1/ingest/locks/devpilot").mock(
            return_value=Response(
                200, json={"lock_key": "devpilot:org/repo:issue-42", "acquired": False}
            )
        )
        client = self._client()
        assert client.acquire_lock("org/repo", 42, "exec-001") is False

    @respx.mock
    def test_release_lock(self) -> None:
        respx.delete("http://dashboard.test/api/v1/ingest/locks/devpilot/test-key").mock(
            return_value=Response(200, json={"released": True})
        )
        client = self._client()
        client.release_lock("test-key")

    @respx.mock
    def test_get_policy_found(self) -> None:
        respx.get("http://dashboard.test/api/v1/policy/org/repo").mock(
            return_value=Response(200, json={"id": 1, "version": 1, "policy_json": {}})
        )
        client = self._client()
        result = client.get_policy("org", "repo")
        assert result is not None
        assert result["version"] == 1

    @respx.mock
    def test_get_policy_not_found(self) -> None:
        respx.get("http://dashboard.test/api/v1/policy/org/repo").mock(return_value=Response(404))
        client = self._client()
        result = client.get_policy("org", "repo")
        assert result is None

    @respx.mock
    def test_is_reachable_true(self) -> None:
        respx.get("http://dashboard.test/health").mock(
            return_value=Response(200, json={"status": "ok"})
        )
        client = self._client()
        assert client.is_reachable() is True

    @respx.mock
    def test_is_reachable_false(self) -> None:
        respx.get("http://dashboard.test/health").mock(return_value=Response(500))
        client = self._client()
        assert client.is_reachable() is False

    @respx.mock
    def test_ingest_handles_error(self) -> None:
        respx.post("http://dashboard.test/api/v1/ingest/review-runs").mock(
            return_value=Response(500, json={"detail": "Internal error"})
        )
        client = self._client()
        result = client.ingest_review_run({"status": "failed"})
        assert "error" in result
