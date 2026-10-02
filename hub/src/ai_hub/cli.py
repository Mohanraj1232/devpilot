"""Typer CLI for the AI Hub engine."""

from __future__ import annotations

from pathlib import Path  # noqa: TCH003 — Typer needs Path at runtime for CLI arg parsing
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel

from ai_hub.config.schema import HubConfig  # noqa: TCH001 — Typer resolves annotations
from ai_hub.errors import ConfigError

app = typer.Typer(
    name="ai-hub", help="AI Hub — DevPilot's code-review and quality-analysis engine."
)
console = Console()

ConfigPath = Annotated[
    Path,
    typer.Argument(
        help="Path to the .ai-review/config.yml file to validate.",
        exists=True,
        readable=True,
    ),
]


@app.command()
def validate_config(config_path: ConfigPath) -> None:
    """Validate a repository configuration file."""
    from ai_hub.config.loader import load_config

    try:
        config, version = load_config(config_path)
    except ConfigError as exc:
        console.print(
            Panel(f"[red bold]Config validation FAILED[/]\n\n{exc.message}", title="Error")
        )
        if exc.details.get("errors"):
            for err in exc.details["errors"]:
                loc = " → ".join(str(part) for part in err.get("loc", []))
                console.print(f"  [red]•[/] {loc}: {err.get('msg', '')}")
        raise typer.Exit(code=1) from None

    console.print(
        Panel(
            f"[green bold]Config is valid[/]\n\n"
            f"Version hash: [cyan]{version}[/]\n"
            f"Review mode: {config.review_mode}\n"
            f"Quality gate: {'enabled' if config.quality_gate.enabled else 'disabled'}\n"
            f"DevPilot: {'enabled' if config.devpilot.enabled else 'disabled'}",
            title="✓ Validation passed",
        )
    )


WorkspaceOpt = Annotated[
    Path,
    typer.Option("--workspace", envvar="GITHUB_WORKSPACE", help="The checked-out repository."),
]
InputsOpt = Annotated[
    Path, typer.Option("--inputs", help="Directory written by `ai-hub review prepare`.")
]
OutOpt = Annotated[Path, typer.Option("--out", help="Directory to write results to.")]

review_app = typer.Typer(help="Stages of the pull-request review pipeline.", no_args_is_help=True)
app.add_typer(review_app, name="review")


def _stage_inputs(inputs: Path) -> tuple[HubConfig, list[str]]:
    """Trusted config and changed files for an analysis stage (from the prepare output)."""
    import json

    from ai_hub.review.outcome import load_config_for_stage

    try:
        config = load_config_for_stage(inputs.resolve())
        changed = json.loads((inputs / "changed-files.json").read_text("utf-8"))
    except (ConfigError, OSError, ValueError) as exc:
        console.print(f"[red]Cannot read the review inputs in {inputs}: {exc}[/]")
        raise typer.Exit(code=1) from None
    return config, [str(f) for f in changed]


@review_app.command("prepare")
def review_prepare(
    workspace: WorkspaceOpt = Path("."),
    out: OutOpt = Path("review-input"),
    config_path: Annotated[
        str, typer.Option("--config-path", help="Config file path inside the repository.")
    ] = ".ai-review/config.yml",
) -> None:
    """Capture the PR diff and the trusted (base-branch) configuration."""
    from ai_hub.errors import HubError
    from ai_hub.review.inputs import prepare_review_inputs

    try:
        result = prepare_review_inputs(workspace.resolve(), out.resolve(), config_path=config_path)
    except HubError as exc:
        console.print(Panel(f"[red bold]Prepare failed[/]\n\n{exc.message}", title="Error"))
        raise typer.Exit(code=1) from None

    state = "valid" if result.config_valid else "[red]INVALID[/]"
    console.print(
        f"{len(result.changed_files)} changed file(s), {result.changed_lines} changed line(s); "
        f"config from {result.config_source} is {state}"
    )
    for error in result.config_errors:
        console.print(f"  [red]•[/] {error}")


@review_app.command("static")
def review_static(
    workspace: WorkspaceOpt = Path("."),
    inputs: InputsOpt = Path("review-input"),
    out: OutOpt = Path("review-results"),
) -> None:
    """Run the configured static-analysis tools."""
    _run_analysis_command("static", workspace, inputs, out)


@review_app.command("security")
def review_security(
    workspace: WorkspaceOpt = Path("."),
    inputs: InputsOpt = Path("review-input"),
    out: OutOpt = Path("review-results"),
) -> None:
    """Run the configured security-analysis tools."""
    _run_analysis_command("security", workspace, inputs, out)


def _run_analysis_command(kind: str, workspace: Path, inputs: Path, out: Path) -> None:
    from ai_hub.review.pipeline import run_analysis
    from ai_hub.review.results import write_result

    config, changed = _stage_inputs(inputs)
    findings, check = run_analysis(kind, workspace.resolve(), changed, config)
    write_result(out, check, findings)
    console.print(f"{kind}: [bold]{check.status.value}[/] — {check.summary}")


@review_app.command("tests")
def review_tests(
    workspace: WorkspaceOpt = Path("."),
    inputs: InputsOpt = Path("review-input"),
    out: OutOpt = Path("review-results"),
) -> None:
    """Run the test suite and measure coverage."""
    from ai_hub.review.pipeline import run_tests_stage
    from ai_hub.review.results import write_result

    config, _ = _stage_inputs(inputs)
    findings, check, coverage = run_tests_stage(workspace.resolve(), config)
    write_result(out, check, findings, coverage_pct=coverage)
    cov = f", coverage {coverage:.1f}%" if coverage is not None else ""
    console.print(f"tests: [bold]{check.status.value}[/]{cov}")


@review_app.command("ai")
def review_ai(
    workspace: WorkspaceOpt = Path("."),
    inputs: InputsOpt = Path("review-input"),
    out: OutOpt = Path("review-results"),
) -> None:
    """AI code review of the PR diff with Claude on Amazon Bedrock."""
    import os

    from ai_hub.analysis.diff import parse_unified_diff
    from ai_hub.models import CheckResult, CheckStatus
    from ai_hub.review.pipeline import normalize_path
    from ai_hub.review.results import write_result

    config, _ = _stage_inputs(inputs)

    def finish(findings: list, check: CheckResult) -> None:  # type: ignore[type-arg]
        write_result(out, check, findings)
        console.print(f"ai_review: [bold]{check.status.value}[/] — {check.summary}")

    model_id = os.environ.get("BEDROCK_MODEL_ID", "")
    if not model_id:
        finish(
            [],
            CheckResult(
                name="ai_review",
                status=CheckStatus.ERROR,
                summary="BEDROCK_MODEL_ID is not configured",
            ),
        )
        return

    from ai_hub.llm.bedrock import BedrockClient
    from ai_hub.review.ai_review import run_ai_review

    try:
        file_diffs = parse_unified_diff((inputs / "diff.patch").read_text("utf-8"))
        client = BedrockClient(model_id, region=os.environ.get("AWS_REGION"))
        findings, check = run_ai_review(client, file_diffs, config)
    except Exception as exc:  # never crash the job: an ERROR check fails the gate instead
        finish(
            [],
            CheckResult(
                name="ai_review",
                status=CheckStatus.ERROR,
                summary=f"AI review failed unexpectedly ({type(exc).__name__})",
            ),
        )
        return

    root = workspace.resolve()
    finish([f.model_copy(update={"file": normalize_path(f.file, root)}) for f in findings], check)


@app.command("gate")
def gate(
    inputs: InputsOpt = Path("review-input"),
    results: Annotated[
        Path, typer.Option("--results", help="Directory containing all stage results.")
    ] = Path("review-results"),
    out: OutOpt = Path("review-outcome"),
    fork_pr: Annotated[
        bool, typer.Option("--fork-pr", help="The PR comes from a fork (AI review cannot run).")
    ] = False,
) -> None:
    """Merge the stage results, compute the scores and evaluate the quality gate."""
    import json

    from ai_hub.report.publisher import render_summary
    from ai_hub.report.sarif import generate_sarif
    from ai_hub.review.outcome import compute_outcome

    outcome = compute_outcome(inputs.resolve(), results.resolve(), fork_pr=fork_pr)
    out.mkdir(parents=True, exist_ok=True)
    (out / "outcome.json").write_text(json.dumps(outcome.to_dict(), indent=2), "utf-8")
    (out / "summary.md").write_text(render_summary(outcome), "utf-8")
    (out / "findings.sarif").write_text(json.dumps(generate_sarif(outcome.findings)), "utf-8")

    colour = "green" if outcome.gate_result.value == "pass" else "red"
    console.print(
        Panel(
            f"[{colour} bold]Gate: {outcome.gate_result.value.upper()}[/]\n"
            + "\n".join(f"- {r}" for r in outcome.gate_reasons),
            title="Quality gate",
        )
    )


@app.command("report")
def report(
    outcome_path: Annotated[
        Path, typer.Option("--outcome", help="outcome.json written by `ai-hub gate`.")
    ] = Path("review-outcome/outcome.json"),
    repo: Annotated[
        str, typer.Option("--repo", envvar="GITHUB_REPOSITORY", help="owner/name.")
    ] = "",
    pr: Annotated[int, typer.Option("--pr", help="Pull request number.")] = 0,
    head_sha: Annotated[
        str, typer.Option("--head-sha", help="SHA of the PR head commit (checks attach here).")
    ] = "",
    comment_author: Annotated[
        list[str],
        typer.Option(
            "--comment-author",
            help="Login(s) whose comment may be updated as the sticky summary.",
        ),
    ] = ["github-actions[bot]"],  # noqa: B006 — Typer copies option defaults
) -> None:
    """Post the result to the PR and the dashboard; exit non-zero if the gate failed."""
    import json
    import logging
    import os

    from ai_hub.github.client import GitHubClient
    from ai_hub.models import GateResult
    from ai_hub.report.dashboard import build_review_payload
    from ai_hub.report.publisher import publish, render_summary
    from ai_hub.review.outcome import ReviewOutcome

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        outcome = ReviewOutcome.from_dict(json.loads(outcome_path.read_text("utf-8")))
    except (OSError, ValueError, KeyError) as exc:
        # No trustworthy verdict: fail closed.
        console.print(f"[red]Cannot read the gate outcome ({outcome_path}): {exc}[/]")
        raise typer.Exit(code=1) from None

    summary = render_summary(outcome)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(summary + "\n")

    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    details_url = f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else None

    token = os.environ.get("GITHUB_TOKEN", "")
    if token and repo and pr and head_sha:
        client = GitHubClient(
            token, repo, api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com")
        )
        published = publish(
            client,
            outcome,
            pr_number=pr,
            head_sha=head_sha,
            details_url=details_url,
            comment_authors=tuple(comment_author),
        )
        for problem in published.problems:
            console.print(f"[yellow]Could not publish: {problem}[/]")
        if published.read_only:
            console.print(
                "[yellow]The token is read-only (e.g. a pull request from a fork); "
                "the result is in the job summary.[/]"
            )
    else:
        console.print("[yellow]GitHub publishing skipped (token, repo, PR or head SHA missing).[/]")

    dashboard_url = os.environ.get("DASHBOARD_URL", "")
    dashboard_token = os.environ.get("DASHBOARD_TOKEN", "")
    if dashboard_url and dashboard_token and repo and pr and run_id.isdigit():
        from ai_hub.telemetry.dashboard_client import DashboardClient

        reply = DashboardClient(dashboard_url, dashboard_token).ingest_review_run(
            build_review_payload(
                outcome, repo=repo, pr_number=pr, head_sha=head_sha, run_id=int(run_id)
            )
        )
        if "error" in reply:
            console.print("[yellow]The dashboard could not be updated (best effort).[/]")

    colour = "green" if outcome.gate_result == GateResult.PASS else "red"
    console.print(f"[{colour} bold]Gate: {outcome.gate_result.value.upper()}[/]")
    raise typer.Exit(code=0 if outcome.gate_result == GateResult.PASS else 1)


@app.command()
def devpilot(
    repo: Annotated[
        str,
        typer.Option("--repo", envvar="GITHUB_REPOSITORY", help="owner/name of the repository."),
    ],
    issue: Annotated[
        int,
        typer.Option("--issue", envvar="DEVPILOT_ISSUE_NUMBER", help="Issue number to implement."),
    ],
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            envvar="GITHUB_WORKSPACE",
            help="Checked-out repository (cloned here if it does not exist).",
        ),
    ] = Path("."),
    config: Annotated[
        Path,
        typer.Option("--config", help="Path to .ai-review/config.yml (relative to workspace)."),
    ] = Path(".ai-review/config.yml"),
    artifacts: Annotated[
        Path, typer.Option("--artifacts", help="Directory for result.json, transcript and diff.")
    ] = Path("devpilot-artifacts"),
    standalone: Annotated[
        bool,
        typer.Option(
            "--standalone",
            envvar="DEVPILOT_STANDALONE",
            help="Run without a dashboard (local/sandbox only; skips registration checks).",
        ),
    ] = False,
    require_gitleaks: Annotated[
        bool,
        typer.Option(
            "--require-gitleaks",
            envvar="DEVPILOT_REQUIRE_GITLEAKS",
            help="Fail the secret scan if Gitleaks is not installed.",
        ),
    ] = False,
) -> None:
    """Run the DevPilot agent: turn a labelled GitHub issue into a pull request."""
    import logging
    import os
    import uuid

    from ai_hub.devpilot.orchestrator import DevPilotRunner, DevPilotSettings
    from ai_hub.github.client import GitHubClient
    from ai_hub.llm.bedrock import BedrockClient
    from ai_hub.telemetry.dashboard_client import DashboardClient

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    token = os.environ.get("DEVPILOT_BOT_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    model_id = os.environ.get("BEDROCK_MODEL_ID", "")
    if not token:
        console.print("[red]DEVPILOT_BOT_TOKEN is not set; cannot act as the DevPilot bot.[/]")
        raise typer.Exit(code=1)
    if not model_id:
        console.print("[red]BEDROCK_MODEL_ID is not set.[/]")
        raise typer.Exit(code=1)

    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    workspace = workspace.resolve()
    config_path = config if config.is_absolute() else workspace / config

    dashboard_url = os.environ.get("DASHBOARD_URL", "")
    dashboard_token = os.environ.get("DASHBOARD_TOKEN", "")
    dashboard = (
        DashboardClient(dashboard_url, dashboard_token)
        if dashboard_url and not standalone
        else None
    )

    settings = DevPilotSettings(
        repo=repo,
        issue_number=issue,
        workspace=workspace,
        execution_id=uuid.uuid4().hex[:16],
        git_token=token,
        artifacts_dir=artifacts.resolve(),
        config_path=config_path,
        workflow_run_id=int(run_id) if run_id.isdigit() else 0,
        run_url=f"{server_url}/{repo}/actions/runs/{run_id}" if run_id else None,
        server_url=server_url,
        standalone=standalone,
        require_gitleaks=require_gitleaks,
    )
    runner = DevPilotRunner(
        settings,
        gh=GitHubClient(token, repo, api_url=api_url),
        llm=BedrockClient(model_id, region=os.environ.get("AWS_REGION"), max_tokens=8192),
        dashboard=dashboard,
    )
    outcome = runner.run()

    console.print(
        Panel(
            f"Status: [bold]{outcome.status.value}[/]\n"
            f"Reason: {outcome.failure_reason or '-'}\n"
            f"PR: {outcome.pr_url or '-'}\n\n{outcome.message}",
            title="DevPilot result",
        )
    )
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"status={outcome.status.value}\n")
            fh.write(f"pr_number={outcome.pr_number or ''}\n")
            fh.write(f"execution_id={outcome.execution_id}\n")
    raise typer.Exit(code=outcome.exit_code)


if __name__ == "__main__":
    app()
