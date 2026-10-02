"""GitHub API access on behalf of the logged-in dashboard user (their OAuth token)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
from fastapi import HTTPException


class GitHubUserClient:
    """Minimal read-only GitHub client used to verify what a user may manage."""

    def __init__(self, token: str, api_url: str = "https://api.github.com") -> None:
        self._token = token
        self._api = api_url.rstrip("/")

    def _get(self, path: str) -> httpx.Response:
        try:
            response = httpx.get(
                f"{self._api}{path}",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=15.0,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="GitHub is unreachable") from exc
        if response.status_code == 401:
            raise HTTPException(
                status_code=401, detail="Your GitHub session expired; please log in again"
            )
        return response

    def get_repo(self, full_name: str) -> dict[str, Any] | None:
        """The repository as the user sees it (includes their ``permissions``), or None."""
        response = self._get(f"/repos/{quote(full_name, safe='/')}")
        if response.status_code in (403, 404):
            return None
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail="Unexpected response from GitHub")
        data: dict[str, Any] = response.json()
        return data

    def collaborator_permission(self, full_name: str, login: str) -> str | None:
        response = self._get(
            f"/repos/{quote(full_name, safe='/')}/collaborators/{quote(login)}/permission"
        )
        if response.status_code != 200:
            return None
        return str(response.json().get("permission", "none"))

    def file_exists(self, full_name: str, path: str, ref: str | None = None) -> bool:
        suffix = f"?ref={quote(ref)}" if ref else ""
        response = self._get(f"/repos/{quote(full_name, safe='/')}/contents/{path}{suffix}")
        return response.status_code == 200

    def branch_protection(self, full_name: str, branch: str) -> dict[str, Any] | None:
        """Classic branch protection, or None if absent or not readable."""
        response = self._get(
            f"/repos/{quote(full_name, safe='/')}/branches/{quote(branch, safe='')}/protection"
        )
        if response.status_code != 200:
            return None
        data: dict[str, Any] = response.json()
        return data

    def branch_rules(self, full_name: str, branch: str) -> list[dict[str, Any]]:
        """Active repository-ruleset rules that apply to the branch."""
        response = self._get(
            f"/repos/{quote(full_name, safe='/')}/rules/branches/{quote(branch, safe='')}"
        )
        if response.status_code != 200:
            return []
        rules: list[dict[str, Any]] = response.json()
        return rules
