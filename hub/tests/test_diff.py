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


class TestLineCountingIsRobust:
    """Line numbers must follow git's own counting (newline-terminated lines only)."""

    HEADER = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,5 @@\n"

    def _added(self, body: str) -> set[int]:
        from ai_hub.analysis.diff import parse_unified_diff

        return parse_unified_diff(self.HEADER + body)[0].added_lines

    def test_plain(self) -> None:
        assert self._added(" a\n+b\n+c\n d\n+e\n") == {2, 3, 5}

    def test_crlf_line_endings(self) -> None:
        assert self._added(" a\r\n+b\r\n+c\r\n d\r\n+e\r\n") == {2, 3, 5}

    def test_double_carriage_return_from_windows_pipes(self) -> None:
        assert self._added(" a\r\r\n+b\r\r\n+c\r\r\n d\r\r\n+e\r\r\n") == {2, 3, 5}

    def test_form_feed_inside_a_line_does_not_split_it(self) -> None:
        # GNU-style Python files contain form feeds; str.splitlines() would split on them.
        assert self._added(" a\n+b\x0cstill line two\n+c\n d\n+e\n") == {2, 3, 5}

    def test_unicode_line_separators_do_not_split_lines(self) -> None:
        assert self._added(" a\n+x = ' '\n+y = ' '\n d\n+e\n") == {2, 3, 5}

    def test_lone_carriage_return_inside_a_line(self) -> None:
        assert self._added(" a\n+old mac\rline\n+c\n d\n+e\n") == {2, 3, 5}

    def test_file_without_a_trailing_newline(self) -> None:
        assert self._added(" a\n+b\n+c\n d\n+e") == {2, 3, 5}

    def test_trailing_newline_does_not_add_a_phantom_context_line(self) -> None:
        from ai_hub.analysis.diff import parse_unified_diff

        hunk = parse_unified_diff(self.HEADER + " a\n+b\n")[0].hunks[0]
        assert len(hunk.lines) == 2

    def test_content_does_not_keep_the_carriage_return(self) -> None:
        from ai_hub.analysis.diff import parse_unified_diff

        hunk = parse_unified_diff(self.HEADER + "+b = 1\r\n")[0].hunks[0]
        assert hunk.lines[0].content == "b = 1"

    def test_diff_file_round_trip_is_byte_exact(self, tmp_path) -> None:
        from ai_hub.analysis.diff import load_diff_text, read_diff_file

        raw = (self.HEADER + " a\r\r\n+b\r\r\n").encode()
        path = tmp_path / "diff.patch"
        path.write_bytes(raw)
        assert load_diff_text(path).encode() == raw  # no newline translation
        assert read_diff_file(path)[0].added_lines == {2}
