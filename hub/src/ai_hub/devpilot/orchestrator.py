"""DevPilot orchestrator — runs an issue through every stage to a pull request.

Stages (each can stop safely; see ``_Stop``):

  guard -> triage -> workspace -> plan -> implement + test/repair -> guardrails
  -> secret scan -> commit -> pre-push revalidation -> push -> pull request -> report

Any unexpected condition ends in ``_finalize``: state is preserved as artifacts, the
failure is reported on the issue and the dashboard, the lock is released, and nothing
is ever merged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from ai_hub.config.loader import load_config
from ai_hub.devpilot.agent import AgentResult, AgentSession
from ai_hub.devpilot.branch_guard import check_branch_ownership
from ai_hub.devpilot.duplicate import IN_PROGRESS_LABEL, check_for_duplicate
from ai_hub.devpilot.git_ops import (
    checkout_base,
    clone_repo,
    create_branch,
    exclude_generated_files,
    git_auth_env,
    make_branch_name,
    push_branch,
    rebase_on_base,
    stage_all,
    stage_and_commit,
    staged_changes,
    staged_diff_text,
)
from ai_hub.devpilot.guardrails import run_all_guardrails
from ai_hub.devpilot.llm_tasks import (
    ImplementationPlan,
    load_prompt,
    make_plan,
    triage_issue_llm,
)
from ai_hub.devpilot.lock import ExecutionLock
from ai_hub.devpilot.pr import build_pr_body
from ai_hub.devpilot.preflight import check_clean_tree, run_preflight_checks
from ai_hub.devpilot.repo_map import build_repo_map
from ai_hub.devpilot.secret_scan import scan_staged_diff
from ai_hub.devpilot.tester import RepairResult, TestRunResult, run_test_repair_loop, run_tests
from ai_hub.devpilot.tools import ToolPolicy
from ai_hub.devpilot.trigger import IssueSnapshot, check_trigger_preconditions, triage_issue
from ai_hub.devpilot.workspace import (
    ProjectType,
    detect_project_type,
    init_submodules,
    install_dependencies,
    is_empty_repo,
)
from ai_hub.errors import ConfigError, FailureReason, HubError
from ai_hub.models import CheckStatus, ExecutionStatus
from ai_hub.safety.redact import redact
from ai_hub.safety.untrusted import neutralize_mentions, wrap_untrusted

if TYPE_CHECKING:
    from pathlib import Path

    from ai_hub.config.schema import DevPilotConfig, HubConfig
    from ai_hub.github.client import GitHubClient
    from ai_hub.llm.bedrock import BedrockClient
    from ai_hub.telemetry.dashboard_client import DashboardClient

logger = logging.getLogger("ai_hub.devpilot")

_EXPECTED_NON_RUNS = {
    FailureReason.ISSUE_CLOSED,
    FailureReason.ISSUE_DELETED,
    FailureReason.LABEL_REMOVED,
    FailureReason.DUPLICATE_EXECUTION,
    FailureReason.EXISTING_PR,
    FailureReason.REPO_NOT_REGISTERED,
    FailureReason.REPO_DISABLED,
    FailureReason.REPO_EMPTY,
    FailureReason.UNSUPPORTED_PROJECT,
    FailureReason.ISSUE_NOT_ACTIONABLE,
    FailureReason.ISSUE_ALREADY_RESOLVED,
}
# Duplicate triggers must not spam the issue; a deleted issue cannot be commented on.
_SILENT_REASONS = {
    FailureReason.DUPLICATE_EXECUTION,
    FailureReason.ISSUE_DELETED,
    FailureReason.ISSUE_CLOSED,
    FailureReason.LABEL_REMOVED,
}


class _Stop(Exception):
    """Internal control flow: end the run safely with the given outcome."""

    def __init__(self, status: ExecutionStatus, reason: FailureReason, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message


@dataclass
class DevPilotSettings:
    repo: str
    issue_number: int
    workspace: Path
    execution_id: str
    git_token: str
    artifacts_dir: Path
    config_path: Path | None = None
    workflow_run_id: int = 0
    run_url: str | None = None
    server_url: str = "https://github.com"
    # Run without a dashboard (local/sandbox use only). Production runs fail closed.
    standalone: bool = False
    require_gitleaks: bool = False


@dataclass
class Outcome:
    status: ExecutionStatus
    message: str
    failure_reason: str | None = None
    execution_id: str = ""
    branch: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    attempts: int = 0
    test_status: str | None = None
    issue_hash: str = ""
    base_sha: str = ""
    limitations: list[str] = field(default_factory=list)
    timeline: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status in (ExecutionStatus.PR_OPENED, ExecutionStatus.PR_UPDATED)

    @property
    def exit_code(self) -> int:
        """0 for success and expected non-runs; 1 for failures a human should look at."""
        if self.succeeded or self.status == ExecutionStatus.NEEDS_CLARIFICATION:
            return 0
        if self.status == ExecutionStatus.BLOCKED:
            return 0 if self._is_expected_non_run() else 1
        return 1

    def _is_expected_non_run(self) -> bool:
        return self.failure_reason in {r.value for r in _EXPECTED_NON_RUNS}

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["exit_code"] = self.exit_code
        return data


def tighten_devpilot_config(config: DevPilotConfig, policy_json: dict[str, Any]) -> DevPilotConfig:
    """Apply dashboard policy to the repo config. The dashboard may only tighten, never loosen."""
    policy = policy_json.get("devpilot")
    if not isinstance(policy, dict):
        return config
    updates: dict[str, Any] = {}
    if policy.get("enabled") is False:
        updates["enabled"] = False
    if policy.get("allow_workflow_changes") is False:
        updates["allow_workflow_changes"] = False
    for key, floor in (("max_fix_attempts", 0), ("max_changed_files", 1), ("max_changed_lines", 1)):
        value = policy.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= floor:
            updates[key] = min(getattr(config, key), value)
    return config.model_copy(update=updates) if updates else config


class DevPilotRunner:
    """Runs one DevPilot execution. Create a new instance per run."""

    def __init__(
        self,
        settings: DevPilotSettings,
        *,
        gh: GitHubClient,
        llm: BedrockClient,
        dashboard: DashboardClient | None = None,
    ) -> None:
        self.s = settings
        self.gh = gh
        self.llm = llm
        self.dashboard = dashboard
        self.owner, _, self.repo_name = settings.repo.partition("/")
        self.git_env = git_auth_env(settings.git_token, server_url=settings.server_url)

        self.config: HubConfig | None = None
        self.config_version = ""
        self.issue: IssueSnapshot | None = None
        self.issue_hash = ""
        self.base_branch = "main"
        self.base_sha = ""
        self.branch: str | None = None
        self.test_command: str | None = None
        self.test_timeout = 900
        self.project: ProjectType | None = None
        self.session: AgentSession | None = None
        self.plan: ImplementationPlan | None = None

        self.attempts = 0
        self.test_status: str | None = None
        self.limitations: list[str] = []
        self.timeline: list[str] = []
        self.pr_number: int | None = None
        self.pr_url: str | None = None
        self.pushed = False
        self._pr_action = "opened"
        self._bot_identity: tuple[str, str] = (
            "devpilot-bot",
            "devpilot-bot@users.noreply.github.com",
        )
        self._repo_map = ""
        self._agent_summary = ""
        self._lock_key = ""

        self._started = False  # in-progress label added / lock taken
        self._label_added = False
        self._dashboard_row: int | None = None
        self._secret_blocked = False

    # ── public ────────────────────────────────────────────────

    def run(self) -> Outcome:
        try:
            outcome = self._run()
        except _Stop as stop:
            outcome = self._outcome(stop.status, stop.message, stop.reason)
        except HubError as exc:
            outcome = self._outcome(ExecutionStatus.FAILED, redact(exc.message), exc.reason)
        except Exception as exc:  # unexpected: stop safely, never leak internals
            logger.exception("Unexpected DevPilot failure")
            message = f"Unexpected internal error ({type(exc).__name__})"
            outcome = self._outcome(ExecutionStatus.FAILED, message, FailureReason.INTERNAL_ERROR)
        self._finalize(outcome)
        return outcome

    # ── helpers ───────────────────────────────────────────────

    def _step(self, text: str) -> None:
        logger.info("[devpilot] %s", text)
        self.timeline.append(text)

    def _stop(self, status: ExecutionStatus, reason: FailureReason, message: str) -> _Stop:
        return _Stop(status, reason, message)

    def _outcome(
        self, status: ExecutionStatus, message: str, reason: FailureReason | None = None
    ) -> Outcome:
        if status == ExecutionStatus.FAILED and reason in _EXPECTED_NON_RUNS:
            status = ExecutionStatus.BLOCKED
        return Outcome(
            status=status,
            message=message,
            failure_reason=reason.value if reason else None,
            execution_id=self.s.execution_id,
            branch=self.branch if self.pushed else None,
            pr_number=self.pr_number,
            pr_url=self.pr_url,
            attempts=self.attempts,
            test_status=self.test_status,
            issue_hash=self.issue_hash,
            base_sha=self.base_sha,
            limitations=list(self.limitations),
            timeline=list(self.timeline),
        )

    def _fetch_issue(self) -> IssueSnapshot:
        raw = self.gh.get_issue(self.s.issue_number)
        if raw is None:
            raise self._stop(
                ExecutionStatus.BLOCKED, FailureReason.ISSUE_DELETED, "Issue no longer exists"
            )
        return IssueSnapshot.from_api(raw)

    # ── main flow ─────────────────────────────────────────────

    def _run(self) -> Outcome:
        self._load_config()
        self._guard()
        self._triage()
        self._prepare_workspace()
        self._plan()
        self._implement_and_test()
        self._guardrails_and_commit()
        self._revalidate()
        self._push_and_open_pr()
        status = (
            ExecutionStatus.PR_UPDATED
            if self._pr_action == "updated"
            else ExecutionStatus.PR_OPENED
        )
        return self._outcome(status, f"Pull request #{self.pr_number} is ready for review")

    # 0. config ------------------------------------------------

    def _load_config(self) -> None:
        path = self.s.config_path
        if path is not None and not path.is_file():
            logger.info("No config at %s; using hub defaults", path)
            path = None
        try:
            self.config, self.config_version = load_config(path)
        except ConfigError as exc:
            detail = "; ".join(
                f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg', '')}"
                for e in exc.details.get("errors", [])
            )
            raise self._stop(
                ExecutionStatus.FAILED,
                FailureReason.CONFIG_INVALID,
                f"Invalid .ai-review/config.yml: {detail or exc.message}",
            ) from exc
        self.base_branch = self.config.devpilot.base_branch

    # 1. guard -------------------------------------------------

    def _guard(self) -> None:
        assert self.config is not None
        self._step("Checking repository access")
        repo = self.gh.get_repo()
        if str(repo.get("full_name", "")).lower() != self.s.repo.lower():
            raise self._stop(
                ExecutionStatus.FAILED,
                FailureReason.REPO_INACCESSIBLE,
                f"Repository was renamed or moved (now {repo.get('full_name')})",
            )
        if repo.get("archived") or repo.get("disabled"):
            raise self._stop(
                ExecutionStatus.FAILED,
                FailureReason.REPO_INACCESSIBLE,
                "Repository is archived or disabled (read-only)",
            )

        self.issue = self._fetch_issue()
        self.issue_hash = self.issue.body_hash

        user = self.gh.get_authenticated_user()
        login = str(user.get("login", ""))
        self._bot_identity = (login, f"{user.get('id', 0)}+{login}@users.noreply.github.com")
        permission = self.gh.get_collaborator_permission(login)
        bot_ok = permission in ("admin", "write")

        registered, dashboard_ok, dashboard_enabled = True, True, True
        devpilot_cfg = self.config.devpilot
        if not self.s.standalone:
            lookup = (
                self.dashboard.lookup_repository(self.owner, self.repo_name)
                if self.dashboard
                else None
            )
            if lookup is None or lookup.state == "unreachable":
                dashboard_ok = False
            elif lookup.state == "not_registered":
                registered = False
            else:
                dashboard_enabled = lookup.devpilot_enabled
                devpilot_cfg = tighten_devpilot_config(devpilot_cfg, lookup.policy_json)
                self.config = self.config.model_copy(update={"devpilot": devpilot_cfg})
        enabled = dashboard_enabled and devpilot_cfg.enabled

        open_prs = self.gh.list_open_pulls(head_prefix=f"devpilot/issue-{self.issue.number}")
        has_open_pr = any(
            p["head"]["ref"] == f"devpilot/issue-{self.issue.number}"
            or p["head"]["ref"].startswith(f"devpilot/issue-{self.issue.number}-")
            for p in open_prs
        )
        duplicate = check_for_duplicate(self.issue.labels)

        verdict = check_trigger_preconditions(
            self.issue,
            repo_registered=registered,
            devpilot_enabled=enabled,
            bot_is_collaborator=bot_ok,
            has_active_execution=duplicate.is_duplicate,
            has_open_pr=has_open_pr,
            dashboard_reachable=dashboard_ok,
        )
        if not verdict.allowed:
            reason = verdict.failure_reason or FailureReason.INTERNAL_ERROR
            raise self._stop(ExecutionStatus.BLOCKED, reason, verdict.reason or "Not allowed")

        self.base_branch = self.config.devpilot.base_branch
        base_sha = self.gh.get_branch_sha(self.base_branch)
        if base_sha is None:
            raise self._stop(
                ExecutionStatus.FAILED,
                FailureReason.CONFIG_INVALID,
                f"Base branch '{self.base_branch}' does not exist",
            )
        self.base_sha = base_sha
        self._take_lock()

    def _take_lock(self) -> None:
        assert self.issue is not None
        lock_key = ExecutionLock.make_key(self.s.repo, self.issue.number)
        if self.dashboard is not None and not self.s.standalone:
            if not self.dashboard.acquire_lock(self.s.repo, self.issue.number, self.s.execution_id):
                raise self._stop(
                    ExecutionStatus.BLOCKED,
                    FailureReason.DUPLICATE_EXECUTION,
                    "An active execution already exists for this issue",
                )
            result = self.dashboard.ingest_execution(
                {
                    "repo_full_name": self.s.repo,
                    "issue_number": self.issue.number,
                    "issue_hash": self.issue_hash,
                    "base_sha": self.base_sha,
                    "workflow_run_id": self.s.workflow_run_id,
                    "config_version": self.config_version,
                    "status": ExecutionStatus.RUNNING.value,
                }
            )
            if result.get("status_code") == 409:
                raise self._stop(
                    ExecutionStatus.BLOCKED,
                    FailureReason.DUPLICATE_EXECUTION,
                    "An active execution already exists for this issue",
                )
            if "error" in result:
                raise self._stop(
                    ExecutionStatus.FAILED,
                    FailureReason.DASHBOARD_UNREACHABLE,
                    "Could not register the execution with the dashboard",
                )
            self._dashboard_row = result.get("id")
        self._lock_key = lock_key
        self._started = True

        self.gh.add_labels(self.issue.number, [IN_PROGRESS_LABEL])
        self._label_added = True
        started = "🤖 **DevPilot** started working on this issue."
        if self.s.run_url:
            started += f" [Workflow run]({self.s.run_url})"
        self.gh.add_issue_comment(self.issue.number, started)
        self._step("Execution started")

    # 2. triage ------------------------------------------------

    def _triage(self) -> None:
        assert self.issue is not None
        self._step("Triaging issue")
        heuristic = triage_issue(self.issue)
        if not heuristic.actionable:
            raise self._needs_info(heuristic.missing_info)
        decision = triage_issue_llm(self.llm, self.issue)
        if decision.already_resolved_hint:
            raise self._stop(
                ExecutionStatus.NEEDS_CLARIFICATION,
                FailureReason.ISSUE_ALREADY_RESOLVED,
                "The issue text suggests this work is already done. "
                "Please confirm it still needs to be implemented.",
            )
        if not decision.actionable:
            raise self._needs_info(decision.missing_info or [decision.rationale or "Unclear scope"])

    def _needs_info(self, missing: list[str]) -> _Stop:
        bullets = "\n".join(f"- {neutralize_mentions(redact(m))}" for m in missing)
        return self._stop(
            ExecutionStatus.NEEDS_CLARIFICATION,
            FailureReason.ISSUE_NOT_ACTIONABLE,
            f"This issue needs more detail before DevPilot can implement it:\n{bullets}",
        )

    # 3. workspace ---------------------------------------------

    def _prepare_workspace(self) -> None:
        assert self.config is not None
        ws = self.s.workspace
        self._step("Preparing workspace")
        if not (ws / ".git").exists():
            clone_repo(
                f"{self.s.server_url.rstrip('/')}/{self.s.repo}.git",
                ws,
                token=self.s.git_token,
                base_branch=self.base_branch,
                server_url=self.s.server_url,
            )

        tree = check_clean_tree(ws)
        if not tree.passed:
            raise self._stop(
                ExecutionStatus.FAILED,
                FailureReason.DIRTY_WORKSPACE,
                "Workspace has uncommitted changes that DevPilot did not produce",
            )
        self.base_sha = checkout_base(ws, self.base_branch, env=self.git_env)

        if is_empty_repo(ws):
            raise self._stop(
                ExecutionStatus.BLOCKED,
                FailureReason.REPO_EMPTY,
                "Repository is empty; DevPilot will not invent a project structure",
            )
        if not init_submodules(ws):
            raise self._stop(
                ExecutionStatus.FAILED,
                FailureReason.SUBMODULE_FAILURE,
                "Git submodules could not be initialised",
            )

        cfg = self.config
        self.project = detect_project_type(ws)
        self.test_command = (
            cfg.devpilot.test_command
            or cfg.tests.command
            or (self.project.test_command if self.project else None)
        )
        self.test_timeout = cfg.tests.timeout_minutes * 60
        if self.project is None and self.test_command is None:
            raise self._stop(
                ExecutionStatus.BLOCKED,
                FailureReason.UNSUPPORTED_PROJECT,
                "Project type is not supported and no devpilot.test_command is configured",
            )

        if self.project is not None:
            self._step(f"Installing dependencies ({self.project.name})")
            install = install_dependencies(ws, self.project)
            if not install.ok:
                raise self._stop(
                    ExecutionStatus.FAILED,
                    FailureReason.DEPENDENCY_INSTALL_FAILED,
                    f"Dependency installation failed:\n{install.output[-1200:]}",
                )
        # Files created by installs/tests must not end up in the commit.
        exclude_generated_files(ws)

    # 4. plan --------------------------------------------------

    def _plan(self) -> None:
        assert self.issue is not None
        self._step("Planning")
        repo_map = build_repo_map(self.s.workspace, f"{self.issue.title}\n{self.issue.body}")
        self._repo_map = repo_map
        self.plan = make_plan(self.llm, self.issue, repo_map)

        name = make_branch_name(self.issue.number, self.issue.title)
        if self.gh.get_branch_sha(name) is not None:
            name = make_branch_name(self.issue.number, self.issue.title, self.s.execution_id)
        self.branch = name
        create_branch(self.s.workspace, name)
        self._step(f"Working on branch {name}")

    # 5. implement + test --------------------------------------

    def _tool_test_runner(self) -> dict[str, Any]:
        assert self.test_command is not None
        run = run_tests(self.s.workspace, self.test_command, timeout_seconds=self.test_timeout)
        return {"status": run.status.value, "no_tests": run.no_tests, "output": run.output[-4000:]}

    def _implement_and_test(self) -> None:
        assert self.issue is not None and self.plan is not None and self.config is not None
        cfg = self.config.devpilot
        policy = ToolPolicy(
            workspace=self.s.workspace,
            allow_workflow_changes=cfg.allow_workflow_changes,
            test_runner=self._tool_test_runner if self.test_command else None,
        )
        self.session = AgentSession(self.llm, load_prompt("agent_system"), policy)

        task = "\n\n".join(
            [
                "Implement this GitHub issue.",
                wrap_untrusted(
                    "untrusted_issue",
                    f"Title: {self.issue.title}\n\n{self.issue.body}",
                    max_chars=10_000,
                ),
                f"Plan:\n{self.plan.approach}\nFiles: {', '.join(self.plan.files_to_touch)}\n"
                f"Verification: {self.plan.test_strategy}",
                wrap_untrusted("untrusted_repo_map", self._repo_map, max_chars=14_000),
            ]
        )
        self._step("Implementing")
        result = self.session.send(task)
        self._agent_summary = result.summary
        self._require_agent_ok(result)
        if not self._has_changes():
            summary = neutralize_mentions(redact(result.summary))
            raise self._stop(
                ExecutionStatus.FAILED,
                FailureReason.NO_CHANGES,
                f"DevPilot made no code changes. Agent summary: {summary}",
            )

        if not self.test_command:
            self.test_status = CheckStatus.SKIPPED.value
            self.limitations.append("No test command is available; changes are unverified.")
            return

        self._step("Running tests")
        agent_failure: list[AgentResult] = []
        session = self.session

        def repair(run: TestRunResult, attempt: int) -> bool:
            self._step(f"Tests failed (attempt {attempt}); asking the agent to fix")
            res = session.send(
                f"The test command failed (attempt {attempt} of {cfg.max_fix_attempts + 1}). "
                "Fix the code so the tests pass, run the tests, then call finish.\n\n"
                + wrap_untrusted("untrusted_test_output", run.output, max_chars=6000)
            )
            if not res.finished:
                agent_failure.append(res)
                return False
            return True

        outcome = run_test_repair_loop(
            self.s.workspace,
            self.test_command,
            max_attempts=cfg.max_fix_attempts + 1,
            timeout_seconds=self.test_timeout,
            repair=repair if cfg.max_fix_attempts > 0 else None,
        )
        self.attempts = len(outcome.history)
        self.test_status = outcome.final_status.value
        self._interpret_tests(outcome, agent_failure)

    def _interpret_tests(self, outcome: RepairResult, agent_failure: list[AgentResult]) -> None:
        status = outcome.final_status
        if status == CheckStatus.SUCCESS:
            if outcome.is_flaky:
                self.limitations.append(
                    f"Flaky tests detected: {outcome.flaky_evidence}. "
                    "A passing result is not fully reliable."
                )
            self._step("Tests passed")
            return
        if status == CheckStatus.SKIPPED:
            self.limitations.append("No automated tests were found; changes are unverified.")
            return
        if status == CheckStatus.ERROR:
            if "timed out" in outcome.test_output:
                raise self._stop(
                    ExecutionStatus.FAILED, FailureReason.TEST_TIMEOUT, outcome.test_output.strip()
                )
            self.limitations.append(
                "The test command could not be run in this environment; "
                "automated verification was unavailable."
            )
            return

        # FAILED
        self._save_diff()
        if agent_failure:
            res = agent_failure[0]
            raise self._stop(
                ExecutionStatus.FAILED,
                FailureReason(res.failure_reason or FailureReason.CANNOT_SOLVE.value),
                f"Agent stopped while fixing test failures: {redact(res.summary)}",
            )
        if outcome.stop_reason == "repeated_failed_state":
            raise self._stop(
                ExecutionStatus.TESTS_FAILED,
                FailureReason.CANNOT_SOLVE,
                "The agent reproduced a previously failed state; stopping instead of looping.",
            )
        raise self._stop(
            ExecutionStatus.TESTS_FAILED,
            FailureReason.TESTS_FAILED,
            f"Tests still failing after {self.attempts} run(s):\n{outcome.test_output[-1500:]}",
        )

    def _require_agent_ok(self, result: AgentResult) -> None:
        if result.finished:
            return
        reason = FailureReason(result.failure_reason or FailureReason.CANNOT_SOLVE.value)
        raise self._stop(ExecutionStatus.FAILED, reason, redact(result.summary))

    def _has_changes(self) -> bool:
        stage_all(self.s.workspace)
        return bool(staged_changes(self.s.workspace).files)

    # 6. guardrails + secrets + commit -------------------------

    def _guardrails_and_commit(self) -> None:
        assert self.issue is not None and self.plan is not None and self.config is not None
        ws = self.s.workspace
        cfg = self.config.devpilot
        self._step("Running guardrails")
        stage_all(ws)
        changes = staged_changes(ws)
        if not changes.files:
            raise self._stop(
                ExecutionStatus.FAILED, FailureReason.NO_CHANGES, "DevPilot produced no changes"
            )

        planned = [p.replace("\\", "/").lstrip("./") for p in self.plan.files_to_touch]
        guard = run_all_guardrails(
            changes.files,
            changes.lines,
            planned,
            max_files=cfg.max_changed_files,
            max_lines=cfg.max_changed_lines,
            allow_workflow_changes=cfg.allow_workflow_changes,
            deleted_files=changes.deleted,
        )
        violations = list(guard.violations)
        reasons = list(guard.reasons)
        if changes.binary:
            violations.append(f"Binary files are not allowed: {changes.binary[:5]}")
            reasons.append(FailureReason.FORBIDDEN_CHANGE)
        if violations:
            self._save_diff()
            raise self._stop(
                ExecutionStatus.FAILED,
                reasons[0],
                "Guardrails rejected the change:\n" + "\n".join(f"- {v}" for v in violations),
            )

        self._step("Scanning for secrets")
        scan = scan_staged_diff(ws, require_gitleaks=self.s.require_gitleaks)
        if not scan.clean:
            self._secret_blocked = True
            raise self._stop(
                ExecutionStatus.FAILED,
                FailureReason.SECRET_DETECTED,
                "Possible secrets were found in the generated change; nothing was committed:\n"
                + "\n".join(f"- {redact(f)}" for f in scan.findings[:10]),
            )
        if not scan.gitleaks_ran:
            self.limitations.append("Gitleaks was unavailable; only the built-in secret scan ran.")

        title = self.issue.title.strip().replace("\n", " ")[:60]
        sha = stage_and_commit(
            ws,
            f"DevPilot: {title} (#{self.issue.number})",
            execution_id=self.s.execution_id,
            identity=self._bot_identity,
        )
        if sha is None:
            raise self._stop(ExecutionStatus.FAILED, FailureReason.NO_CHANGES, "Nothing to commit")
        ownership = check_branch_ownership(ws, self.base_branch, self.s.execution_id)
        if not ownership.is_owned:
            raise self._stop(
                ExecutionStatus.FAILED,
                FailureReason.FOREIGN_COMMITS,
                "The branch contains commits not made by this execution",
            )
        self._step("Committed changes")

    # 7. revalidate --------------------------------------------

    def _revalidate(self) -> None:
        assert self.issue is not None
        ws = self.s.workspace
        self._step("Revalidating before push")
        current = self._fetch_issue()
        current_base = self.gh.get_branch_sha(self.base_branch)
        if current_base is None:
            raise self._stop(
                ExecutionStatus.FAILED,
                FailureReason.REPO_INACCESSIBLE,
                f"Base branch '{self.base_branch}' disappeared during execution",
            )

        # Only rebase if nothing else invalidates the run.
        early = run_preflight_checks(current, self.issue_hash, current_base, current_base, ws)
        if not early.passed:
            raise self._stop(
                ExecutionStatus.FAILED,
                early.failure_reason or FailureReason.INTERNAL_ERROR,
                "; ".join(early.failures),
            )

        if current_base != self.base_sha:
            self._step("Base branch moved; rebasing")
            if not rebase_on_base(ws, self.base_branch, env=self.git_env):
                raise self._stop(
                    ExecutionStatus.FAILED,
                    FailureReason.REBASE_CONFLICT,
                    f"The base branch moved and rebasing onto '{self.base_branch}' conflicts. "
                    "Resolve manually or re-run DevPilot.",
                )
            if self.test_command:
                run = run_tests(ws, self.test_command, timeout_seconds=self.test_timeout)
                if run.status == CheckStatus.FAILED:
                    raise self._stop(
                        ExecutionStatus.TESTS_FAILED,
                        FailureReason.TESTS_FAILED,
                        "Tests fail after rebasing onto the updated base branch:\n"
                        + run.output[-1200:],
                    )
            self.base_sha = current_base

    # 8. push + PR ---------------------------------------------

    def _pr_body(self) -> str:
        assert self.issue is not None and self.plan is not None
        summary = neutralize_mentions(redact(self._agent_summary))
        plan = f"{neutralize_mentions(self.plan.approach)}\n\nFiles: " + ", ".join(
            f"`{p}`" for p in self.plan.files_to_touch
        )
        if self.test_status == CheckStatus.SUCCESS.value:
            evidence: str | None = (
                f"`{self.test_command}` passed"
                + (f" after {self.attempts} run(s)" if self.attempts > 1 else "")
                + "."
            )
        else:
            evidence = None
        body = build_pr_body(
            self.issue.number,
            summary or "Implementation generated by DevPilot.",
            plan=plan,
            test_evidence=evidence,
            limitations=self.limitations or None,
        )
        footer = (
            "\n---\n_Generated by DevPilot. This pull request requires human review and approval "
            "before merging._\n"
            f"<!-- devpilot-execution: {self.s.execution_id} -->\n"
        )
        return body + footer

    def _push_and_open_pr(self) -> None:
        assert self.issue is not None and self.branch is not None
        ws = self.s.workspace
        self._step("Pushing branch")
        push_branch(ws, self.branch, env=self.git_env)
        self.pushed = True

        title = f"DevPilot: {self.issue.title.strip()}"[:100]
        body = self._pr_body()
        try:
            pr = self.gh.create_pull_request(
                title=title, body=body, head=self.branch, base=self.base_branch
            )
            self._pr_action = "opened"
        except HubError as exc:
            existing = None
            if getattr(exc, "status_code", None) == 422:
                existing = self.gh.find_open_pull_for_branch(self.branch)
            if existing is None:
                raise self._stop(
                    ExecutionStatus.FAILED,
                    FailureReason.PR_CREATION_FAILED,
                    f"Branch `{self.branch}` was pushed but the pull request could not be created: "
                    f"{redact(exc.message)}",
                ) from exc
            pr = self.gh.update_pull_request(existing["number"], title=title, body=body)
            self._pr_action = "updated"

        self.pr_number = pr["number"]
        self.pr_url = pr.get("html_url")
        try:
            self.gh.add_labels(self.pr_number, ["devpilot"])
        except HubError:
            logger.warning("Could not label the pull request")
        self._step(f"Pull request #{self.pr_number} {self._pr_action}")

    # ── reporting / cleanup ───────────────────────────────────

    def _save_diff(self) -> None:
        if self._secret_blocked:
            return
        try:
            stage_all(self.s.workspace)  # include edits made since the last staging
            diff = redact(staged_diff_text(self.s.workspace))
            self.s.artifacts_dir.mkdir(parents=True, exist_ok=True)
            (self.s.artifacts_dir / "diff.patch").write_text(diff, encoding="utf-8")
        except (OSError, HubError):
            logger.warning("Could not save diff artifact")

    def _comment_for(self, outcome: Outcome) -> str:
        run = f"\n\n[Workflow run]({self.s.run_url})" if self.s.run_url else ""
        if outcome.succeeded:
            lines = [f"✅ DevPilot opened {outcome.pr_url or f'#{outcome.pr_number}'} for review."]
            if outcome.limitations:
                lines += ["", "**Limitations**"] + [f"- {x}" for x in outcome.limitations]
            lines.append(
                "\nThe pull request will go through AI review and the quality gate; "
                "a human must approve it before merging."
            )
            return "\n".join(lines) + run
        if outcome.status == ExecutionStatus.NEEDS_CLARIFICATION:
            return (
                f"❓ {outcome.message}\n\nUpdate the issue, then remove and re-apply the "
                f"`devpilot` label to run again." + run
            )
        if outcome.status == ExecutionStatus.BLOCKED:
            return f"⏸️ DevPilot did not run: {outcome.message}" + run
        extra = (
            f"\n\nThe branch `{outcome.branch}` was pushed, but no pull request exists."
            if outcome.branch
            else "\n\nNo pull request was created and nothing was merged."
        )
        return (
            f"❌ DevPilot stopped: {outcome.message}\n\nReason code: `{outcome.failure_reason}`"
            + extra
            + run
        )

    def _finalize(self, outcome: Outcome) -> None:
        """Preserve state, report, and release everything. Never raises."""
        outcome.timeline = list(self.timeline)
        reason = outcome.failure_reason
        silent = reason in {r.value for r in _SILENT_REASONS}

        self._safely(self._write_artifacts, outcome)
        if self.issue is not None and self._started:
            number = self.issue.number
            self._safely(self.gh.add_issue_comment, number, self._comment_for(outcome))
            if self._label_added:
                self._safely(self.gh.remove_label, number, IN_PROGRESS_LABEL)
            if outcome.status == ExecutionStatus.NEEDS_CLARIFICATION:
                self._safely(self.gh.remove_label, number, "devpilot")
        elif self.issue is not None and not silent:
            # Blocked at the guard before we took any lock: still tell the person who labelled.
            self._safely(self.gh.add_issue_comment, self.issue.number, self._comment_for(outcome))

        if self.dashboard is not None and not self.s.standalone:
            if self._dashboard_row is not None:
                self._safely(
                    self.dashboard.update_execution,
                    self._dashboard_row,
                    {
                        "status": outcome.status.value,
                        "branch": outcome.branch,
                        "pr_number": outcome.pr_number,
                        "attempts": outcome.attempts,
                        "test_status": outcome.test_status,
                        "failure_reason": outcome.failure_reason,
                    },
                )
            if self._started:
                self._safely(self.dashboard.release_lock, self._lock_key)

    def _write_artifacts(self, outcome: Outcome) -> None:
        out = self.s.artifacts_dir
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.json").write_text(
            redact(json.dumps(outcome.to_dict(), indent=2, default=str)), encoding="utf-8"
        )
        if self.session is not None:
            (out / "transcript.json").write_text(
                redact(json.dumps(self.session.messages, indent=1, default=str)), encoding="utf-8"
            )

    @staticmethod
    def _safely(func: Any, *args: Any) -> None:
        try:
            func(*args)
        except Exception as exc:  # reporting must never mask the real outcome
            logger.warning("Cleanup step %s failed: %s", getattr(func, "__name__", func), exc)
