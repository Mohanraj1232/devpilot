"""PR diff fetch, unified-diff parsing, hunk extraction, and changed-line maps."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class HunkLine:
    """A single line in a diff hunk."""

    old_lineno: int | None
    new_lineno: int | None
    content: str
    kind: str  # "add", "delete", "context"


@dataclass
class Hunk:
    """A contiguous block of changes within a file diff."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str = ""
    lines: list[HunkLine] = field(default_factory=list)

    @property
    def added_lines(self) -> list[int]:
        return [ln.new_lineno for ln in self.lines if ln.kind == "add" and ln.new_lineno]

    @property
    def deleted_lines(self) -> list[int]:
        return [ln.old_lineno for ln in self.lines if ln.kind == "delete" and ln.old_lineno]


@dataclass
class FileDiff:
    """Parsed diff for a single file."""

    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)
    is_binary: bool = False
    is_new: bool = False
    is_deleted: bool = False
    is_rename: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def added_lines(self) -> set[int]:
        result: set[int] = set()
        for hunk in self.hunks:
            result.update(hunk.added_lines)
        return result

    @property
    def deleted_lines(self) -> set[int]:
        result: set[int] = set()
        for hunk in self.hunks:
            result.update(hunk.deleted_lines)
        return result

    @property
    def changed_line_count(self) -> int:
        return len(self.added_lines) + len(self.deleted_lines)


_DIFF_HEADER = re.compile(r"^diff --git a/(.*) b/(.*)$")
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def parse_unified_diff(diff_text: str) -> list[FileDiff]:
    """Parse a unified diff string into structured FileDiff objects."""
    files: list[FileDiff] = []
    current_file: FileDiff | None = None
    current_hunk: Hunk | None = None

    # Split on "\n" only. str.splitlines() also splits on "\r", form feed, U+2028 and other
    # characters that can legitimately appear *inside* a source line, which shifts every line
    # number after them. A trailing "\r" (CRLF files) is not part of the line's content.
    lines = diff_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the newline that terminates the last line

    for raw_line in lines:
        raw_line = raw_line.rstrip("\r")
        header_match = _DIFF_HEADER.match(raw_line)
        if header_match:
            current_file = FileDiff(
                old_path=header_match.group(1),
                new_path=header_match.group(2),
            )
            files.append(current_file)
            current_hunk = None
            continue

        if current_file is None:
            continue

        if raw_line.startswith("Binary files"):
            current_file.is_binary = True
            continue

        if raw_line.startswith("new file"):
            current_file.is_new = True
            continue

        if raw_line.startswith("deleted file"):
            current_file.is_deleted = True
            continue

        if raw_line.startswith("rename from") or raw_line.startswith("rename to"):
            current_file.is_rename = True
            continue

        if raw_line.startswith("--- ") or raw_line.startswith("+++ "):
            continue

        hunk_match = _HUNK_HEADER.match(raw_line)
        if hunk_match:
            current_hunk = Hunk(
                old_start=int(hunk_match.group(1)),
                old_count=int(hunk_match.group(2) or "1"),
                new_start=int(hunk_match.group(3)),
                new_count=int(hunk_match.group(4) or "1"),
                header=hunk_match.group(5).strip(),
            )
            current_file.hunks.append(current_hunk)
            old_lineno = current_hunk.old_start
            new_lineno = current_hunk.new_start
            continue

        if current_hunk is None:
            continue

        if raw_line.startswith("+"):
            current_hunk.lines.append(
                HunkLine(
                    old_lineno=None,
                    new_lineno=new_lineno,
                    content=raw_line[1:],
                    kind="add",
                )
            )
            new_lineno += 1
        elif raw_line.startswith("-"):
            current_hunk.lines.append(
                HunkLine(
                    old_lineno=old_lineno,
                    new_lineno=None,
                    content=raw_line[1:],
                    kind="delete",
                )
            )
            old_lineno += 1
        elif raw_line.startswith(" ") or raw_line == "":
            content = raw_line[1:] if raw_line.startswith(" ") else ""
            current_hunk.lines.append(
                HunkLine(
                    old_lineno=old_lineno,
                    new_lineno=new_lineno,
                    content=content,
                    kind="context",
                )
            )
            old_lineno += 1
            new_lineno += 1

    return files


def build_changed_line_map(file_diffs: list[FileDiff]) -> dict[str, set[int]]:
    """Build a mapping of file path -> set of changed (added) line numbers."""
    return {fd.path: fd.added_lines for fd in file_diffs if fd.path}


def load_diff_text(path: Path) -> str:
    """Read a diff file as bytes: text mode would translate "\r" and corrupt line counting."""
    return path.read_bytes().decode("utf-8", errors="replace")


def read_diff_file(path: Path) -> list[FileDiff]:
    """Read and parse a diff from a file on disk."""
    return parse_unified_diff(load_diff_text(path))
