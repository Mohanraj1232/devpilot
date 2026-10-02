"""Agent tool definitions — sandboxed filesystem and test operations.

Every path the model supplies is untrusted. Paths are normalised, resolved
(following symlinks) and checked against the sandbox policy before any I/O.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import posixpath
import time
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TCH003 — used in a dataclass field annotation at runtime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("ai_hub.devpilot")

# Directories that are denied wherever they appear in a path (case-insensitive).
_DENIED_DIR_NAMES = {".git", "node_modules", "vendor"}
# Path prefixes (relative to the workspace root) that are denied.
_DENIED_PREFIXES = [".github/workflows"]
# Glob patterns matched against the file name (no "/") or the full relative path.
_DENIED_GLOBS = [
    "*.lock",
    "*.lockb",
    # Credentials and key material must never be read into prompts or written by the model.
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    ".npmrc",
    ".pypirc",
    ".netrc",
]

MAX_READ_CHARS = 60_000
MAX_READ_BYTES = 1_000_000
MAX_WRITE_BYTES = 200_000
MAX_SEARCH_RESULTS = 30
_MAX_SEARCH_FILES = 5_000
_MAX_SEARCH_FILE_BYTES = 500_000
_MAX_SEARCH_SECONDS = 20.0


@dataclass
class ToolPolicy:
    workspace: Path
    allow_workflow_changes: bool = False
    denied_globs: list[str] = field(default_factory=lambda: list(_DENIED_GLOBS))
    # Supplied by the orchestrator; runs the configured test command.
    test_runner: Callable[[], dict[str, Any]] | None = None


def _normalize_rel(rel_path: str) -> str | None:
    """Normalise a model-supplied relative path. Returns None if it escapes the root."""
    if not isinstance(rel_path, str) or "\x00" in rel_path:
        return None
    cleaned = rel_path.replace("\\", "/").strip()
    if cleaned.startswith("/") or (len(cleaned) > 1 and cleaned[1] == ":"):
        return None
    normalized = posixpath.normpath(cleaned or ".")
    if normalized == ".." or normalized.startswith("../"):
        return None
    return normalized


def _is_path_allowed(rel_path: str, policy: ToolPolicy) -> bool:
    """Check if a path (relative to the workspace root) is allowed by the sandbox policy."""
    normalized = _normalize_rel(rel_path)
    if normalized is None:
        return False
    if normalized == ".":
        return True

    lowered = normalized.lower()
    parts = lowered.split("/")

    if any(part in _DENIED_DIR_NAMES for part in parts):
        return False

    for prefix in _DENIED_PREFIXES:
        if lowered == prefix or lowered.startswith(prefix + "/"):
            if prefix == ".github/workflows" and policy.allow_workflow_changes:
                continue
            return False

    name = parts[-1]
    for glob in policy.denied_globs:
        pattern = glob.lower()
        if "/" in pattern:
            if fnmatch.fnmatch(lowered, pattern):
                return False
        elif fnmatch.fnmatch(name, pattern):
            return False
    return True


def _resolve_safe_path(rel_path: str, policy: ToolPolicy) -> Path | None:
    """Resolve a relative path within the workspace, preventing traversal and symlink escapes."""
    normalized = _normalize_rel(rel_path)
    if normalized is None or not _is_path_allowed(normalized, policy):
        return None
    try:
        root = policy.workspace.resolve()
        full = (root / normalized).resolve()
        if not full.is_relative_to(root):
            return None
        # A symlink inside the repo may point at a denied location inside the repo.
        resolved_rel = full.relative_to(root).as_posix()
        if not _is_path_allowed(resolved_rel, policy):
            return None
        return full
    except (ValueError, OSError):
        return None


def tool_list_dir(path: str, policy: ToolPolicy) -> dict[str, Any]:
    """List directory contents (denied entries are hidden)."""
    safe = _resolve_safe_path(path, policy)
    if safe is None:
        return {"error": f"Path denied: {path}"}

    if not safe.is_dir():
        return {"error": f"Not a directory: {path}"}

    base = _normalize_rel(path) or "."
    entries = []
    for entry in sorted(safe.iterdir()):
        rel = entry.name if base == "." else f"{base}/{entry.name}"
        if not _is_path_allowed(rel, policy):
            continue
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
        if safe.stat().st_size > MAX_READ_BYTES:
            return {"error": f"File too large to read: {path}"}
        content = safe.read_text(encoding="utf-8")
        if len(content) > MAX_READ_CHARS:
            content = content[:MAX_READ_CHARS] + "\n... (truncated)"
        return {"content": content}
    except (OSError, UnicodeDecodeError) as exc:
        return {"error": f"Cannot read: {exc}"}


def tool_write_file(path: str, content: str, policy: ToolPolicy) -> dict[str, Any]:
    """Write content to a file."""
    safe = _resolve_safe_path(path, policy)
    if safe is None:
        return {"error": f"Path denied: {path}"}

    if not isinstance(content, str):
        return {"error": "content must be a string"}
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        return {"error": f"Content too large (max {MAX_WRITE_BYTES} bytes)"}
    if safe.is_dir():
        return {"error": f"Path is a directory: {path}"}

    try:
        safe.parent.mkdir(parents=True, exist_ok=True)
        safe.write_text(content, encoding="utf-8")
        return {"success": True, "path": path}
    except OSError as exc:
        return {"error": f"Cannot write: {exc}"}


def tool_apply_edit(path: str, old_text: str, new_text: str, policy: ToolPolicy) -> dict[str, Any]:
    """Replace one occurrence of ``old_text`` with ``new_text`` in a file."""
    safe = _resolve_safe_path(path, policy)
    if safe is None:
        return {"error": f"Path denied: {path}"}
    if not safe.is_file():
        return {"error": f"File not found: {path}"}
    if not isinstance(old_text, str) or not old_text:
        return {"error": "old_text must be a non-empty string"}
    if not isinstance(new_text, str):
        return {"error": "new_text must be a string"}

    try:
        content = safe.read_text(encoding="utf-8")
        occurrences = content.count(old_text)
        if occurrences == 0:
            return {"error": "Old text not found in file"}
        if occurrences > 1:
            return {
                "error": f"Old text matches {occurrences} places; include more surrounding context"
            }
        new_content = content.replace(old_text, new_text, 1)
        if len(new_content.encode("utf-8")) > MAX_WRITE_BYTES:
            return {"error": f"Resulting file too large (max {MAX_WRITE_BYTES} bytes)"}
        safe.write_text(new_content, encoding="utf-8")
        return {"success": True, "path": path}
    except (OSError, UnicodeDecodeError) as exc:
        return {"error": f"Edit failed: {exc}"}


def tool_search_code(
    query: str, policy: ToolPolicy, *, max_results: int = MAX_SEARCH_RESULTS
) -> dict[str, Any]:
    """Case-insensitive literal search over allowed text files (pure Python, cross-platform)."""
    if not isinstance(query, str) or not query.strip():
        return {"error": "query must be a non-empty string"}

    needle = query.lower()
    root = policy.workspace.resolve()
    matches: list[dict[str, Any]] = []
    scanned = 0
    deadline = time.monotonic() + _MAX_SEARCH_SECONDS
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        dirnames[:] = sorted(
            d
            for d in dirnames
            if _is_path_allowed(d if rel_dir == "." else f"{rel_dir}/{d}", policy)
        )
        for filename in sorted(filenames):
            rel = filename if rel_dir == "." else f"{rel_dir}/{filename}"
            if not _is_path_allowed(rel, policy):
                continue
            scanned += 1
            if scanned > _MAX_SEARCH_FILES or time.monotonic() > deadline:
                truncated = True
                break
            full = Path(dirpath) / filename
            try:
                if full.is_symlink() or full.stat().st_size > _MAX_SEARCH_FILE_BYTES:
                    continue
                data = full.read_bytes()
                if b"\x00" in data[:1024]:
                    continue
                text = data.decode("utf-8", errors="ignore")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if needle in line.lower():
                    matches.append({"file": rel, "line": lineno, "text": line.strip()[:200]})
                    if len(matches) >= max_results:
                        return {"matches": matches, "truncated": True}
        if truncated:
            break

    return {"matches": matches, "truncated": truncated}


def tool_run_tests(policy: ToolPolicy) -> dict[str, Any]:
    """Run the configured test command via the orchestrator-supplied runner."""
    if policy.test_runner is None:
        return {"error": "No test command is configured; automated verification is unavailable"}
    return policy.test_runner()


def _schema(properties: dict[str, str], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            name: {"type": "string", "description": d} for name, d in properties.items()
        },
        "required": required,
    }


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_dir",
        "description": "List files and directories at the given path",
        "input_schema": _schema({"path": "Relative directory path, e.g. '.' or 'src'"}, ["path"]),
    },
    {
        "name": "read_file",
        "description": "Read the contents of a text file",
        "input_schema": _schema({"path": "Relative file path"}, ["path"]),
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file with the given content",
        "input_schema": _schema(
            {"path": "Relative file path", "content": "Full file content"}, ["path", "content"]
        ),
    },
    {
        "name": "apply_edit",
        "description": (
            "Replace exactly one occurrence of old_text with new_text in an existing file. "
            "old_text must match exactly and be unique in the file."
        ),
        "input_schema": _schema(
            {
                "path": "Relative file path",
                "old_text": "Exact text to find (must be unique)",
                "new_text": "Replacement text",
            },
            ["path", "old_text", "new_text"],
        ),
    },
    {
        "name": "search_code",
        "description": "Case-insensitive text search across the repository; returns file:line hits",
        "input_schema": _schema({"query": "Text to search for"}, ["query"]),
    },
    {
        "name": "run_tests",
        "description": "Run the project's configured test command and return the result",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "finish",
        "description": "Call when the implementation is complete",
        "input_schema": _schema({"summary": "Short summary of the changes made"}, ["summary"]),
    },
]


def bedrock_tool_specs() -> list[dict[str, Any]]:
    """Return TOOL_DEFINITIONS in the Bedrock Converse ``toolConfig.tools`` format."""
    return [
        {
            "toolSpec": {
                "name": tool["name"],
                "description": tool["description"],
                "inputSchema": {"json": tool["input_schema"]},
            }
        }
        for tool in TOOL_DEFINITIONS
    ]
