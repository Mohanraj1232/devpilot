"""Context enrichment — adds surrounding code context to diff chunks for better AI review."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from ai_hub.analysis.diff import FileDiff


def add_file_context(
    file_diff: FileDiff,
    repo_path: Path,
    context_lines: int = 10,
) -> str:
    """Read the full file and add surrounding context to help the AI reviewer."""
    file_path = repo_path / file_diff.path
    if not file_path.is_file():
        return ""

    try:
        source_lines = file_path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return ""

    parts: list[str] = [f"### Full context for {file_diff.path}"]

    for hunk in file_diff.hunks:
        start = max(0, hunk.new_start - context_lines - 1)
        end = min(len(source_lines), hunk.new_start + hunk.new_count + context_lines)

        parts.append(f"\n--- Lines {start + 1}-{end} ---")
        for i in range(start, end):
            line_num = i + 1
            marker = " "
            if line_num in file_diff.added_lines:
                marker = "+"
            parts.append(f"{line_num:4d}{marker} {source_lines[i]}")

    return "\n".join(parts)
