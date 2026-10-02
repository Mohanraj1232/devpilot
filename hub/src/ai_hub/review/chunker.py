"""Diff chunker — splits PR diffs into token-budget-friendly chunks for AI review."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ai_hub.analysis.diff import FileDiff

_CHARS_PER_TOKEN = 4
_SENSITIVE_PATHS = {"auth", "crypto", "security", "migration", "secret", "key", "password"}


def estimate_tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN


def _risk_score(fd: FileDiff) -> int:
    """Higher score = should be reviewed first."""
    score = fd.changed_line_count
    path_lower = fd.path.lower()
    if any(s in path_lower for s in _SENSITIVE_PATHS):
        score += 500
    if fd.is_new:
        score += 100
    return score


def _render_file_diff(fd: FileDiff, context_lines: int = 3) -> str:
    """Render a FileDiff into a readable text block for the LLM."""
    parts: list[str] = []
    status = ""
    if fd.is_new:
        status = " (new file)"
    elif fd.is_deleted:
        status = " (deleted)"
    elif fd.is_rename:
        status = f" (renamed from {fd.old_path})"

    parts.append(f"### {fd.path}{status}")
    parts.append("")

    for hunk in fd.hunks:
        header = f"@@ -{hunk.old_start},{hunk.old_count} +{hunk.new_start},{hunk.new_count} @@"
        if hunk.header:
            header += f" {hunk.header}"
        parts.append(header)

        for line in hunk.lines:
            if line.kind == "add":
                parts.append(f"+{line.content}")
            elif line.kind == "delete":
                parts.append(f"-{line.content}")
            else:
                parts.append(f" {line.content}")
        parts.append("")

    return "\n".join(parts)


def chunk_diffs(
    file_diffs: list[FileDiff],
    *,
    token_budget: int = 30000,
    max_files: int = 50,
    ignore_patterns: list[str] | None = None,
    generated_patterns: list[str] | None = None,
) -> list[str]:
    """Split file diffs into chunks that fit within the token budget.

    Files are sorted by risk (sensitive paths first, then by change size).
    Generated/vendor files are skipped. Each chunk is a rendered text block
    ready for the LLM prompt.
    """
    import fnmatch

    ignore = ignore_patterns or []
    generated = generated_patterns or []

    filtered: list[FileDiff] = []
    for fd in file_diffs:
        if fd.is_binary:
            continue
        path = fd.path
        if any(fnmatch.fnmatch(path, p) for p in ignore + generated):
            continue
        filtered.append(fd)

    sorted_diffs = sorted(filtered, key=_risk_score, reverse=True)[:max_files]

    chunks: list[str] = []
    current_parts: list[str] = []
    current_tokens = 0

    for fd in sorted_diffs:
        rendered = _render_file_diff(fd)
        tokens = estimate_tokens(rendered)

        if current_tokens + tokens > token_budget and current_parts:
            chunks.append("\n".join(current_parts))
            current_parts = []
            current_tokens = 0

        if tokens > token_budget:
            chunks.append(rendered[: token_budget * _CHARS_PER_TOKEN])
        else:
            current_parts.append(rendered)
            current_tokens += tokens

    if current_parts:
        chunks.append("\n".join(current_parts))

    return chunks
