"""GitHub App authentication: token sources, the dashboard token request, per-request tokens."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from ai_hub.devpilot.token_source import (
    AppTokenSource,
    BotIdentity,
    StaticTokenSource,
    choose_token_source,
)
from ai_hub.errors import AuthError, FailureReason, HubError
from ai_hub.github.client import GitHubClient
from ai_hub.telemetry.dashboard_client import DashboardClient, InstallationToken

DASH = "http://dash.test"
GH = "https://api.github.test"
T0 = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)


def token_json(
    token: str = "ghs_first", expires: str = "2026-10-03T13:00:00Z", **extra: object
) -> dict[str, object]:
    return {
        "token": token,
        "expires_at": expires,
        "bot_login": "devpilot-app[bot]",
        "bot_user_id": 4321,
        **extra,
    }


class FakeDashboard:
    """Mints tokens that expire after `lifetime`, and counts how often it was asked."""

    def __init__(self, clock: list[datetime], lifetime: timedelta = timedelta(hours=1)) -> None:
        self.clock = clock
        self.lifetime = lifetime
        self.calls = 0
        self.error: Exception | None = None

    def request_installation_token(self, repo: str) -> InstallationToken:
        self.calls += 1
        if self.error:
            raise self.error
        return InstallationToken(
            token=f"ghs_{self.calls}",
            expires_at=self.clock[0] + self.lifetime,
            bot_login="devpilot-app[bot]",
            bot_user_id=4321,
        )


def make_source(
    lifetime: timedelta = timedelta(hours=1),
) -> tuple[AppTokenSource, FakeDashboard, list[datetime]]:
    clock = [T0]
    dashboard = FakeDashboard(clock, lifetime)
    source = AppTokenSource(dashboard, "org/repo", now=lambda: clock[0])  # type: ignore[arg-type]
    return source, dashboard, clock


class TestAppTokenSource:
    def test_mints_once_and_reuses_while_valid(self) -> None:
        source, dashboard, _ = make_source()
        assert [source.token() for _ in range(5)] == ["ghs_1"] * 5
        assert dashboard.calls == 1

    def test_refreshes_before_it_expires(self) -> None:
        source, dashboard, clock = make_source()
        assert source.token() == "ghs_1"
        clock[0] = T0 + timedelta(minutes=54)  # 6 minutes left: still fine
        assert source.token() == "ghs_1"
        clock[0] = T0 + timedelta(minutes=56)  # 4 minutes left: inside the 5 minute margin
        assert source.token() == "ghs_2"
        assert dashboard.calls == 2

    def test_a_token_that_already_expired_is_replaced(self) -> None:
        source, _, clock = make_source()
        source.token()
        clock[0] = T0 + timedelta(hours=3)
        assert source.token() == "ghs_2"

    def test_identity_comes_from_the_dashboard(self) -> None:
        source, dashboard, _ = make_source()
        identity = source.identity()
        assert identity == BotIdentity("devpilot-app[bot]", 4321)
        assert identity.email == "4321+devpilot-app[bot]@users.noreply.github.com"
        assert dashboard.calls == 1  # identity and token share one mint

    def test_a_dashboard_outage_near_expiry_reuses_the_still_valid_token(self) -> None:
        source, dashboard, clock = make_source()
        source.token()
        clock[0] = T0 + timedelta(minutes=58)  # 2 minutes left, inside the refresh margin
        dashboard.error = HubError(FailureReason.DASHBOARD_UNREACHABLE, "down")
        assert source.token() == "ghs_1"

    def test_an_expired_token_is_never_served(self) -> None:
        source, dashboard, clock = make_source()
        source.token()
        clock[0] = T0 + timedelta(hours=1, seconds=1)
        dashboard.error = HubError(FailureReason.DASHBOARD_UNREACHABLE, "down")
        with pytest.raises(HubError):
            source.token()

    def test_the_first_mint_failing_is_an_error(self) -> None:
        source, dashboard, _ = make_source()
        dashboard.error = HubError(FailureReason.REPO_DISABLED, "disabled")
        with pytest.raises(HubError, match="disabled"):
            source.token()

    def test_recovers_after_a_transient_error(self) -> None:
        source, dashboard, _ = make_source()
        dashboard.error = HubError(FailureReason.DASHBOARD_UNREACHABLE, "down")
        with pytest.raises(HubError):
            source.token()
        dashboard.error = None
        assert source.token().startswith("ghs_")

    def test_is_marked_as_an_app(self) -> None:
        assert make_source()[0].is_app is True


class TestStaticTokenSource:
    def test_returns_the_token_and_no_identity(self) -> None:
        source = StaticTokenSource("ghp_x")
        assert source.token() == "ghp_x"
        assert source.identity() is None
        assert source.is_app is False


class TestChooseTokenSource:
    def _dashboard(self) -> DashboardClient:
        return DashboardClient(DASH, "dtok")

    def test_an_explicit_personal_token_wins(self) -> None:
        source, mode = choose_token_source("ghp_x", self._dashboard(), "o/r")
        assert isinstance(source, StaticTokenSource)
        assert "personal" in mode

    def test_dashboard_gives_the_app(self) -> None:
        source, mode = choose_token_source("", self._dashboard(), "o/r")
        assert isinstance(source, AppTokenSource)
        assert "GitHub App" in mode

    def test_nothing_configured_is_a_clear_error(self) -> None:
        with pytest.raises(HubError) as exc:
            choose_token_source("", None, "o/r")
        assert exc.value.reason == FailureReason.TOKEN_MISSING
        assert "DASHBOARD_URL" in exc.value.message
        assert "DEVPILOT_BOT_TOKEN" in exc.value.message


# ── asking the dashboard ─────────────────────────────────────


class TestRequestInstallationToken:
    def _client(self) -> DashboardClient:
        return DashboardClient(DASH, "dtok")

    @respx.mock
    def test_success(self) -> None:
        route = respx.post(f"{DASH}/api/v1/ingest/installation-token").mock(
            return_value=httpx.Response(200, json=token_json())
        )
        token = self._client().request_installation_token("org/repo")
        assert token.token == "ghs_first"
        assert token.expires_at == datetime(2026, 10, 3, 13, 0, 0, tzinfo=UTC)
        assert (token.bot_login, token.bot_user_id) == ("devpilot-app[bot]", 4321)
        request = route.calls[0].request
        assert request.headers["authorization"] == "Bearer dtok"
        assert b'"repo_full_name":"org/repo"' in request.content.replace(b" ", b"")

    @respx.mock
    @pytest.mark.parametrize(
        ("status", "reason"),
        [
            (401, FailureReason.AUTH_FAILURE),
            (403, FailureReason.REPO_DISABLED),
            (404, FailureReason.REPO_NOT_REGISTERED),
            (409, FailureReason.BOT_NOT_COLLABORATOR),
            (502, FailureReason.DASHBOARD_UNREACHABLE),
            (503, FailureReason.DASHBOARD_UNREACHABLE),
        ],
    )
    def test_error_statuses_map_to_specific_reasons(
        self, status: int, reason: FailureReason
    ) -> None:
        respx.post(f"{DASH}/api/v1/ingest/installation-token").mock(
            return_value=httpx.Response(status, json={"detail": "operator message"})
        )
        with pytest.raises(HubError) as exc:
            self._client().request_installation_token("org/repo")
        assert exc.value.reason == reason

    @respx.mock
    def test_401_is_an_auth_error(self) -> None:
        respx.post(f"{DASH}/api/v1/ingest/installation-token").mock(
            return_value=httpx.Response(401, json={"detail": "x"})
        )
        with pytest.raises(AuthError):
            self._client().request_installation_token("org/repo")

    @respx.mock
    def test_the_dashboards_explanation_is_surfaced(self) -> None:
        respx.post(f"{DASH}/api/v1/ingest/installation-token").mock(
            return_value=httpx.Response(
                409, json={"detail": "The GitHub App is not installed on this repository"}
            )
        )
        with pytest.raises(HubError, match="not installed"):
            self._client().request_installation_token("org/repo")

    @respx.mock
    def test_secrets_in_error_details_are_redacted(self) -> None:
        leaked = "ghp_" + "k" * 36
        respx.post(f"{DASH}/api/v1/ingest/installation-token").mock(
            return_value=httpx.Response(500, json={"detail": f"boom {leaked}"})
        )
        with pytest.raises(HubError) as exc:
            self._client().request_installation_token("org/repo")
        assert leaked not in exc.value.message

    @respx.mock
    def test_network_failure(self) -> None:
        respx.post(f"{DASH}/api/v1/ingest/installation-token").mock(
            side_effect=httpx.ConnectError("down")
        )
        with pytest.raises(HubError) as exc:
            self._client().request_installation_token("org/repo")
        assert exc.value.reason == FailureReason.DASHBOARD_UNREACHABLE

    @respx.mock
    @pytest.mark.parametrize(
        "body", [{}, {"token": "x"}, {**token_json(), "expires_at": "yesterday-ish"}, "not json"]
    )
    def test_malformed_responses_are_rejected(self, body: object) -> None:
        respx.post(f"{DASH}/api/v1/ingest/installation-token").mock(
            return_value=httpx.Response(200, json=body)
        )
        with pytest.raises(HubError) as exc:
            self._client().request_installation_token("org/repo")
        assert exc.value.reason == FailureReason.DASHBOARD_UNREACHABLE

    def test_the_token_never_appears_in_a_repr(self) -> None:
        token = InstallationToken("ghs_SECRET", T0, "bot[bot]", 1)
        assert "ghs_SECRET" not in repr(token)
        assert "ghs_SECRET" not in str(token)


# ── the GitHub client uses a fresh token for every request ───


class TestGitHubClientWithAProvider:
    @respx.mock
    def test_the_provider_is_called_for_every_request(self) -> None:
        route = respx.get(f"{GH}/repos/o/r").mock(return_value=httpx.Response(200, json={}))
        tokens = iter(["ghs_one", "ghs_two", "ghs_three"])
        client = GitHubClient(lambda: next(tokens), "o/r", api_url=GH)
        client.get_repo()
        client.get_repo()
        client.get_repo()
        assert [c.request.headers["authorization"] for c in route.calls] == [
            "Bearer ghs_one",
            "Bearer ghs_two",
            "Bearer ghs_three",
        ]

    @respx.mock
    def test_a_retry_uses_a_freshly_provided_token(self) -> None:
        route = respx.get(f"{GH}/repos/o/r").mock(
            side_effect=[httpx.Response(502), httpx.Response(200, json={})]
        )
        tokens = iter(["ghs_old", "ghs_new"])
        GitHubClient(lambda: next(tokens), "o/r", api_url=GH, sleep=lambda s: None).get_repo()
        assert [c.request.headers["authorization"] for c in route.calls] == [
            "Bearer ghs_old",
            "Bearer ghs_new",
        ]

    @respx.mock
    def test_a_plain_string_token_still_works(self) -> None:
        route = respx.get(f"{GH}/repos/o/r").mock(return_value=httpx.Response(200, json={}))
        GitHubClient("ghp_static", "o/r", api_url=GH).get_repo()
        assert route.calls[0].request.headers["authorization"] == "Bearer ghp_static"

    @respx.mock
    def test_provider_errors_propagate_instead_of_sending_an_unauthenticated_request(self) -> None:
        route = respx.get(f"{GH}/repos/o/r").mock(return_value=httpx.Response(200, json={}))

        def failing() -> str:
            raise HubError(FailureReason.REPO_DISABLED, "DevPilot is disabled for this repository")

        with pytest.raises(HubError, match="disabled"):
            GitHubClient(failing, "o/r", api_url=GH).get_repo()
        assert not route.called

    def test_an_empty_token_is_refused(self) -> None:
        with pytest.raises(AuthError):
            GitHubClient("", "o/r")
