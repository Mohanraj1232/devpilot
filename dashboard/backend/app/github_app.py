"""GitHub App support: authenticate as the App and mint short-lived, repository-scoped tokens.

The App's private key lives only here, on the dashboard server. Target repositories never see
it: a DevPilot run asks the dashboard for a token, and the dashboard returns a one-hour
installation token limited to that single repository and to the permissions DevPilot needs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from jose import jwt

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.config import Settings

# What a DevPilot run may do. Deliberately no `workflows` and no `administration`.
TOKEN_PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "metadata": "read",
}
REQUIRED_WRITE = ("contents", "pull_requests", "issues")
FORBIDDEN = ("workflows", "administration")
_JWT_LIFETIME = 540  # GitHub allows at most 10 minutes
_CLOCK_SKEW = 60


class GitHubAppError(Exception):
    """A GitHub App call failed. ``status_code`` is GitHub's, or None for transport errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class InstallationToken:
    token: str
    expires_at: str
    permissions: dict[str, str]


def normalize_private_key(value: str) -> str:
    """Accept a PEM with real newlines or with literal ``\\n`` (as env vars often carry it)."""
    return value.replace("\\n", "\n").strip()


class GitHubApp:
    def __init__(
        self,
        app_id: str,
        private_key: str,
        slug: str,
        *,
        api_url: str = "https://api.github.com",
        now: Callable[[], float] = time.time,
    ) -> None:
        self.app_id = app_id
        self.slug = slug
        self._key = normalize_private_key(private_key)
        self._api = api_url.rstrip("/")
        self._now = now
        self._bot_user_id: int | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> GitHubApp | None:
        """The configured App, or None if the App is not set up."""
        key = settings.github_app_private_key
        if not key and settings.github_app_private_key_path:
            try:
                key = Path(settings.github_app_private_key_path).read_text(encoding="utf-8")
            except OSError:
                return None
        if not (settings.github_app_id and settings.github_app_slug and key):
            return None
        return cls(
            settings.github_app_id,
            key,
            settings.github_app_slug,
            api_url=settings.github_api_url,
        )

    # ── authentication ────────────────────────────────────────

    def app_jwt(self) -> str:
        """A short-lived JWT proving we are the App (signed with the private key)."""
        issued = int(self._now()) - _CLOCK_SKEW
        claims = {"iat": issued, "exp": issued + _CLOCK_SKEW + _JWT_LIFETIME, "iss": self.app_id}
        try:
            return str(jwt.encode(claims, self._key, algorithm="RS256"))
        except Exception as exc:  # malformed key
            raise GitHubAppError("The GitHub App private key is invalid") from exc

    def _request(
        self, method: str, path: str, *, json: Any = None, authenticated: bool = True
    ) -> httpx.Response:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "devpilot-dashboard",
        }
        if authenticated:
            headers["Authorization"] = f"Bearer {self.app_jwt()}"
        try:
            return httpx.request(
                method, f"{self._api}{path}", headers=headers, json=json, timeout=20.0
            )
        except httpx.HTTPError as exc:
            raise GitHubAppError("GitHub is unreachable") from exc

    # ── operations ────────────────────────────────────────────

    def installation_for_repo(self, full_name: str) -> dict[str, Any] | None:
        """The App's installation on this repository, or None if it is not installed."""
        response = self._request("GET", f"/repos/{full_name}/installation")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise GitHubAppError(
                f"Could not look up the installation (HTTP {response.status_code})",
                response.status_code,
            )
        data: dict[str, Any] = response.json()
        return data

    def mint_repo_token(self, full_name: str) -> InstallationToken:
        """A one-hour token for exactly this repository and TOKEN_PERMISSIONS."""
        installation = self.installation_for_repo(full_name)
        if installation is None:
            raise GitHubAppError("The GitHub App is not installed on this repository", 404)

        response = self._request(
            "POST",
            f"/app/installations/{installation['id']}/access_tokens",
            json={
                "repositories": [full_name.split("/", 1)[1]],
                "permissions": TOKEN_PERMISSIONS,
            },
        )
        if response.status_code == 422:
            raise GitHubAppError(
                "The installation does not grant the permissions DevPilot needs "
                "(contents, pull requests and issues: write)",
                422,
            )
        if response.status_code != 201:
            raise GitHubAppError(
                f"Could not create an installation token (HTTP {response.status_code})",
                response.status_code,
            )
        data = response.json()
        return InstallationToken(
            token=str(data["token"]),
            expires_at=str(data["expires_at"]),
            permissions=dict(data.get("permissions", {})),
        )

    @property
    def bot_login(self) -> str:
        return f"{self.slug}[bot]"

    def bot_user_id(self) -> int:
        """Numeric id of the App's bot user (needed for the commit author e-mail address)."""
        if self._bot_user_id is None:
            response = self._request("GET", f"/users/{self.slug}%5Bbot%5D", authenticated=False)
            if response.status_code != 200:
                raise GitHubAppError(
                    f"Could not look up the bot user (HTTP {response.status_code})",
                    response.status_code,
                )
            self._bot_user_id = int(response.json()["id"])
        return self._bot_user_id


def installation_problems(installation: dict[str, Any]) -> list[str]:
    """Explain what is wrong with an installation's permissions (empty list = fine)."""
    granted: dict[str, str] = installation.get("permissions") or {}
    problems = [
        f"Grant the App '{name.replace('_', ' ')}: write'"
        for name in REQUIRED_WRITE
        if granted.get(name) != "write"
    ]
    problems += [
        f"Remove the App's '{name}' permission (DevPilot must not have it)"
        for name in FORBIDDEN
        if granted.get(name)
    ]
    return problems
