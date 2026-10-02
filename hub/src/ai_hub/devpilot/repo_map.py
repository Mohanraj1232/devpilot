"""Compact, size-limited repository map given to the model for planning.

Large repositories are never dumped wholesale: only a shallow tree, a few key
files, and the files most relevant to the issue text are included.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_SKIP_DIRS = {
    ".git",
    "node_modules",
    "vendor",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    "target",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
}
_TEXT_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".rb", ".rs", ".c", ".h", ".cpp",
    ".cs", ".php", ".md", ".txt", ".yml", ".yaml", ".toml", ".json", ".html", ".css", ".sql",
}  # fmt: skip
_KEY_FILES = [
    "README.md",
    "README.rst",
    "pyproject.toml",
    "package.json",
    "go.mod",
    "pom.xml",
    "requirements.txt",
]
_STOPWORDS = {
    "that", "this", "with", "from", "have", "should", "when", "then", "into", "will", "would",
    "there", "their", "which", "while", "about", "after", "before", "also", "add", "make",
    "need", "want", "issue", "please", "using", "used", "does", "doesn", "didn", "cannot",
}  # fmt: skip
_MAX_DEPTH = 4
_MAX_TREE_ENTRIES = 300
_MAX_SCAN_FILES = 2000
_MAX_SCAN_BYTES = 100_000


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", text.lower())
    seen: dict[str, None] = {}
    for word in words:
        if word not in _STOPWORDS:
            seen.setdefault(word)
    return list(seen)[:25]


def _head(path: Path, limit: int) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


def rank_relevant_files(workspace: Path, issue_text: str, *, limit: int = 15) -> list[str]:
    """Rank files by how often the issue's keywords appear in their path and content."""
    tokens = _tokens(issue_text)
    if not tokens:
        return []
    scores: dict[str, int] = {}
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            full = Path(dirpath) / name
            if full.suffix.lower() not in _TEXT_EXTS:
                continue
            scanned += 1
            if scanned > _MAX_SCAN_FILES:
                break
            rel = full.relative_to(workspace).as_posix()
            score = sum(3 for t in tokens if t in rel.lower())
            try:
                if full.stat().st_size <= _MAX_SCAN_BYTES:
                    content = full.read_text(encoding="utf-8", errors="ignore").lower()
                    score += sum(min(content.count(t), 5) for t in tokens)
            except OSError:
                continue
            if score:
                scores[rel] = score
    return [p for p, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def build_repo_map(workspace: Path, issue_text: str, *, max_chars: int = 14_000) -> str:
    """Build the repository map: tree, key files and issue-relevant files."""
    tree: list[str] = []
    root_depth = len(workspace.parts)
    truncated = False
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        depth = len(Path(dirpath).parts) - root_depth
        if depth >= _MAX_DEPTH:
            dirnames[:] = []
        rel_dir = Path(dirpath).relative_to(workspace).as_posix()
        for name in sorted(filenames):
            tree.append(name if rel_dir == "." else f"{rel_dir}/{name}")
            if len(tree) >= _MAX_TREE_ENTRIES:
                truncated = True
                break
        if truncated:
            break

    sections = ["## File tree" + (" (truncated)" if truncated else ""), "\n".join(tree)]

    relevant = rank_relevant_files(workspace, issue_text)
    if relevant:
        sections += ["## Files most relevant to the issue", "\n".join(relevant)]

    for key in _KEY_FILES:
        key_path = workspace / key
        if key_path.is_file():
            sections += [f"## {key} (head)", _head(key_path, 1500)]

    return "\n\n".join(sections)[:max_chars]
