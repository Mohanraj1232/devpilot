"""Workspace management — project detection, submodules and dependency install."""

from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ai_hub.safety.env import scrubbed_env
from ai_hub.safety.redact import redact

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("ai_hub.devpilot")

_INSTALL_TIMEOUT = 600


@dataclass
class ProjectType:
    name: str
    language: str
    test_command: str | None = None
    install_command: str | None = None
    # Tried if install_command fails (e.g. no [dev] extra defined).
    install_fallback: str | None = None
    # Always run after the main install (e.g. make sure the test runner exists).
    tooling_install: str | None = None
    markers: list[str] | None = None


KNOWN_PROJECTS: list[ProjectType] = [
    ProjectType(
        name="python-pytest",
        language="python",
        test_command="python -m pytest",
        install_command="python -m pip install -e .[dev]",
        install_fallback="python -m pip install -e .",
        tooling_install="python -m pip install pytest",
        markers=["pyproject.toml", "setup.py", "setup.cfg"],
    ),
    ProjectType(
        name="python-requirements",
        language="python",
        test_command="python -m pytest",
        install_command="python -m pip install -r requirements.txt",
        tooling_install="python -m pip install pytest",
        markers=["requirements.txt"],
    ),
    ProjectType(
        name="node-npm",
        language="javascript",
        test_command="npm test",
        install_command="npm install",
        markers=["package.json"],
    ),
    ProjectType(
        name="go",
        language="go",
        test_command="go test ./...",
        install_command=None,
        markers=["go.mod"],
    ),
    ProjectType(
        name="java-maven",
        language="java",
        test_command="mvn test",
        install_command="mvn install -DskipTests",
        markers=["pom.xml"],
    ),
]


def detect_project_type(repo_path: Path) -> ProjectType | None:
    """Detect the project type from marker files in the repository."""
    for project in KNOWN_PROJECTS:
        if project.markers:
            for marker in project.markers:
                if (repo_path / marker).is_file():
                    return project
    return None


def is_empty_repo(repo_path: Path) -> bool:
    """Check if a repo is empty (no files beyond .git)."""
    entries = [e for e in repo_path.iterdir() if e.name != ".git"]
    return len(entries) == 0


def init_submodules(repo_path: Path) -> bool:
    """Initialize git submodules. Returns False on failure."""
    gitmodules = repo_path / ".gitmodules"
    if not gitmodules.is_file():
        return True

    try:
        result = subprocess.run(
            ["git", "submodule", "update", "--init", "--recursive"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return result.returncode == 0


@dataclass
class InstallResult:
    ok: bool
    output: str = ""
    commands_run: list[str] = field(default_factory=list)


def _run_install(command: str, repo_path: Path) -> tuple[bool, str]:
    try:
        argv = shlex.split(command)
        result = subprocess.run(
            argv,
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_INSTALL_TIMEOUT,
            env=scrubbed_env(),
        )
    except subprocess.TimeoutExpired:
        return False, f"Timed out after {_INSTALL_TIMEOUT}s: {command}"
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        return False, f"Cannot run '{command}': {exc}"
    output = redact(((result.stdout or "")[-1500:] + "\n" + (result.stderr or "")[-1500:]).strip())
    return result.returncode == 0, output


def install_dependencies(repo_path: Path, project: ProjectType) -> InstallResult:
    """Install project dependencies in a scrubbed environment."""
    outcome = InstallResult(ok=True)
    install = project.install_command
    if project.name == "node-npm" and (repo_path / "package-lock.json").is_file():
        install = "npm ci"

    if install:
        outcome.commands_run.append(install)
        ok, output = _run_install(install, repo_path)
        if not ok and project.install_fallback:
            outcome.commands_run.append(project.install_fallback)
            ok, output = _run_install(project.install_fallback, repo_path)
        if not ok:
            return InstallResult(False, output, outcome.commands_run)
        outcome.output = output

    if project.tooling_install:
        outcome.commands_run.append(project.tooling_install)
        ok, output = _run_install(project.tooling_install, repo_path)
        if not ok:
            return InstallResult(False, output, outcome.commands_run)

    return outcome
