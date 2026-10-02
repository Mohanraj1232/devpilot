"""Thin GitHub REST client with retry, backoff and rate-limit handling."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from ai_hub.errors import AuthError, FailureReason, HubError
from ai_hub.safety.redact import redact

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("ai_hub.github")

_RETRY_STATUSES = {500, 502, 503, 504}
_MAX_RATE_LIMIT_WAIT = 90.0


class GitHubError(HubError):
    """A GitHub API call failed. ``status_code`` is None for transport-level failures."""

    def __init__(
        self,
        reason: FailureReason,
        message: str,
        *,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(reason, message, details)
        self.status_code = status_code


class GitHubClient:
    """GitHub REST API client scoped to a single repository."""

    def __init__(
        self,
        token: str,
        repo_full_name: str,
        *,
        api_url: str = "https://api.github.com",
        timeout: float = 30.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token:
            raise AuthError(FailureReason.TOKEN_MISSING, "GitHub token is not set")
        if repo_full_name.count("/") != 1:
            raise GitHubError(
                FailureReason.REPO_INACCESSIBLE, f"Invalid repository name: {repo_full_name}"
            )
        self.repo = repo_full_name
        self._max_retries = max_retries
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=api_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "devpilot-ai-hub",
            },
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    # ── low-level ─────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        allow_statuses: tuple[int, ...] = (),
    ) -> httpx.Response:
        """Send a request, retrying transient failures. Statuses in ``allow_statuses`` return."""
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._http.request(method, path, json=json, params=params)
            except httpx.TransportError as exc:
                if attempt <= self._max_retries:
                    self._sleep(min(2.0**attempt, 20.0))
                    continue
                raise GitHubError(
                    FailureReason.INTERNAL_ERROR, f"GitHub API unreachable: {exc}"
                ) from exc

            status = response.status_code
            if status < 400 or status in allow_statuses:
                return response

            if status in _RETRY_STATUSES and attempt <= self._max_retries:
                self._sleep(min(2.0**attempt, 20.0))
                continue

            wait = self._rate_limit_wait(response)
            if wait is not None:
                if wait <= _MAX_RATE_LIMIT_WAIT and attempt <= self._max_retries:
                    logger.warning("GitHub rate limited; sleeping %.0fs", wait)
                    self._sleep(wait)
                    continue
                raise GitHubError(
                    FailureReason.RATE_LIMITED, "GitHub API rate limit exceeded", status_code=status
                )

            raise self._error_for(response)

    @staticmethod
    def _rate_limit_wait(response: httpx.Response) -> float | None:
        if response.status_code not in (403, 429):
            return None
        headers = response.headers
        retry_after = headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return float(retry_after)
        if headers.get("x-ratelimit-remaining") == "0":
            reset = headers.get("x-ratelimit-reset")
            if reset and reset.isdigit():
                return max(0.0, float(reset) - time.time()) + 1.0
            return 60.0
        text = response.text.lower()
        if "secondary rate limit" in text or "abuse detection" in text:
            return 60.0
        if response.status_code == 429:
            return 60.0
        return None

    @staticmethod
    def _error_for(response: httpx.Response) -> GitHubError:
        status = response.status_code
        try:
            message = str(response.json().get("message", ""))
        except (ValueError, AttributeError):
            message = response.text[:200]
        message = redact(message)
        if status == 401:
            return GitHubError(
                FailureReason.AUTH_FAILURE, f"GitHub auth failed: {message}", status_code=status
            )
        if status == 403:
            return GitHubError(
                FailureReason.PERMISSION_DENIED,
                f"GitHub permission denied: {message}",
                status_code=status,
            )
        if status in (404, 410):
            return GitHubError(
                FailureReason.REPO_INACCESSIBLE,
                f"GitHub resource not found or inaccessible: {message}",
                status_code=status,
            )
        return GitHubError(
            FailureReason.INTERNAL_ERROR,
            f"GitHub API error {status}: {message}",
            status_code=status,
        )

    def _json(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubError(
                FailureReason.INTERNAL_ERROR, "GitHub returned a non-JSON response"
            ) from exc

    # ── repository ────────────────────────────────────────────

    def get_repo(self) -> dict[str, Any]:
        data: dict[str, Any] = self._json(self._request("GET", f"/repos/{self.repo}"))
        return data

    def get_authenticated_user(self) -> dict[str, Any]:
        data: dict[str, Any] = self._json(self._request("GET", "/user"))
        return data

    def get_collaborator_permission(self, login: str) -> str:
        """Return admin | write | read | none for ``login``."""
        response = self._request(
            "GET",
            f"/repos/{self.repo}/collaborators/{login}/permission",
            allow_statuses=(404,),
        )
        if response.status_code == 404:
            return "none"
        return str(self._json(response).get("permission", "none"))

    def get_branch_sha(self, branch: str) -> str | None:
        response = self._request(
            "GET", f"/repos/{self.repo}/git/ref/heads/{branch}", allow_statuses=(404,)
        )
        if response.status_code == 404:
            return None
        return str(self._json(response)["object"]["sha"])

    # ── issues ────────────────────────────────────────────────

    def get_issue(self, number: int) -> dict[str, Any] | None:
        """Fetch an issue. Returns None if it no longer exists (404/410)."""
        response = self._request(
            "GET", f"/repos/{self.repo}/issues/{number}", allow_statuses=(404, 410)
        )
        if response.status_code in (404, 410):
            return None
        data: dict[str, Any] = self._json(response)
        return data

    def add_issue_comment(self, number: int, body: str) -> None:
        self._request(
            "POST", f"/repos/{self.repo}/issues/{number}/comments", json={"body": body[:60000]}
        )

    def add_labels(self, number: int, labels: list[str]) -> None:
        self._request("POST", f"/repos/{self.repo}/issues/{number}/labels", json={"labels": labels})

    def remove_label(self, number: int, label: str) -> None:
        self._request(
            "DELETE",
            f"/repos/{self.repo}/issues/{number}/labels/{label}",
            allow_statuses=(404,),
        )

    # ── pull requests ─────────────────────────────────────────

    def list_open_pulls(self, *, head_prefix: str | None = None) -> list[dict[str, Any]]:
        """List open PRs from this repository (not forks), optionally by head branch prefix."""
        found: list[dict[str, Any]] = []
        for page in range(1, 6):
            response = self._request(
                "GET",
                f"/repos/{self.repo}/pulls",
                params={"state": "open", "per_page": 100, "page": page},
            )
            batch: list[dict[str, Any]] = self._json(response)
            for pull in batch:
                head = pull.get("head", {})
                if (head.get("repo") or {}).get("full_name") != self.repo:
                    continue
                if head_prefix and not str(head.get("ref", "")).startswith(head_prefix):
                    continue
                found.append(pull)
            if len(batch) < 100:
                break
        return found

    def find_open_pull_for_branch(self, branch: str) -> dict[str, Any] | None:
        for pull in self.list_open_pulls(head_prefix=branch):
            if pull["head"]["ref"] == branch:
                return pull
        return None

    def create_pull_request(self, *, title: str, body: str, head: str, base: str) -> dict[str, Any]:
        data: dict[str, Any] = self._json(
            self._request(
                "POST",
                f"/repos/{self.repo}/pulls",
                json={"title": title[:250], "body": body[:60000], "head": head, "base": base},
            )
        )
        return data

    def update_pull_request(self, number: int, *, title: str, body: str) -> dict[str, Any]:
        data: dict[str, Any] = self._json(
            self._request(
                "PATCH",
                f"/repos/{self.repo}/pulls/{number}",
                json={"title": title[:250], "body": body[:60000]},
            )
        )
        return data

    # ── pull request review output ────────────────────────────

    def _paginate(self, path: str, *, max_pages: int = 10) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            response = self._request("GET", path, params={"per_page": 100, "page": page})
            batch: list[dict[str, Any]] = self._json(response)
            items.extend(batch)
            if len(batch) < 100:
                break
        return items

    def list_issue_comments(self, number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{self.repo}/issues/{number}/comments")

    def create_issue_comment(self, number: int, body: str) -> dict[str, Any]:
        data: dict[str, Any] = self._json(
            self._request(
                "POST", f"/repos/{self.repo}/issues/{number}/comments", json={"body": body[:60000]}
            )
        )
        return data

    def update_issue_comment(self, comment_id: int, body: str) -> dict[str, Any]:
        data: dict[str, Any] = self._json(
            self._request(
                "PATCH",
                f"/repos/{self.repo}/issues/comments/{comment_id}",
                json={"body": body[:60000]},
            )
        )
        return data

    def list_review_comments(self, number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{self.repo}/pulls/{number}/comments")

    def create_review(
        self, number: int, *, commit_id: str, body: str, comments: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Post one review (event COMMENT: never approves or requests changes)."""
        data: dict[str, Any] = self._json(
            self._request(
                "POST",
                f"/repos/{self.repo}/pulls/{number}/reviews",
                json={
                    "commit_id": commit_id,
                    "body": body[:60000],
                    "event": "COMMENT",
                    "comments": comments,
                },
            )
        )
        return data

    def create_check_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        data: dict[str, Any] = self._json(
            self._request("POST", f"/repos/{self.repo}/check-runs", json=payload)
        )
        return data
