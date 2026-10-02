"""Tests for ai_hub.analysis.diff — unified diff parsing."""

from pathlib import Path

from ai_hub.analysis.diff import build_changed_line_map, parse_unified_diff

FIXTURES = Path(__file__).parent / "fixtures"


class TestParseUnifiedDiff:
    def test_parse_sample_diff(self) -> None:
        diff_text = (FIXTURES / "sample.diff").read_text()
        files = parse_unified_diff(diff_text)
        assert len(files) == 3

    def test_new_file_detected(self) -> None:
        diff_text = (FIXTURES / "sample.diff").read_text()
        files = parse_unified_diff(diff_text)
        app_diff = next(f for f in files if f.path == "app.py")
        assert app_diff.is_new is True
        assert len(app_diff.hunks) == 1
        assert len(app_diff.added_lines) == 15

    def test_modified_file(self) -> None:
        diff_text = (FIXTURES / "sample.diff").read_text()
        files = parse_unified_diff(diff_text)
        utils_diff = next(f for f in files if f.path == "utils.py")
        assert utils_diff.is_new is False
        assert len(utils_diff.hunks) == 1
        assert 2 in utils_diff.added_lines
        assert len(utils_diff.deleted_lines) > 0

    def test_binary_file(self) -> None:
        diff_text = (FIXTURES / "sample.diff").read_text()
        files = parse_unified_diff(diff_text)
        binary_diff = next(f for f in files if f.path == "logo.png")
        assert binary_diff.is_binary is True
        assert len(binary_diff.hunks) == 0

    def test_hunk_line_numbers(self) -> None:
        diff_text = (FIXTURES / "sample.diff").read_text()
        files = parse_unified_diff(diff_text)
        utils_diff = next(f for f in files if f.path == "utils.py")
        hunk = utils_diff.hunks[0]
        assert hunk.old_start == 1
        assert hunk.new_start == 1
        added = hunk.added_lines
        assert all(isinstance(n, int) for n in added)

    def test_empty_diff(self) -> None:
        assert parse_unified_diff("") == []

    def test_changed_line_count(self) -> None:
        diff_text = (FIXTURES / "sample.diff").read_text()
        files = parse_unified_diff(diff_text)
        app_diff = next(f for f in files if f.path == "app.py")
        assert app_diff.changed_line_count == 15

    def test_rename_detection(self) -> None:
        diff = "diff --git a/old.py b/new.py\nrename from old.py\nrename to new.py\n"
        files = parse_unified_diff(diff)
        assert len(files) == 1
        assert files[0].is_rename is True


class TestBuildChangedLineMap:
    def test_map_from_sample(self) -> None:
        diff_text = (FIXTURES / "sample.diff").read_text()
        files = parse_unified_diff(diff_text)
        line_map = build_changed_line_map(files)
        assert "app.py" in line_map
        assert "utils.py" in line_map
        assert len(line_map["app.py"]) == 15

    def test_empty_input(self) -> None:
        assert build_changed_line_map([]) == {}
