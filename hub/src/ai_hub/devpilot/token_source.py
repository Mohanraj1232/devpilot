"""Where DevPilot gets the credential it acts with.

Two modes:

* **GitHub App (preferred).** The dashboard holds the App's private key and hands each run a
  one-hour installation token limited to the single repository. No long-lived secret is
  copied into target repositories, and the token is refreshed shortly before it expires.
* **Static token.** A personal access token, for a single owner or a sandbox.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from ai_hub.errors import HubError

if TYPE_CHECKING:
    from collections.abc import Callable

    from ai_hub.telemetry.dashboard_client import DashboardClient, InstallationToken

logger = logging.getLogger("ai_hub.devpilot")

_REFRESH_MARGIN = timedelta(minutes=5)
_ZERO = timedelta(0)


@dataclass(frozen=True)
class BotIdentity:
    """Who commits are authored as."""

    login: str
    user_id: int

    @property
    def email(self) -> str:
        return f"{self.user_id}+{self.login}@users.noreply.github.com"


class TokenSource(Protocol):
    is_app: bool

    def token(self) -> str:
        """A currently valid token (refreshed if it is about to expire)."""

    def identity(self) -> BotIdentity | None:
        """The bot's identity if the source knows it (None: look it up with the token)."""


class StaticTokenSource:
    """A fixed personal access token."""

    is_app = False

    def __init__(self, token: str) -> None:
        self._token = token

    def token(self) -> str:
        return self._token

    def identity(self) -> BotIdentity | None:
        return None


class AppTokenSource:
    """Installation tokens minted by the dashboard, cached and refreshed before expiry."""

    is_app = True

    def __init__(
        self,
        dashboard: DashboardClient,
        repo_full_name: str,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._dashboard = dashboard
        self._repo = repo_full_name
        self._now = now
        self._current: InstallationToken | None = None
        self._lock = threading.Lock()
        self.mint_count = 0

    def _fresh(self) -> InstallationToken:
        with self._lock:
            current = self._current
            remaining = current.expires_at - self._now() if current else None
            if current is None or remaining is None or remaining <= _REFRESH_MARGIN:
                try:
                    current = self._dashboard.request_installation_token(self._repo)
                except HubError:
                    # A brief dashboard outage must not fail a run that still holds a valid
                    # token; but never hand out one that has actually expired.
                    if self._current is not None and remaining is not None and remaining > _ZERO:
                        logger.warning(
                            "Could not refresh the GitHub token; reusing the current one"
                        )
                        return self._current
                    raise
                self._current = current
                self.mint_count += 1
            return current

    def token(self) -> str:
        return self._fresh().token

    def identity(self) -> BotIdentity:
        fresh = self._fresh()
        return BotIdentity(fresh.bot_login, fresh.bot_user_id)


def choose_token_source(
    pat: str, dashboard: DashboardClient | None, repo_full_name: str
) -> tuple[TokenSource, str]:
    """Pick how to authenticate. An explicitly configured personal access token wins; otherwise
    GitHub App tokens are requested from the dashboard. Returns (source, human description)."""
    from ai_hub.errors import FailureReason, HubError

    if pat:
        return StaticTokenSource(pat), "personal access token"
    if dashboard is not None:
        return AppTokenSource(dashboard, repo_full_name), "GitHub App (via the dashboard)"
    raise HubError(
        FailureReason.TOKEN_MISSING,
        "No credential for DevPilot: set DASHBOARD_URL and DASHBOARD_TOKEN to use the GitHub App, "
        "or DEVPILOT_BOT_TOKEN to use a personal access token",
    )
