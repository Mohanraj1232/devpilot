"""End-to-end tests for the DevPilot orchestrator.

A real bare "origin" repository and a real workspace clone are used; GitHub and the
model are scripted fakes. Each test asserts the externally visible outcome: the
status/exit code, what was pushed, and what was reported on the issue.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ai_hub.devpilot import orchestrator as orch
from ai_hub.devpilot.llm_tasks import ImplementationPlan, TriageDecision
from ai_hub.devpilot.orchestrator import DevPilotRunner, DevPilotSettings, Outcome
from ai_hub.devpilot.tester import RepairResult
from ai_hub.errors import FailureReason, LLMError
from ai_hub.github.client import GitHubError
from ai_hub.models import CheckStatus, ExecutionStatus
from ai_hub.telemetry.dashboard_client import RepoLookup

PY = Path(sys.executable).as_posix()
SECRET = "AKIAABCDEFGHIJKLMNOP"

CHECK_PY = (
    "import pathlib, sys\n"
    "p = pathlib.Path('src/greet.py')\n"
    "sys.exit(0 if p.exists() and 'hi' in p.read_text() else 1)\n"
)
GOOD_GREET = "def hello():\n    return 'hi'\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


# ── world: real origin + workspace ──────────────────────────


@dataclass
class World:
    root: Path
    origin: Path
    ws: Path
    artifacts: Path


def build_world(
    tmp_path: Path, *, files: dict[str, str] | None = None, config: str | None = "default"
) -> World:
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
    )
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    if files is None:
        files = {"README.md": "# demo\n", "check.py": CHECK_PY, "src/keep.txt": "keep\n"}
    if config == "default":
        config = f'devpilot:\n  test_command: "{PY} check.py"\n  max_fix_attempts: 2\n'
    all_files = dict(files)
    if config:
        all_files[".ai-review/config.yml"] = config
    for name, content in all_files.items():
        path = seed / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(seed, "add", "-A")
    _git(seed, "commit", "--allow-empty", "-m", "initial")
    _git(seed, "remote", "add", "origin", origin.as_posix())
    _git(seed, "push", "origin", "main")
    ws = tmp_path / "ws"
    subprocess.run(["git", "clone", origin.as_posix(), str(ws)], check=True, capture_output=True)
    return World(tmp_path, origin, ws, tmp_path / "artifacts")


def push_to_origin_main(world: World, files: dict[str, str], message: str = "other") -> None:
    other = world.root / f"other_{len(list(world.root.glob('other_*')))}"
    subprocess.run(
        ["git", "clone", world.origin.as_posix(), str(other)], check=True, capture_output=True
    )
    for name, content in files.items():
        (other / name).parent.mkdir(parents=True, exist_ok=True)
        (other / name).write_text(content)
    _git(other, "add", "-A")
    _git(other, "commit", "-m", message)
    _git(other, "push", "origin", "main")


def origin_branches(world: World) -> list[str]:
    out = _git(world.origin, "branch", "--list", "devpilot/*", "--format=%(refname:short)")
    return [line for line in out.splitlines() if line]


# ── fakes ───────────────────────────────────────────────────


def make_issue(**overrides: Any) -> dict[str, Any]:
    issue = {
        "number": 7,
        "title": "Add greeting",
        "body": "Please add a greeting module with a hello() function returning hi.",
        "state": "open",
        "labels": [{"name": "devpilot"}],
        "updated_at": "2026-01-01T00:00:00Z",
    }
    issue.update(overrides)
    return issue


class FakeGitHub:
    def __init__(self, world: World, issue: dict[str, Any] | None = None) -> None:
        self.world = world
        self.issue = make_issue() if issue is None else issue
        self.comments: list[str] = []
        self.labels_added: list[str] = []
        self.labels_removed: list[str] = []
        self.created_prs: list[dict[str, Any]] = []
        self.updated_prs: list[dict[str, Any]] = []
        self.open_pulls: list[dict[str, Any]] = []
        self.permission = "write"
        self.repo_data: dict[str, Any] = {"full_name": "octo/repo"}
        self.create_pr_error: GitHubError | None = None
        self.existing_pr: dict[str, Any] | None = None
        self.comment_error: Exception | None = None

    # repository
    def get_repo(self) -> dict[str, Any]:
        return self.repo_data

    def get_authenticated_user(self) -> dict[str, Any]:
        return {"login": "devpilot-bot", "id": 99}

    def get_collaborator_permission(self, login: str) -> str:
        return self.permission

    def get_branch_sha(self, branch: str) -> str | None:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
            cwd=self.world.origin,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    # issues
    def get_issue(self, number: int) -> dict[str, Any] | None:
        return None if self.issue is None else copy.deepcopy(self.issue)

    def _label_names(self) -> list[str]:
        return [label["name"] for label in self.issue["labels"]]

    def add_labels(self, number: int, labels: list[str]) -> None:
        self.labels_added.extend(labels)
        if self.issue is not None and number == self.issue["number"]:
            for name in labels:
                if name not in self._label_names():
                    self.issue["labels"].append({"name": name})

    def remove_label(self, number: int, label: str) -> None:
        self.labels_removed.append(label)
        if self.issue is not None:
            self.issue["labels"] = [x for x in self.issue["labels"] if x["name"] != label]

    def add_issue_comment(self, number: int, body: str) -> None:
        if self.comment_error:
            raise self.comment_error
        self.comments.append(body)

    # pull requests
    def list_open_pulls(self, *, head_prefix: str | None = None) -> list[dict[str, Any]]:
        return [
            p
            for p in self.open_pulls
            if not head_prefix or p["head"]["ref"].startswith(head_prefix)
        ]

    def find_open_pull_for_branch(self, branch: str) -> dict[str, Any] | None:
        return self.existing_pr

    def create_pull_request(self, *, title: str, body: str, head: str, base: str) -> dict[str, Any]:
        if self.create_pr_error:
            raise self.create_pr_error
        pr = {"number": 11, "html_url": "https://github.test/octo/repo/pull/11"}
        self.created_prs.append({"title": title, "body": body, "head": head, "base": base, **pr})
        return pr

    def update_pull_request(self, number: int, *, title: str, body: str) -> dict[str, Any]:
        self.updated_prs.append({"number": number, "title": title, "body": body})
        return {"number": number, "html_url": f"https://github.test/octo/repo/pull/{number}"}

    @property
    def all_comments(self) -> str:
        return "\n".join(self.comments)


def tool_turn(*uses: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "output": {
            "message": {
                "content": [
                    {"toolUse": {"toolUseId": f"t{i}", "name": name, "input": inp}}
                    for i, (name, inp) in enumerate(uses)
                ]
            }
        },
        "stopReason": "tool_use",
    }


def write_and_finish(path: str, content: str, summary: str = "Added the greeting.") -> list[Any]:
    return [
        tool_turn(("write_file", {"path": path, "content": content})),
        tool_turn(("finish", {"summary": summary})),
    ]


class FakeLLM:
    def __init__(
        self,
        script: list[Any] | None = None,
        *,
        triage: TriageDecision | Exception | None = None,
        plan: ImplementationPlan | Exception | None = None,
    ) -> None:
        self.script = list(script or [])
        self.triage = triage or TriageDecision(actionable=True)
        self.plan = plan or ImplementationPlan(
            approach="Create src/greet.py with hello().",
            files_to_touch=["src/greet.py"],
            test_strategy="Run check.py",
        )
        self.agent_calls: list[list[dict[str, Any]]] = []
        self.structured_calls: list[str] = []

    def converse_with_tool_retry(
        self, messages: list[Any], system: str, tool_schema: dict[str, Any], model: Any
    ) -> Any:
        name = tool_schema["name"]
        self.structured_calls.append(name)
        value = self.triage if name == "report_triage" else self.plan
        if isinstance(value, Exception):
            raise value
        return value

    def converse(
        self, messages: list[Any], system: str | None = None, tools: Any = None
    ) -> dict[str, Any]:
        self.agent_calls.append(copy.deepcopy(messages))
        item = self.script.pop(0)
        if callable(item):
            item = item()
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[no-any-return]


class FakeDashboard:
    def __init__(
        self,
        state: str = "ok",
        *,
        devpilot_enabled: bool = True,
        policy_json: dict[str, Any] | None = None,
        lock_free: bool = True,
        ingest_result: dict[str, Any] | None = None,
    ) -> None:
        self.state = state
        self.devpilot_enabled = devpilot_enabled
        self.policy_json = policy_json or {}
        self.lock_free = lock_free
        self.ingest_result = ingest_result or {"id": 5, "action": "created"}
        self.ingested: list[dict[str, Any]] = []
        self.updates: list[tuple[int, dict[str, Any]]] = []
        self.released: list[str] = []

    def lookup_repository(self, owner: str, repo: str) -> RepoLookup:
        if self.state != "ok":
            return RepoLookup(self.state)
        return RepoLookup(
            "ok", {"devpilot_enabled": self.devpilot_enabled, "policy_json": self.policy_json}
        )

    def acquire_lock(self, repo: str, number: int, execution_id: str) -> bool:
        return self.lock_free

    def ingest_execution(self, data: dict[str, Any]) -> dict[str, Any]:
        self.ingested.append(data)
        return self.ingest_result

    def update_execution(self, row: int, data: dict[str, Any]) -> dict[str, Any]:
        self.updates.append((row, data))
        return {}

    def release_lock(self, key: str) -> None:
        self.released.append(key)


def run(
    world: World,
    gh: FakeGitHub,
    llm: FakeLLM,
    *,
    dashboard: FakeDashboard | None = None,
    standalone: bool = True,
) -> Outcome:
    settings = DevPilotSettings(
        repo="octo/repo",
        issue_number=7,
        workspace=world.ws,
        execution_id="exec1234abcd5678",
        git_token="tok",
        artifacts_dir=world.artifacts,
        config_path=world.ws / ".ai-review" / "config.yml",
        run_url="https://github.test/octo/repo/actions/runs/1",
        standalone=standalone,
    )
    runner = DevPilotRunner(settings, gh=gh, llm=llm, dashboard=dashboard)  # type: ignore[arg-type]
    return runner.run()


@pytest.fixture
def world(tmp_path: Path) -> World:
    return build_world(tmp_path)


def assert_nothing_pushed(world: World, gh: FakeGitHub) -> None:
    assert origin_branches(world) == []
    assert gh.created_prs == []


# ── happy path ──────────────────────────────────────────────


class TestHappyPath:
    def test_issue_becomes_a_pull_request(self, world: World) -> None:
        gh = FakeGitHub(world)
        main_before = _git(world.origin, "rev-parse", "main").strip()
        llm = FakeLLM(write_and_finish("src/greet.py", GOOD_GREET))

        outcome = run(world, gh, llm)

        assert outcome.status == ExecutionStatus.PR_OPENED
        assert outcome.exit_code == 0
        assert outcome.pr_number == 11
        assert outcome.test_status == "success"

        # Branch pushed to origin, main untouched.
        assert origin_branches(world) == ["devpilot/issue-7-add-greeting"]
        assert _git(world.origin, "rev-parse", "main").strip() == main_before

        # Commit carries the bot identity and the execution trailer.
        log = _git(world.origin, "log", "-1", "devpilot/issue-7-add-greeting", "--format=%an%n%B")
        assert "devpilot-bot" in log
        assert "DevPilot-Execution: exec1234abcd5678" in log
        assert "Add greeting (#7)" in log

        # PR targets main from the feature branch and links the issue.
        pr = gh.created_prs[0]
        assert (pr["head"], pr["base"]) == ("devpilot/issue-7-add-greeting", "main")
        assert "Closes #7" in pr["body"]
        assert "passed" in pr["body"]
        assert "devpilot-execution: exec1234abcd5678" in pr["body"]
        assert "human review" in pr["body"]

        # Reporting and cleanup.
        assert "devpilot:in-progress" in gh.labels_added
        assert "devpilot:in-progress" in gh.labels_removed
        assert any("started" in c for c in gh.comments)
        assert "opened" in gh.comments[-1]

        # The model saw the issue as untrusted data and the model never got a token.
        first = json.dumps(llm.agent_calls[0])
        assert "<untrusted_issue>" in first
        assert "tok" not in first

    def test_artifacts_are_preserved(self, world: World) -> None:
        run(world, FakeGitHub(world), FakeLLM(write_and_finish("src/greet.py", GOOD_GREET)))
        result = json.loads((world.artifacts / "result.json").read_text())
        assert result["status"] == "pr_opened"
        assert result["execution_id"] == "exec1234abcd5678"
        assert (world.artifacts / "transcript.json").is_file()

    def test_mentions_in_ai_text_do_not_ping_people(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = FakeLLM(write_and_finish("src/greet.py", GOOD_GREET, summary="cc @octocat please"))
        run(world, gh, llm)
        assert "@octocat" not in gh.created_prs[0]["body"]

    def test_workspace_artifacts_are_not_committed(self, world: World) -> None:
        (world.ws / "src").mkdir(exist_ok=True)
        llm = FakeLLM(
            [
                tool_turn(
                    ("write_file", {"path": "src/greet.py", "content": GOOD_GREET}),
                    ("write_file", {"path": "__pycache__/x.pyc", "content": "junk"}),
                ),
                tool_turn(("finish", {"summary": "done"})),
            ]
        )
        outcome = run(world, FakeGitHub(world), llm)
        assert outcome.status == ExecutionStatus.PR_OPENED
        files = _git(
            world.origin, "show", "--name-only", "--format=", "devpilot/issue-7-add-greeting"
        )
        assert files.split() == ["src/greet.py"]

    def test_branch_collision_gets_a_unique_name(self, world: World) -> None:
        _git(world.ws, "branch", "devpilot/issue-7-add-greeting")
        _git(world.ws, "push", "origin", "devpilot/issue-7-add-greeting")
        gh = FakeGitHub(world)
        outcome = run(world, gh, FakeLLM(write_and_finish("src/greet.py", GOOD_GREET)))
        assert outcome.status == ExecutionStatus.PR_OPENED
        assert outcome.branch == "devpilot/issue-7-add-greeting-exec1234"
        # The pre-existing branch is untouched.
        assert "devpilot/issue-7-add-greeting" in origin_branches(world)

    def test_existing_pr_for_the_branch_is_updated_not_duplicated(self, world: World) -> None:
        gh = FakeGitHub(world)
        gh.create_pr_error = GitHubError(
            FailureReason.INTERNAL_ERROR, "already exists", status_code=422
        )
        gh.existing_pr = {"number": 5, "head": {"ref": "devpilot/issue-7-add-greeting"}}
        outcome = run(world, gh, FakeLLM(write_and_finish("src/greet.py", GOOD_GREET)))
        assert outcome.status == ExecutionStatus.PR_UPDATED
        assert outcome.pr_number == 5
        assert gh.created_prs == []
        assert gh.updated_prs[0]["number"] == 5


# ── A. issue trigger problems ───────────────────────────────


class TestIssueTriggers:
    def test_vague_issue_asks_for_clarification_without_calling_the_model(
        self, world: World
    ) -> None:
        gh = FakeGitHub(world, make_issue(body="fix it"))
        llm = FakeLLM()
        outcome = run(world, gh, llm)
        assert outcome.status == ExecutionStatus.NEEDS_CLARIFICATION
        assert outcome.exit_code == 0
        assert llm.structured_calls == [] and llm.agent_calls == []
        assert "too short" in gh.all_comments
        assert "devpilot" in gh.labels_removed  # so re-applying the label re-triggers
        assert_nothing_pushed(world, gh)

    def test_model_triage_can_reject_the_issue(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = FakeLLM(triage=TriageDecision(actionable=False, missing_info=["Which endpoint?"]))
        outcome = run(world, gh, llm)
        assert outcome.status == ExecutionStatus.NEEDS_CLARIFICATION
        assert "Which endpoint?" in gh.all_comments
        assert llm.agent_calls == []
        assert_nothing_pushed(world, gh)

    def test_already_resolved_issue_is_not_implemented(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = FakeLLM(triage=TriageDecision(actionable=True, already_resolved_hint=True))
        outcome = run(world, gh, llm)
        assert outcome.status == ExecutionStatus.NEEDS_CLARIFICATION
        assert outcome.failure_reason == "issue_already_resolved"
        assert llm.agent_calls == []

    def test_duplicate_trigger_is_silent_and_does_not_steal_the_label(self, world: World) -> None:
        issue = make_issue(labels=[{"name": "devpilot"}, {"name": "devpilot:in-progress"}])
        gh = FakeGitHub(world, issue)
        outcome = run(world, gh, FakeLLM())
        assert outcome.status == ExecutionStatus.BLOCKED
        assert outcome.failure_reason == "duplicate_execution"
        assert outcome.exit_code == 0
        assert gh.comments == []
        assert gh.labels_removed == []  # the other run still owns its label

    def test_open_devpilot_pr_blocks_a_second_run(self, world: World) -> None:
        gh = FakeGitHub(world)
        gh.open_pulls = [{"number": 3, "head": {"ref": "devpilot/issue-7-old-attempt"}}]
        outcome = run(world, gh, FakeLLM())
        assert outcome.failure_reason == "existing_pr"
        assert outcome.exit_code == 0
        assert gh.labels_added == []

    def test_unrelated_open_pr_does_not_block(self, world: World) -> None:
        gh = FakeGitHub(world)
        gh.open_pulls = [{"number": 3, "head": {"ref": "devpilot/issue-70-other"}}]
        outcome = run(world, gh, FakeLLM(write_and_finish("src/greet.py", GOOD_GREET)))
        assert outcome.status == ExecutionStatus.PR_OPENED

    def test_closed_issue_does_nothing(self, world: World) -> None:
        gh = FakeGitHub(world, make_issue(state="closed"))
        outcome = run(world, gh, FakeLLM())
        assert outcome.status == ExecutionStatus.BLOCKED
        assert outcome.failure_reason == "issue_closed"
        assert gh.comments == [] and gh.labels_added == []

    def test_missing_label_does_nothing(self, world: World) -> None:
        gh = FakeGitHub(world, make_issue(labels=[{"name": "bug"}]))
        outcome = run(world, gh, FakeLLM())
        assert outcome.status == ExecutionStatus.BLOCKED
        assert gh.labels_added == []

    def test_deleted_issue_stops_cleanly(self, world: World) -> None:
        gh = FakeGitHub(world)
        gh.issue = None  # type: ignore[assignment]
        outcome = run(world, gh, FakeLLM())
        assert outcome.failure_reason == "issue_deleted"
        assert outcome.exit_code == 0

    def test_pull_request_target_is_rejected(self, world: World) -> None:
        gh = FakeGitHub(world, make_issue(pull_request={"url": "x"}))
        outcome = run(world, gh, FakeLLM())
        assert outcome.status == ExecutionStatus.BLOCKED
        assert gh.labels_added == []


# ── B. repository / permission / dashboard ──────────────────


class TestRepositoryAndAccess:
    def test_archived_repository(self, world: World) -> None:
        gh = FakeGitHub(world)
        gh.repo_data = {"full_name": "octo/repo", "archived": True}
        outcome = run(world, gh, FakeLLM())
        assert outcome.failure_reason == "repo_inaccessible"
        assert outcome.exit_code == 1

    def test_renamed_repository(self, world: World) -> None:
        gh = FakeGitHub(world)
        gh.repo_data = {"full_name": "octo/renamed"}
        outcome = run(world, gh, FakeLLM())
        assert outcome.failure_reason == "repo_inaccessible"
        assert "renamed" in outcome.message

    def test_bot_without_write_access(self, world: World) -> None:
        gh = FakeGitHub(world)
        gh.permission = "read"
        outcome = run(world, gh, FakeLLM())
        assert outcome.failure_reason == "bot_not_collaborator"
        assert outcome.exit_code == 1
        assert "collaborator" in gh.all_comments  # the labeller is told why nothing happened
        assert gh.labels_added == []

    def test_empty_repository(self, tmp_path: Path) -> None:
        world = build_world(tmp_path, files={}, config=None)
        gh = FakeGitHub(world)
        outcome = run(world, gh, FakeLLM())
        assert outcome.failure_reason == "repo_empty"
        assert outcome.exit_code == 0
        assert_nothing_pushed(world, gh)

    def test_unsupported_project_without_test_command(self, tmp_path: Path) -> None:
        world = build_world(tmp_path, files={"README.md": "# x\n"}, config=None)
        gh = FakeGitHub(world)
        outcome = run(world, gh, FakeLLM())
        assert outcome.failure_reason == "unsupported_project"
        assert_nothing_pushed(world, gh)

    def test_invalid_config_fails_closed(self, tmp_path: Path) -> None:
        world = build_world(tmp_path, config="devpilot:\n  max_changed_files: 0\n")
        gh = FakeGitHub(world)
        outcome = run(world, gh, FakeLLM())
        assert outcome.status == ExecutionStatus.FAILED
        assert outcome.failure_reason == "config_invalid"
        assert "max_changed_files" in outcome.message
        assert outcome.exit_code == 1
        assert gh.labels_added == []

    def test_missing_base_branch(self, tmp_path: Path) -> None:
        world = build_world(
            tmp_path,
            config=f'devpilot:\n  base_branch: nope\n  test_command: "{PY} check.py"\n',
        )
        outcome = run(world, FakeGitHub(world), FakeLLM())
        assert outcome.failure_reason == "config_invalid"
        assert "nope" in outcome.message

    def test_unexpected_local_changes_are_not_overwritten(self, world: World) -> None:
        (world.ws / "scratch.txt").write_text("someone else's work")
        gh = FakeGitHub(world)
        outcome = run(world, gh, FakeLLM())
        assert outcome.failure_reason == "dirty_workspace"
        assert (world.ws / "scratch.txt").read_text() == "someone else's work"
        assert_nothing_pushed(world, gh)


class TestDashboard:
    def test_registered_repo_runs_and_reports_to_the_dashboard(self, world: World) -> None:
        dash = FakeDashboard()
        gh = FakeGitHub(world)
        outcome = run(
            world,
            gh,
            FakeLLM(write_and_finish("src/greet.py", GOOD_GREET)),
            dashboard=dash,
            standalone=False,
        )
        assert outcome.status == ExecutionStatus.PR_OPENED
        assert dash.ingested[0]["status"] == "running"
        assert dash.ingested[0]["issue_hash"] == outcome.issue_hash
        row, update = dash.updates[-1]
        assert row == 5
        assert update["status"] == "pr_opened"
        assert update["pr_number"] == 11
        assert dash.released == ["devpilot:octo/repo:issue-7"]

    def test_unreachable_dashboard_fails_closed(self, world: World) -> None:
        gh = FakeGitHub(world)
        outcome = run(
            world, gh, FakeLLM(), dashboard=FakeDashboard("unreachable"), standalone=False
        )
        assert outcome.failure_reason == "dashboard_unreachable"
        assert outcome.exit_code == 1
        assert_nothing_pushed(world, gh)

    def test_no_dashboard_configured_fails_closed(self, world: World) -> None:
        outcome = run(world, FakeGitHub(world), FakeLLM(), dashboard=None, standalone=False)
        assert outcome.failure_reason == "dashboard_unreachable"

    def test_unregistered_repo_does_not_run(self, world: World) -> None:
        gh = FakeGitHub(world)
        outcome = run(
            world, gh, FakeLLM(), dashboard=FakeDashboard("not_registered"), standalone=False
        )
        assert outcome.failure_reason == "repo_not_registered"
        assert outcome.exit_code == 0
        assert gh.labels_added == []

    def test_disabled_repo_does_not_run(self, world: World) -> None:
        outcome = run(
            world,
            FakeGitHub(world),
            FakeLLM(),
            dashboard=FakeDashboard(devpilot_enabled=False),
            standalone=False,
        )
        assert outcome.failure_reason == "repo_disabled"

    def test_repo_config_can_disable_devpilot(self, tmp_path: Path) -> None:
        world = build_world(tmp_path, config="devpilot:\n  enabled: false\n")
        outcome = run(world, FakeGitHub(world), FakeLLM())
        assert outcome.failure_reason == "repo_disabled"

    def test_lock_held_by_another_execution(self, world: World) -> None:
        gh = FakeGitHub(world)
        dash = FakeDashboard(lock_free=False)
        outcome = run(world, gh, FakeLLM(), dashboard=dash, standalone=False)
        assert outcome.failure_reason == "duplicate_execution"
        assert dash.ingested == [] and gh.comments == []
        assert dash.released == []  # we never owned it

    def test_lock_conflict_on_registration(self, world: World) -> None:
        dash = FakeDashboard(ingest_result={"error": "conflict", "status_code": 409})
        gh = FakeGitHub(world)
        outcome = run(world, gh, FakeLLM(), dashboard=dash, standalone=False)
        assert outcome.failure_reason == "duplicate_execution"
        assert gh.labels_added == []

    def test_registration_failure_fails_closed(self, world: World) -> None:
        dash = FakeDashboard(ingest_result={"error": "boom"})
        outcome = run(world, FakeGitHub(world), FakeLLM(), dashboard=dash, standalone=False)
        assert outcome.failure_reason == "dashboard_unreachable"
        assert outcome.exit_code == 1

    def test_lock_is_released_even_when_the_run_fails(self, world: World) -> None:
        dash = FakeDashboard()
        llm = FakeLLM([LLMError(FailureReason.MODEL_UNAVAILABLE, "down")])
        outcome = run(world, FakeGitHub(world), llm, dashboard=dash, standalone=False)
        assert outcome.status == ExecutionStatus.FAILED
        assert dash.released == ["devpilot:octo/repo:issue-7"]
        assert dash.updates[-1][1]["failure_reason"] == "model_unavailable"

    def test_dashboard_policy_can_tighten_limits(self, world: World) -> None:
        dash = FakeDashboard(policy_json={"devpilot": {"max_changed_files": 1}})
        llm = FakeLLM(
            [
                tool_turn(
                    ("write_file", {"path": "src/greet.py", "content": GOOD_GREET}),
                    ("write_file", {"path": "src/other.py", "content": "x = 1\n"}),
                ),
                tool_turn(("finish", {"summary": "done"})),
            ],
            plan=ImplementationPlan(approach="a", files_to_touch=["src/greet.py", "src/other.py"]),
        )
        outcome = run(world, FakeGitHub(world), llm, dashboard=dash, standalone=False)
        assert outcome.failure_reason == "diff_too_large"


# ── D/E. model, tests and repair ────────────────────────────


class TestModelFailures:
    def test_model_unavailable_stops_safely(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = FakeLLM([LLMError(FailureReason.MODEL_UNAVAILABLE, "Bedrock down")])
        outcome = run(world, gh, llm)
        assert outcome.status == ExecutionStatus.FAILED
        assert outcome.failure_reason == "model_unavailable"
        assert "No pull request was created" in gh.comments[-1]
        assert "devpilot:in-progress" in gh.labels_removed
        assert_nothing_pushed(world, gh)

    def test_planning_failure_stops_safely(self, world: World) -> None:
        llm = FakeLLM(plan=LLMError(FailureReason.INVALID_RESPONSE, "bad plan"))
        gh = FakeGitHub(world)
        outcome = run(world, gh, llm)
        assert outcome.failure_reason == "invalid_response"
        assert_nothing_pushed(world, gh)

    def test_agent_that_makes_no_changes_does_not_open_a_pr(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = FakeLLM([tool_turn(("finish", {"summary": "I could not find where to change."}))])
        outcome = run(world, gh, llm)
        assert outcome.failure_reason == "no_changes"
        assert "could not find" in outcome.message
        assert_nothing_pushed(world, gh)

    def test_agent_budget_exhaustion(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = FakeLLM([tool_turn(("list_dir", {"path": "."}))] * 60)
        outcome = run(world, gh, llm)
        assert outcome.failure_reason == "budget_exhausted"
        assert_nothing_pushed(world, gh)

    def test_unexpected_exception_is_contained_and_not_leaked(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = FakeLLM(plan=RuntimeError("kaboom ghp_" + "q" * 36))
        outcome = run(world, gh, llm)
        assert outcome.status == ExecutionStatus.FAILED
        assert outcome.failure_reason == "internal_error"
        assert "kaboom" not in outcome.message
        assert "ghp_" not in gh.all_comments
        assert "devpilot:in-progress" in gh.labels_removed  # cleanup still ran

    def test_reporting_failure_never_masks_the_outcome(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = FakeLLM(write_and_finish("src/greet.py", GOOD_GREET))
        original = gh.add_issue_comment

        def flaky_comment(number: int, body: str) -> None:
            if "opened" in body:
                raise GitHubError(FailureReason.INTERNAL_ERROR, "comment failed")
            original(number, body)

        gh.add_issue_comment = flaky_comment  # type: ignore[method-assign]
        outcome = run(world, gh, llm)
        assert outcome.status == ExecutionStatus.PR_OPENED
        assert "devpilot:in-progress" in gh.labels_removed


class TestTestsAndRepair:
    def test_failing_tests_are_fed_back_and_fixed(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = FakeLLM(
            write_and_finish("src/greet.py", "def hello():\n    return 'nope'\n")
            + write_and_finish("src/greet.py", GOOD_GREET, summary="Fixed it.")
        )
        outcome = run(world, gh, llm)
        assert outcome.status == ExecutionStatus.PR_OPENED
        assert outcome.attempts == 2
        assert outcome.test_status == "success"
        # The failure was sent back to the same conversation as untrusted data.
        feedback = json.dumps(llm.agent_calls[2])
        assert "untrusted_test_output" in feedback
        assert "The test command failed" in feedback
        # Only the repaired code ended up in the commit.
        content = _git(world.origin, "show", "devpilot/issue-7-add-greeting:src/greet.py")
        assert "hi" in content

    def test_persistent_failure_stops_without_a_pr_and_keeps_the_diff(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = FakeLLM(
            write_and_finish("src/greet.py", "x = 'a'\n")
            + write_and_finish("src/greet.py", "x = 'b'\n")
            + write_and_finish("src/greet.py", "x = 'c'\n")
        )
        outcome = run(world, gh, llm)
        assert outcome.status == ExecutionStatus.TESTS_FAILED
        assert outcome.failure_reason == "tests_failed"
        assert outcome.attempts == 3  # initial run + max_fix_attempts (2)
        assert outcome.exit_code == 1
        assert_nothing_pushed(world, gh)
        assert "x = 'c'" in (world.artifacts / "diff.patch").read_text()
        assert "No pull request was created" in gh.comments[-1]

    def test_repeating_the_same_failed_fix_stops_early(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = FakeLLM(
            write_and_finish("src/greet.py", "x = 'a'\n")
            # "Fix" that changes nothing: same working tree as the failed one.
            + [tool_turn(("finish", {"summary": "tried again"}))]
        )
        outcome = run(world, gh, llm)
        assert outcome.status == ExecutionStatus.TESTS_FAILED
        assert outcome.failure_reason == "cannot_solve"
        assert outcome.attempts == 2
        assert_nothing_pushed(world, gh)

    def test_agent_giving_up_during_repair(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = FakeLLM(
            write_and_finish("src/greet.py", "x = 'a'\n")
            + [LLMError(FailureReason.MODEL_UNAVAILABLE, "down")]
        )
        outcome = run(world, gh, llm)
        assert outcome.status == ExecutionStatus.FAILED
        assert outcome.failure_reason == "model_unavailable"
        assert_nothing_pushed(world, gh)

    def test_agent_can_run_tests_itself(self, world: World) -> None:
        llm = FakeLLM(
            [
                tool_turn(("write_file", {"path": "src/greet.py", "content": GOOD_GREET})),
                tool_turn(("run_tests", {})),
                tool_turn(("finish", {"summary": "verified"})),
            ]
        )
        outcome = run(world, FakeGitHub(world), llm)
        assert outcome.status == ExecutionStatus.PR_OPENED
        tool_result = json.dumps(llm.agent_calls[2][-1])
        assert '"status": "success"' in tool_result

    def test_unavailable_test_command_is_reported_not_hidden(self, tmp_path: Path) -> None:
        world = build_world(
            tmp_path, config='devpilot:\n  test_command: "definitely-not-a-command"\n'
        )
        gh = FakeGitHub(world)
        outcome = run(world, gh, FakeLLM(write_and_finish("src/greet.py", GOOD_GREET)))
        assert outcome.status == ExecutionStatus.PR_OPENED
        assert outcome.test_status == "error"
        body = gh.created_prs[0]["body"]
        assert "could not be run" in body
        assert "Automated verification unavailable" in body

    @pytest.mark.parametrize(
        ("repair_result", "expected_status", "expected_in_body", "expected_reason"),
        [
            (
                RepairResult(CheckStatus.SKIPPED, 1, "", no_tests=True),
                ExecutionStatus.PR_OPENED,
                "No automated tests were found",
                None,
            ),
            (
                RepairResult(
                    CheckStatus.SUCCESS, 2, "", is_flaky=True, flaky_evidence="1 pass, 1 fail"
                ),
                ExecutionStatus.PR_OPENED,
                "Flaky tests detected",
                None,
            ),
            (
                RepairResult(CheckStatus.ERROR, 1, "Test command timed out after 900s"),
                ExecutionStatus.FAILED,
                None,
                "test_timeout",
            ),
        ],
    )
    def test_test_outcomes_are_interpreted(
        self,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
        repair_result: RepairResult,
        expected_status: ExecutionStatus,
        expected_in_body: str | None,
        expected_reason: str | None,
    ) -> None:
        monkeypatch.setattr(orch, "run_test_repair_loop", lambda *a, **k: repair_result)
        gh = FakeGitHub(world)
        outcome = run(world, gh, FakeLLM(write_and_finish("src/greet.py", GOOD_GREET)))
        assert outcome.status == expected_status
        assert outcome.failure_reason == expected_reason
        if expected_in_body:
            assert expected_in_body in gh.created_prs[0]["body"]
        else:
            assert_nothing_pushed(world, gh)


# ── G. guardrails and secrets ───────────────────────────────


class TestGuardrails:
    def test_secret_in_generated_code_is_never_committed(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = FakeLLM(
            [
                tool_turn(
                    ("write_file", {"path": "src/greet.py", "content": GOOD_GREET}),
                    ("write_file", {"path": "src/cfg.py", "content": f"KEY = '{SECRET}'\n"}),
                ),
                tool_turn(("finish", {"summary": "done"})),
            ],
            plan=ImplementationPlan(approach="a", files_to_touch=["src/greet.py", "src/cfg.py"]),
        )
        outcome = run(world, gh, llm)
        assert outcome.failure_reason == "secret_detected"
        assert_nothing_pushed(world, gh)
        # Only the initial commit exists: nothing was committed.
        assert _git(world.ws, "log", "--oneline").count("\n") == 1
        # The secret appears nowhere we publish: comments, result, transcript, diff.
        everything = gh.all_comments + "".join(
            p.read_text() for p in world.artifacts.glob("*") if p.is_file()
        )
        assert SECRET not in everything
        assert not (world.artifacts / "diff.patch").exists()

    def test_oversized_diff(self, tmp_path: Path) -> None:
        world = build_world(
            tmp_path,
            config=f'devpilot:\n  test_command: "{PY} check.py"\n  max_changed_lines: 3\n',
        )
        gh = FakeGitHub(world)
        content = GOOD_GREET + "".join(f"v{i} = {i}\n" for i in range(10))
        outcome = run(world, gh, FakeLLM(write_and_finish("src/greet.py", content)))
        assert outcome.failure_reason == "diff_too_large"
        assert_nothing_pushed(world, gh)

    def test_unrelated_changes(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = FakeLLM(
            [
                tool_turn(
                    ("write_file", {"path": "src/greet.py", "content": GOOD_GREET}),
                    ("write_file", {"path": "src/a.py", "content": "a = 1\n"}),
                    ("write_file", {"path": "src/b.py", "content": "b = 1\n"}),
                ),
                tool_turn(("finish", {"summary": "done"})),
            ]
        )
        outcome = run(world, gh, llm)
        assert outcome.failure_reason == "unrelated_changes"
        assert_nothing_pushed(world, gh)

    def test_forbidden_codeowners_change(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = FakeLLM(
            [
                tool_turn(
                    ("write_file", {"path": "src/greet.py", "content": GOOD_GREET}),
                    ("write_file", {"path": "CODEOWNERS", "content": "* @me\n"}),
                ),
                tool_turn(("finish", {"summary": "done"})),
            ],
            plan=ImplementationPlan(approach="a", files_to_touch=["src/greet.py", "CODEOWNERS"]),
        )
        outcome = run(world, gh, llm)
        assert outcome.failure_reason == "forbidden_change"
        assert_nothing_pushed(world, gh)

    def test_workflow_files_cannot_be_written_at_all(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = FakeLLM(
            [
                tool_turn(("write_file", {"path": ".github/workflows/ci.yml", "content": "x"})),
                tool_turn(("finish", {"summary": "done"})),
            ]
        )
        outcome = run(world, gh, llm)
        assert outcome.failure_reason == "no_changes"
        assert not (world.ws / ".github").exists()

    def test_weakening_the_gate_config_is_rejected(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = FakeLLM(
            [
                tool_turn(
                    ("write_file", {"path": "src/greet.py", "content": GOOD_GREET}),
                    (
                        "write_file",
                        {
                            "path": ".ai-review/config.yml",
                            "content": "quality_gate:\n  enabled: false\n",
                        },
                    ),
                ),
                tool_turn(("finish", {"summary": "done"})),
            ],
            plan=ImplementationPlan(
                approach="a", files_to_touch=["src/greet.py", ".ai-review/config.yml"]
            ),
        )
        outcome = run(world, gh, llm)
        assert outcome.failure_reason == "destructive_operation"
        assert_nothing_pushed(world, gh)

    def test_binary_files_are_rejected(self, world: World) -> None:
        # Simulate the test run producing a binary file that is not git-ignored.
        script = (
            "import pathlib; pathlib.Path('blob.bin').write_bytes(bytes(range(256))*8); "
            "import sys; sys.exit(0)"
        )
        escaped = script.replace('"', '\\"')
        tmp = world.root / "cfg"
        tmp.mkdir()
        # Re-point the test command at a script that also drops a binary file.
        cfg = world.ws / ".ai-review" / "config.yml"
        cfg.write_text(f'devpilot:\n  test_command: "{PY} -c \\"{escaped}\\""\n')
        _git(world.ws, "add", "-A")
        _git(world.ws, "commit", "-m", "cfg")
        _git(world.ws, "push", "origin", "main")
        gh = FakeGitHub(world)
        outcome = run(world, gh, FakeLLM(write_and_finish("src/greet.py", GOOD_GREET)))
        assert outcome.failure_reason == "forbidden_change"
        assert "Binary" in outcome.message
        assert_nothing_pushed(world, gh)


# ── C/H. changes during the run ─────────────────────────────


class TestChangesDuringExecution:
    def _llm_with_hook(self, hook: Any) -> FakeLLM:
        def finish_with_side_effect() -> dict[str, Any]:
            hook()
            return tool_turn(("finish", {"summary": "done"}))

        return FakeLLM(
            [
                tool_turn(("write_file", {"path": "src/greet.py", "content": GOOD_GREET})),
                finish_with_side_effect,
            ]
        )

    def test_issue_edited_mid_run(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = self._llm_with_hook(
            lambda: gh.issue.update(body="Actually build something else entirely.")
        )
        outcome = run(world, gh, llm)
        assert outcome.failure_reason == "issue_edited"
        assert outcome.exit_code == 1
        assert "edited" in gh.comments[-1]
        assert_nothing_pushed(world, gh)

    def test_issue_closed_mid_run(self, world: World) -> None:
        gh = FakeGitHub(world)
        outcome = run(world, gh, self._llm_with_hook(lambda: gh.issue.update(state="closed")))
        assert outcome.failure_reason == "issue_closed"
        assert_nothing_pushed(world, gh)

    def test_label_removed_mid_run_stops_before_pushing(self, world: World) -> None:
        gh = FakeGitHub(world)

        def remove_label() -> None:
            gh.issue["labels"] = [x for x in gh.issue["labels"] if x["name"] != "devpilot"]

        outcome = run(world, gh, self._llm_with_hook(remove_label))
        assert outcome.failure_reason == "label_removed"
        assert_nothing_pushed(world, gh)

    def test_issue_deleted_mid_run(self, world: World) -> None:
        gh = FakeGitHub(world)

        def delete() -> None:
            gh.issue = None  # type: ignore[assignment]

        outcome = run(world, gh, self._llm_with_hook(delete))
        assert outcome.failure_reason == "issue_deleted"
        assert_nothing_pushed(world, gh)

    def test_base_moved_is_rebased_and_retested(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = self._llm_with_hook(
            lambda: push_to_origin_main(world, {"docs/new.md": "from someone else\n"})
        )
        outcome = run(world, gh, llm)
        assert outcome.status == ExecutionStatus.PR_OPENED
        branch = "devpilot/issue-7-add-greeting"
        tree = _git(world.origin, "ls-tree", "-r", "--name-only", branch).split()
        assert "docs/new.md" in tree and "src/greet.py" in tree
        # Our commit sits on top of the new main.
        assert _git(world.origin, "merge-base", "--is-ancestor", "main", branch) == ""

    def test_rebase_conflict_stops_without_pushing(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = self._llm_with_hook(
            lambda: push_to_origin_main(
                world, {"src/greet.py": "def hello():\n    return 'hi!!'\n"}
            )
        )
        outcome = run(world, gh, llm)
        assert outcome.failure_reason == "rebase_conflict"
        assert_nothing_pushed(world, gh)
        assert _git(world.ws, "status", "--porcelain") == ""  # rebase aborted cleanly

    def test_tests_that_break_after_rebase_stop_the_run(self, world: World) -> None:
        gh = FakeGitHub(world)
        # Someone changes check.py so that the new greeting no longer satisfies it.
        llm = self._llm_with_hook(
            lambda: push_to_origin_main(world, {"check.py": "import sys\nsys.exit(1)\n"})
        )
        outcome = run(world, gh, llm)
        assert outcome.status == ExecutionStatus.TESTS_FAILED
        assert "after rebasing" in outcome.message
        assert_nothing_pushed(world, gh)

    def test_push_failure_is_reported_as_failure(self, world: World) -> None:
        gh = FakeGitHub(world)
        llm = self._llm_with_hook(
            lambda: _git(
                world.ws, "remote", "set-url", "origin", (world.root / "gone.git").as_posix()
            )
        )
        outcome = run(world, gh, llm)
        assert outcome.status == ExecutionStatus.FAILED
        assert outcome.failure_reason == "push_failed"
        assert outcome.branch is None
        assert gh.created_prs == []
        assert "No pull request was created" in gh.comments[-1]


# ── H. pull request problems ────────────────────────────────


class TestPullRequestFailures:
    def test_pr_creation_failure_preserves_the_pushed_branch(self, world: World) -> None:
        gh = FakeGitHub(world)
        gh.create_pr_error = GitHubError(
            FailureReason.PERMISSION_DENIED, "Resource not accessible", status_code=403
        )
        outcome = run(world, gh, FakeLLM(write_and_finish("src/greet.py", GOOD_GREET)))
        assert outcome.status == ExecutionStatus.FAILED
        assert outcome.failure_reason == "pr_creation_failed"
        assert outcome.branch == "devpilot/issue-7-add-greeting"
        assert "devpilot/issue-7-add-greeting" in gh.comments[-1]
        assert "no pull request exists" in gh.comments[-1]

    def test_422_without_an_existing_pr_is_a_failure(self, world: World) -> None:
        gh = FakeGitHub(world)
        gh.create_pr_error = GitHubError(
            FailureReason.INTERNAL_ERROR, "validation failed", status_code=422
        )
        outcome = run(world, gh, FakeLLM(write_and_finish("src/greet.py", GOOD_GREET)))
        assert outcome.failure_reason == "pr_creation_failed"

    def test_nothing_is_ever_merged(self, world: World) -> None:
        gh = FakeGitHub(world)
        main_before = _git(world.origin, "rev-parse", "main").strip()
        run(world, gh, FakeLLM(write_and_finish("src/greet.py", GOOD_GREET)))
        assert not hasattr(gh, "merge_pull_request")
        assert _git(world.origin, "rev-parse", "main").strip() == main_before
