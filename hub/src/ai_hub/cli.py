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
def devpilot() -> None:
    """Run the DevPilot agent (placeholder)."""
    console.print("[yellow]Not yet implemented — coming in M4.[/]")
    raise typer.Exit(code=0)


if __name__ == "__main__":
    app()
