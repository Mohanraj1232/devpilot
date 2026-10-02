"""Agent tool definitions — sandboxed filesystem and test operations."""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("ai_hub.devpilot")

_DENIED_PATHS = [".git", ".github/workflows"]
_DENIED_GLOBS = ["*.lock", "*.lockb", "vendor/**", "node_modules/**"]


@dataclass
class ToolPolicy:
    workspace: Path
    allow_workflow_changes: bool = False
    denied_globs: list[str] = field(default_factory=lambda: list(_DENIED_GLOBS))


def _is_path_allowed(rel_path: str, policy: ToolPolicy) -> bool:
    """Check if a path is allowed by the sandbox policy."""
    parts = rel_path.replace("\\", "/").split("/")
    for denied in _DENIED_PATHS:
        denied_parts = denied.split("/")
        if parts[: len(denied_parts)] == denied_parts:
            if denied == ".github/workflows" and policy.allow_workflow_changes:
                continue
            return False

    return all(not fnmatch.fnmatch(rel_path, glob) for glob in policy.denied_globs)


def _resolve_safe_path(rel_path: str, policy: ToolPolicy) -> Path | None:
    """Resolve a relative path within the workspace, preventing traversal."""
    try:
        full = (policy.workspace / rel_path).resolve()
        if not str(full).startswith(str(policy.workspace.resolve())):
            return None
        if not _is_path_allowed(rel_path, policy):
            return None
        return full
    except (ValueError, OSError):
        return None


def tool_list_dir(path: str, policy: ToolPolicy) -> dict[str, Any]:
    """List directory contents."""
    safe = _resolve_safe_path(path, policy)
    if safe is None:
        return {"error": f"Path denied: {path}"}

    if not safe.is_dir():
        return {"error": f"Not a directory: {path}"}

    entries = []
    for entry in sorted(safe.iterdir()):
        entries.append(
            {
                "name": entry.name,
                "type": "directory" if entry.is_dir() else "file",
            }
        )
    return {"entries": entries}


def tool_read_file(path: str, policy: ToolPolicy) -> dict[str, Any]:
    """Read a file's contents."""
    safe = _resolve_safe_path(path, policy)
    if safe is None:
        return {"error": f"Path denied: {path}"}

    if not safe.is_file():
        return {"error": f"Not a file: {path}"}

    try:
        content = safe.read_text()
        if len(content) > 100_000:
            content = content[:100_000] + "\n... (truncated)"
        return {"content": content}
    except (OSError, UnicodeDecodeError) as exc:
        return {"error": f"Cannot read: {exc}"}


def tool_write_file(path: str, content: str, policy: ToolPolicy) -> dict[str, Any]:
    """Write content to a file."""
    safe = _resolve_safe_path(path, policy)
    if safe is None:
        return {"error": f"Path denied: {path}"}

    try:
        safe.parent.mkdir(parents=True, exist_ok=True)
        safe.write_text(content)
        return {"success": True, "path": path}
    except OSError as exc:
        return {"error": f"Cannot write: {exc}"}


def tool_search_code(query: str, policy: ToolPolicy, *, max_results: int = 20) -> dict[str, Any]:
    """Search for a pattern in the workspace files."""
    import subprocess

    try:
        result = subprocess.run(
            [
                "grep",
                "-rnl",
                "--include=*.py",
                "--include=*.js",
                "--include=*.ts",
                "--include=*.java",
                "--include=*.go",
                query,
                ".",
            ],
            cwd=policy.workspace,
            capture_output=True,
            text=True,
            timeout=30,
        )
        files = result.stdout.strip().split("\n")[:max_results]
        return {"matches": [f for f in files if f]}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {"matches": []}


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_dir",
        "description": "List files and directories at the given path",
        "parameters": {"path": {"type": "string", "description": "Relative path to list"}},
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file",
        "parameters": {"path": {"type": "string", "description": "Relative path to read"}},
    },
    {
        "name": "write_file",
        "description": "Write content to a file (create or overwrite)",
        "parameters": {
            "path": {"type": "string", "description": "Relative path to write"},
            "content": {"type": "string", "description": "File content to write"},
        },
    },
    {
        "name": "apply_edit",
        "description": "Apply a targeted edit to a file by replacing old text with new text",
        "parameters": {
            "path": {"type": "string", "description": "Relative path to edit"},
            "old_text": {"type": "string", "description": "Text to find and replace"},
            "new_text": {"type": "string", "description": "Replacement text"},
        },
    },
    {
        "name": "search_code",
        "description": "Search for a pattern across workspace files",
        "parameters": {"query": {"type": "string", "description": "Search pattern"}},
    },
    {
        "name": "run_tests",
        "description": "Run the project test suite",
        "parameters": {},
    },
    {
        "name": "finish",
        "description": "Signal that implementation is complete",
        "parameters": {
            "summary": {"type": "string", "description": "Summary of changes made"},
        },
    },
]
