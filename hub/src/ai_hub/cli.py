"""Typer CLI for the AI Hub engine."""

from __future__ import annotations

from pathlib import Path  # noqa: TCH003 — Typer needs Path at runtime for CLI arg parsing
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel

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


@app.command()
def review() -> None:
    """Run the AI review pipeline (placeholder)."""
    console.print("[yellow]Not yet implemented — coming in M2/M3.[/]")
    raise typer.Exit(code=0)


@app.command()
def gate() -> None:
    """Evaluate the quality gate (placeholder)."""
    console.print("[yellow]Not yet implemented — coming in M3.[/]")
    raise typer.Exit(code=0)


@app.command()
def report() -> None:
    """Generate the PR report (placeholder)."""
    console.print("[yellow]Not yet implemented — coming in M3.[/]")
    raise typer.Exit(code=0)


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
