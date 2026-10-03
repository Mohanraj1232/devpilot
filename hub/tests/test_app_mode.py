"""DevPilot as a GitHub App: identity, per-call tokens, refusals, and the CLI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from ai_hub.cli import app
from ai_hub.devpilot import orchestrator as orch
from ai_hub.devpilot.orchestrator import DevPilotRunner, DevPilotSettings, Outcome
from ai_hub.devpilot.token_source import AppTokenSource, BotIdentity, StaticTokenSource
from ai_hub.errors import FailureReason, HubError
from ai_hub.models import ExecutionStatus
from tests.test_orchestrator import (
    GOOD_GREET,
    FakeGitHub,
    FakeLLM,
    World,
    _git,
    build_world,
    write_and_finish,
)

if TYPE_CHECKING:
    from pathlib import Path

BOT = BotIdentity("devpilot-app[bot]", 4321)


class FakeSource:
    """A token source whose token changes on every call, like a refreshing app token."""

    is_app = True

    def __init__(self) -> None:
        self.calls = 0

    def token(self) -> str:
        self.calls += 1
        return f"ghs_{self.calls}"

    def identity(self) -> BotIdentity:
        return BOT


class AppGitHub(FakeGitHub):
    """The GitHub API as seen with an installation token: `/user` and collaborator checks are
    not available to apps, so using them is a bug."""

    def get_authenticated_user(self) -> dict[str, Any]:
        raise AssertionError("an app token cannot call /user")

    def get_collaborator_permission(self, login: str) -> str:
        raise AssertionError("collaborator permission does not apply to an app")


@pytest.fixture
def world(tmp_path: Path) -> World:
    return build_world(tmp_path)


def run_as_app(world: World, gh: FakeGitHub, llm: FakeLLM, source: Any) -> Outcome:
    settings = DevPilotSettings(
        repo="octo/repo",
        issue_number=7,
        workspace=world.ws,
        execution_id="exec1234abcd5678",
        artifacts_dir=world.artifacts,
        config_path=world.ws / ".ai-review" / "config.yml",
        standalone=True,
        token_source=source,
    )
    return DevPilotRunner(settings, gh=gh, llm=llm).run()  # type: ignore[arg-type]


class TestAsAnApp:
    def test_the_pull_request_is_authored_as_the_app_bot(self, world: World) -> None:
        gh = AppGitHub(world)
        outcome = run_as_app(
            world, gh, FakeLLM(write_and_finish("src/greet.py", GOOD_GREET)), FakeSource()
        )
        assert outcome.status == ExecutionStatus.PR_OPENED
        log = _git(world.origin, "log", "-1", "devpilot/issue-7-add-greeting", "--format=%an <%ae>")
        assert log.strip() == "devpilot-app[bot] <4321+devpilot-app[bot]@users.noreply.github.com>"

    def test_no_collaborator_check_is_made_for_an_app(self, world: World) -> None:
        # AppGitHub raises if /user or the collaborator API is used; reaching the PR proves not.
        outcome = run_as_app(
            world,
            AppGitHub(world),
            FakeLLM(write_and_finish("src/greet.py", GOOD_GREET)),
            FakeSource(),
        )
        assert outcome.succeeded

    def test_git_authentication_uses_a_fresh_token_for_each_operation(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        used: list[str] = []
        real = orch.git_auth_env

        def spy(token: str, **kwargs: Any) -> dict[str, str]:
            used.append(token)
            return real(token, **kwargs)

        monkeypatch.setattr(orch, "git_auth_env", spy)
        outcome = run_as_app(
            world,
            AppGitHub(world),
            FakeLLM(write_and_finish("src/greet.py", GOOD_GREET)),
            FakeSource(),
        )
        assert outcome.succeeded
        # Checkout and push each asked the source: nothing is cached across the run.
        assert len(used) >= 2
        assert len(set(used)) == len(used)
        assert used == sorted(used, key=lambda t: int(t.split("_")[1]))

    def test_the_same_run_with_a_personal_token_still_checks_the_collaborator(
        self, world: World
    ) -> None:
        gh = FakeGitHub(world)
        gh.permission = "read"
        outcome = run_as_app(world, gh, FakeLLM(), StaticTokenSource("ghp_x"))
        assert outcome.failure_reason == "bot_not_collaborator"


class TestWhenTheDashboardRefusesAToken:
    """The first GitHub call asks the token source; its refusal must end the run cleanly."""

    def _refused(self, world: World, reason: FailureReason, message: str) -> Outcome:
        gh = AppGitHub(world)

        def refuse() -> dict[str, Any]:
            raise HubError(reason, message)

        gh.get_repo = refuse  # type: ignore[method-assign]
        return run_as_app(world, gh, FakeLLM(), FakeSource())

    def test_disabled_repository_is_an_expected_non_run(self, world: World) -> None:
        outcome = self._refused(world, FailureReason.REPO_DISABLED, "DevPilot is disabled")
        assert outcome.status == ExecutionStatus.BLOCKED
        assert outcome.exit_code == 0

    def test_unregistered_repository_is_an_expected_non_run(self, world: World) -> None:
        outcome = self._refused(world, FailureReason.REPO_NOT_REGISTERED, "not registered")
        assert outcome.exit_code == 0

    def test_app_not_installed_is_a_failure_to_fix(self, world: World) -> None:
        outcome = self._refused(world, FailureReason.BOT_NOT_COLLABORATOR, "App not installed")
        assert outcome.status == ExecutionStatus.FAILED
        assert outcome.exit_code == 1
        assert "not installed" in outcome.message

    def test_unreachable_dashboard_fails_closed(self, world: World) -> None:
        outcome = self._refused(world, FailureReason.DASHBOARD_UNREACHABLE, "down")
        assert outcome.exit_code == 1

    def test_nothing_is_pushed_when_refused(self, world: World) -> None:
        self._refused(world, FailureReason.BOT_NOT_COLLABORATOR, "x")
        assert _git(world.origin, "branch", "--list", "devpilot/*").strip() == ""


# ── the CLI ──────────────────────────────────────────────────

runner = CliRunner()


class FakeRunner:
    captured: DevPilotSettings | None = None
    outcome = Outcome(status=ExecutionStatus.PR_OPENED, message="done", pr_number=3)

    def __init__(self, settings: DevPilotSettings, **kwargs: Any) -> None:
        FakeRunner.captured = settings

    def run(self) -> Outcome:
        return FakeRunner.outcome


class FakeBedrock:
    def __init__(self, *args: Any, **kwargs: Any) -> None: ...


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr("ai_hub.devpilot.orchestrator.DevPilotRunner", FakeRunner)
    monkeypatch.setattr("ai_hub.llm.bedrock.BedrockClient", FakeBedrock)
    FakeRunner.captured = None
    FakeRunner.outcome = Outcome(status=ExecutionStatus.PR_OPENED, message="done", pr_number=3)
    for name in (
        "DEVPILOT_BOT_TOKEN",
        "GITHUB_TOKEN",
        "DASHBOARD_URL",
        "DASHBOARD_TOKEN",
        "GITHUB_STEP_SUMMARY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BEDROCK_MODEL_ID", "model")
    return tmp_path


def invoke(tmp_path: Path, env: dict[str, str]) -> Any:
    return runner.invoke(
        app,
        ["devpilot", "--repo", "o/r", "--issue", "7", "--workspace", str(tmp_path)],
        env=env,
        catch_exceptions=False,
    )


class TestCliCredentials:
    def test_dashboard_credentials_select_the_github_app(self, cli: Path) -> None:
        result = invoke(cli, {"DASHBOARD_URL": "http://d", "DASHBOARD_TOKEN": "t"})
        assert result.exit_code == 0
        assert isinstance(FakeRunner.captured.token_source, AppTokenSource)  # type: ignore[union-attr]
        assert "GitHub App" in result.output

    def test_a_personal_token_selects_pat_mode(self, cli: Path) -> None:
        result = invoke(cli, {"DEVPILOT_BOT_TOKEN": "ghp_x"})
        assert isinstance(FakeRunner.captured.token_source, StaticTokenSource)  # type: ignore[union-attr]
        assert "personal access token" in result.output

    def test_an_explicit_personal_token_wins_over_the_dashboard(self, cli: Path) -> None:
        invoke(
            cli,
            {"DEVPILOT_BOT_TOKEN": "ghp_x", "DASHBOARD_URL": "http://d", "DASHBOARD_TOKEN": "t"},
        )
        assert isinstance(FakeRunner.captured.token_source, StaticTokenSource)  # type: ignore[union-attr]

    def test_no_credentials_is_a_clear_error_and_never_starts_a_run(self, cli: Path) -> None:
        result = invoke(cli, {})
        assert result.exit_code == 1
        assert "DASHBOARD_URL" in result.output
        assert FakeRunner.captured is None

    def test_standalone_ignores_the_dashboard(self, cli: Path) -> None:
        result = runner.invoke(
            app,
            ["devpilot", "--repo", "o/r", "--issue", "7", "--workspace", str(cli), "--standalone"],
            env={"DASHBOARD_URL": "http://d", "DASHBOARD_TOKEN": "t"},
            catch_exceptions=False,
        )
        assert result.exit_code == 1  # standalone without a PAT has no credential
        assert FakeRunner.captured is None

    def test_missing_model_is_an_error(self, cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BEDROCK_MODEL_ID")
        assert invoke(cli, {"DEVPILOT_BOT_TOKEN": "x"}).exit_code == 1


class TestCliOutput:
    def test_exit_code_follows_the_outcome(self, cli: Path) -> None:
        FakeRunner.outcome = Outcome(
            status=ExecutionStatus.FAILED, message="boom", failure_reason="internal_error"
        )
        assert invoke(cli, {"DEVPILOT_BOT_TOKEN": "x"}).exit_code == 1

    def test_job_summary_is_written_and_redacted(self, cli: Path) -> None:
        leaked = "ghp_" + "m" * 36
        FakeRunner.outcome = Outcome(
            status=ExecutionStatus.BLOCKED,
            message=f"DevPilot is disabled {leaked}",
            failure_reason="repo_disabled",
        )
        summary = cli / "summary.md"
        result = invoke(
            cli,
            {
                "DASHBOARD_URL": "http://d",
                "DASHBOARD_TOKEN": "t",
                "GITHUB_STEP_SUMMARY": str(summary),
            },
        )
        text = summary.read_text(encoding="utf-8")
        assert result.exit_code == 0
        assert "blocked" in text and "repo_disabled" in text and "GitHub App" in text
        assert leaked not in text

    def test_github_output_is_still_written(self, cli: Path) -> None:
        out = cli / "gh_output"
        invoke(cli, {"DEVPILOT_BOT_TOKEN": "x", "GITHUB_OUTPUT": str(out)})
        assert "status=pr_opened" in out.read_text(encoding="utf-8")
        assert "pr_number=3" in out.read_text(encoding="utf-8")


def test_outcome_serialises(tmp_path: Path) -> None:
    assert json.loads(json.dumps(Outcome(ExecutionStatus.PR_OPENED, "m").to_dict()))["status"]
