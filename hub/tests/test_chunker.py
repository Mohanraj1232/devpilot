"""Tests for ai_hub.review.chunker — diff chunking for AI review."""

from pathlib import Path

from ai_hub.analysis.diff import parse_unified_diff
from ai_hub.review.chunker import chunk_diffs, estimate_tokens

FIXTURES = Path(__file__).parent / "fixtures"


class TestEstimateTokens:
    def test_empty(self) -> None:
        assert estimate_tokens("") == 0

    def test_basic(self) -> None:
        assert estimate_tokens("hello world!") == 3

    def test_long_text(self) -> None:
        text = "x" * 4000
        assert estimate_tokens(text) == 1000


class TestChunkDiffs:
    def test_basic_chunking(self) -> None:
        diff_text = (FIXTURES / "sample.diff").read_text()
        file_diffs = parse_unified_diff(diff_text)
        chunks = chunk_diffs(file_diffs)
        assert len(chunks) >= 1
        assert "app.py" in chunks[0] or "utils.py" in chunks[0]

    def test_binary_files_skipped(self) -> None:
        diff_text = (FIXTURES / "sample.diff").read_text()
        file_diffs = parse_unified_diff(diff_text)
        chunks = chunk_diffs(file_diffs)
        full_text = " ".join(chunks)
        assert "logo.png" not in full_text

    def test_max_files_limit(self) -> None:
        diff_text = (FIXTURES / "sample.diff").read_text()
        file_diffs = parse_unified_diff(diff_text)
        chunks = chunk_diffs(file_diffs, max_files=1)
        full_text = " ".join(chunks)
        file_count = sum(1 for fd in file_diffs if not fd.is_binary and fd.path in full_text)
        assert file_count <= 1

    def test_ignore_patterns(self) -> None:
        diff_text = (FIXTURES / "sample.diff").read_text()
        file_diffs = parse_unified_diff(diff_text)
        chunks = chunk_diffs(file_diffs, ignore_patterns=["app.py"])
        full_text = " ".join(chunks)
        assert "### app.py" not in full_text

    def test_empty_diffs(self) -> None:
        assert chunk_diffs([]) == []

    def test_small_budget_forces_split(self) -> None:
        diff_text = (FIXTURES / "sample.diff").read_text()
        file_diffs = parse_unified_diff(diff_text)
        chunks = chunk_diffs(file_diffs, token_budget=50)
        assert len(chunks) >= 2

    def test_sensitive_paths_prioritized(self) -> None:
        diff_text = (
            "diff --git a/auth/handler.py b/auth/handler.py\n"
            "--- a/auth/handler.py\n"
            "+++ b/auth/handler.py\n"
            "@@ -1,3 +1,4 @@\n"
            " import os\n"
            "+import secrets\n"
            " def login():\n"
            "     pass\n"
            "diff --git a/readme.txt b/readme.txt\n"
            "--- a/readme.txt\n"
            "+++ b/readme.txt\n"
            "@@ -1,1 +1,2 @@\n"
            " Hello\n"
            "+World\n"
        )
        file_diffs = parse_unified_diff(diff_text)
        chunks = chunk_diffs(file_diffs)
        assert "auth/handler.py" in chunks[0]
