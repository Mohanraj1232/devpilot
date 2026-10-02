"""The "prepare" stage: capture the PR diff and the *trusted* configuration.

The workflow checks out the PR's merge commit, whose first parent is the base branch tip.
The gate configuration is read from that parent, not from the PR, so a pull request
cannot weaken its own quality gate by editing ``.ai-review/config.yml``.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from ai_hub.analysis.diff import parse_unified_diff
from ai_hub.config.loader import load_config
from ai_hub.errors import ConfigError, FailureReason, GitError

if TYPE_CHECKING:
    from pathlib import Path

BASE_REF = "HEAD^1"
HEAD_REF = "HEAD"
_GIT = ["git", "-c", f"core.hooksPath={os.devnull}"]


@dataclass
class PrepareResult:
    config_valid: bool
    config_version: str = ""
    config_source: str = "defaults"
    config_errors: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    changed_lines: int = 0
    pr_modifies_config: bool = False


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            [*_GIT, *args], cwd=workspace, capture_output=True, timeout=120, check=False
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise GitError(FailureReason.TIMEOUT, f"git {args[0]} failed: {exc}") from exc


def _safe_config_path(config_path: str) -> str:
    normalized = PurePosixPath(config_path.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts or not normalized.parts:
        raise ConfigError(f"Config path must be relative to the repository: {config_path}")
    return normalized.as_posix()


def prepare_review_inputs(
    workspace: Path, out_dir: Path, *, config_path: str = ".ai-review/config.yml"
) -> PrepareResult:
    """Write diff.patch, changed-files.json, config.yml (from base) and config-status.json."""
    out_dir.mkdir(parents=True, exist_ok=True)
    config_rel = _safe_config_path(config_path)

    diff = _git(
        workspace, "diff", "--no-color", "--no-ext-diff", "--no-textconv", BASE_REF, HEAD_REF
    )
    if diff.returncode != 0:
        raise GitError(
            FailureReason.CLONE_FAILURE,
            "Cannot compute the PR diff; check out the pull request merge commit with "
            "fetch-depth: 2 or more",
            details={"stderr": diff.stderr.decode("utf-8", "replace")[:300]},
        )
    diff_text = diff.stdout.decode("utf-8", errors="replace")
    (out_dir / "diff.patch").write_text(diff_text, encoding="utf-8")

    file_diffs = parse_unified_diff(diff_text)
    changed = [fd.path for fd in file_diffs if fd.path and not fd.is_deleted]
    (out_dir / "changed-files.json").write_text(json.dumps(changed), encoding="utf-8")
    changed_lines = sum(fd.changed_line_count for fd in file_diffs)
    modifies_config = any(config_rel in (fd.old_path, fd.new_path) for fd in file_diffs)

    # Trusted config: the version on the base branch.
    shown = _git(workspace, "show", f"{BASE_REF}:{config_rel}")
    config_file = out_dir / "config.yml"
    source = "defaults"
    if shown.returncode == 0:
        config_file.write_bytes(shown.stdout)
        source = "base"
    elif config_file.exists():
        config_file.unlink()

    result = PrepareResult(
        config_valid=True,
        config_source=source,
        changed_files=changed,
        changed_lines=changed_lines,
        pr_modifies_config=modifies_config,
    )
    try:
        _, result.config_version = load_config(config_file if source == "base" else None)
    except ConfigError as exc:
        result.config_valid = False
        result.config_errors = [
            f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg', '')}"
            for e in exc.details.get("errors", [])
        ] or [exc.message]

    (out_dir / "config-status.json").write_text(
        json.dumps(
            {
                "valid": result.config_valid,
                "version": result.config_version,
                "source": result.config_source,
                "errors": result.config_errors,
                "pr_modifies_config": result.pr_modifies_config,
                "changed_lines": result.changed_lines,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return result
