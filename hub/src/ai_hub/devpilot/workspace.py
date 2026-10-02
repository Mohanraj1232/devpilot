"""Workspace management — clone, project detection, dependency install."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("ai_hub.devpilot")


@dataclass
class ProjectType:
    name: str
    language: str
    test_command: str | None = None
    install_command: str | None = None
    markers: list[str] | None = None


KNOWN_PROJECTS: list[ProjectType] = [
    ProjectType(
        name="python-pytest",
        language="python",
        test_command="pytest",
        install_command="pip install -e '.[dev]'",
        markers=["pyproject.toml", "setup.py", "setup.cfg"],
    ),
    ProjectType(
        name="python-requirements",
        language="python",
        test_command="pytest",
        install_command="pip install -r requirements.txt",
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
        subprocess.run(
            ["git", "submodule", "update", "--init", "--recursive"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return True
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return False


def install_dependencies(repo_path: Path, project: ProjectType) -> bool:
    """Install project dependencies. Returns False on failure."""
    if not project.install_command:
        return True

    try:
        result = subprocess.run(
            project.install_command.split(),
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
