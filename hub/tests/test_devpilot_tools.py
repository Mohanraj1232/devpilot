"""Security and behaviour tests for the DevPilot sandboxed tools."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from ai_hub.devpilot.tools import (
    MAX_WRITE_BYTES,
    ToolPolicy,
    _is_path_allowed,
    _resolve_safe_path,
    bedrock_tool_specs,
    tool_apply_edit,
    tool_list_dir,
    tool_read_file,
    tool_run_tests,
    tool_search_code,
    tool_write_file,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def policy(tmp_path: Path) -> ToolPolicy:
    return ToolPolicy(workspace=tmp_path)


class TestPathSandbox:
    @pytest.mark.parametrize(
        "path",
        [
            "./.git/config",
            "a/../.git/config",
            ".git",
            ".GIT/config",
            "src/vendor/lib.py",
            "node_modules/x/index.js",
            "pkg/node_modules/x.js",
            ".env",
            "config/.env.production",
            "keys/server.pem",
            "deploy/id_rsa",
            ".npmrc",
            "./.github/workflows/ci.yml",
            ".GitHub/Workflows/ci.yml",
            "../outside.txt",
            "a/../../outside.txt",
            "/etc/passwd",
            "C:/Windows/system.ini",
            "bad\x00name",
        ],
    )
    def test_denied(self, path: str, policy: ToolPolicy) -> None:
        assert _is_path_allowed(path, policy) is False

    @pytest.mark.parametrize("path", ["src/main.py", "README.md", "tests/test_a.py", "a/./b.py"])
    def test_allowed(self, path: str, policy: ToolPolicy) -> None:
        assert _is_path_allowed(path, policy) is True

    def test_sibling_directory_with_shared_prefix_is_not_inside(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (tmp_path / "ws-evil").mkdir()
        (tmp_path / "ws-evil" / "secret.txt").write_text("nope")
        policy = ToolPolicy(workspace=workspace)
        assert _resolve_safe_path("../ws-evil/secret.txt", policy) is None
        assert tool_read_file("../ws-evil/secret.txt", policy).get("error")

    def test_symlink_escape_is_blocked(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("top secret")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        try:
            os.symlink(outside, workspace / "link", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not available on this platform")
        policy = ToolPolicy(workspace=workspace)
        assert tool_read_file("link/secret.txt", policy).get("error")
        assert tool_write_file("link/new.txt", "x", policy).get("error")
        assert not (outside / "new.txt").exists()

    def test_symlink_to_denied_dir_inside_repo_is_blocked(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("[core]")
        try:
            os.symlink(tmp_path / ".git", tmp_path / "gitlink", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not available on this platform")
        assert tool_read_file("gitlink/config", ToolPolicy(workspace=tmp_path)).get("error")


class TestFileTools:
    def test_list_dir_hides_denied_entries(self, tmp_path: Path, policy: ToolPolicy) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".env").write_text("A=1")
        (tmp_path / "app.py").write_text("x")
        names = [e["name"] for e in tool_list_dir(".", policy)["entries"]]
        assert names == ["app.py"]

    def test_read_rejects_binary(self, tmp_path: Path, policy: ToolPolicy) -> None:
        (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
        assert "error" in tool_read_file("blob.bin", policy)

    def test_write_size_limit(self, policy: ToolPolicy) -> None:
        assert "error" in tool_write_file("big.txt", "x" * (MAX_WRITE_BYTES + 1), policy)

    def test_write_over_directory_rejected(self, tmp_path: Path, policy: ToolPolicy) -> None:
        (tmp_path / "d").mkdir()
        assert "error" in tool_write_file("d", "x", policy)

    def test_apply_edit_requires_unique_match(self, tmp_path: Path, policy: ToolPolicy) -> None:
        f = tmp_path / "a.py"
        f.write_text("x = 1\nx = 1\n")
        result = tool_apply_edit("a.py", "x = 1", "x = 2", policy)
        assert "error" in result
        assert f.read_text() == "x = 1\nx = 1\n"

    def test_apply_edit_rejects_empty_old_text(self, tmp_path: Path, policy: ToolPolicy) -> None:
        (tmp_path / "a.py").write_text("x")
        assert "error" in tool_apply_edit("a.py", "", "y", policy)

    def test_apply_edit_unique(self, tmp_path: Path, policy: ToolPolicy) -> None:
        f = tmp_path / "a.py"
        f.write_text("x = 1\ny = 2\n")
        assert tool_apply_edit("a.py", "x = 1", "x = 9", policy)["success"] is True
        assert f.read_text() == "x = 9\ny = 2\n"


class TestSearch:
    def test_finds_matches_with_line_numbers(self, tmp_path: Path, policy: ToolPolicy) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("def Hello():\n    pass\n")
        result = tool_search_code("hello", policy)
        assert result["matches"] == [{"file": "src/a.py", "line": 1, "text": "def Hello():"}]

    def test_skips_denied_and_binary_files(self, tmp_path: Path, policy: ToolPolicy) -> None:
        (tmp_path / ".env").write_text("SECRET=needle")
        (tmp_path / "vendor").mkdir()
        (tmp_path / "vendor" / "x.py").write_text("needle")
        (tmp_path / "blob.dat").write_bytes(b"\x00needle")
        (tmp_path / "ok.py").write_text("needle")
        files = [m["file"] for m in tool_search_code("needle", policy)["matches"]]
        assert files == ["ok.py"]

    def test_result_cap(self, tmp_path: Path, policy: ToolPolicy) -> None:
        (tmp_path / "many.txt").write_text("\n".join("hit" for _ in range(100)))
        result = tool_search_code("hit", policy, max_results=5)
        assert len(result["matches"]) == 5
        assert result["truncated"] is True

    def test_empty_query_rejected(self, policy: ToolPolicy) -> None:
        assert "error" in tool_search_code("  ", policy)


class TestRunTestsTool:
    def test_unavailable_without_runner(self, policy: ToolPolicy) -> None:
        assert "error" in tool_run_tests(policy)

    def test_delegates_to_runner(self, tmp_path: Path) -> None:
        policy = ToolPolicy(workspace=tmp_path, test_runner=lambda: {"status": "success"})
        assert tool_run_tests(policy) == {"status": "success"}


def test_bedrock_tool_specs_shape() -> None:
    specs = bedrock_tool_specs()
    names = {s["toolSpec"]["name"] for s in specs}
    assert names == {
        "list_dir",
        "read_file",
        "write_file",
        "apply_edit",
        "search_code",
        "run_tests",
        "finish",
    }
    for spec in specs:
        schema = spec["toolSpec"]["inputSchema"]["json"]
        assert schema["type"] == "object"
        assert "properties" in schema
