"""Tests for ai_hub.analysis.coverage — Cobertura XML and LCOV parsers."""

from pathlib import Path

from ai_hub.analysis.coverage import parse_cobertura_xml, parse_lcov

FIXTURES = Path(__file__).parent / "fixtures"


class TestCoberturaParser:
    def test_parse_valid_xml(self) -> None:
        report = parse_cobertura_xml(FIXTURES / "coverage.xml")
        assert report is not None
        assert report.line_rate == 0.85
        assert report.branch_rate == 0.72
        assert len(report.files) == 2

    def test_file_coverage_details(self) -> None:
        report = parse_cobertura_xml(FIXTURES / "coverage.xml")
        assert report is not None
        main_cov = next(f for f in report.files if "main.py" in f.file)
        assert main_cov.lines_valid == 10
        assert main_cov.lines_covered == 9
        assert main_cov.line_rate == 0.9

    def test_utils_coverage(self) -> None:
        report = parse_cobertura_xml(FIXTURES / "coverage.xml")
        assert report is not None
        utils_cov = next(f for f in report.files if "utils.py" in f.file)
        assert utils_cov.lines_valid == 4
        assert utils_cov.lines_covered == 3
        assert utils_cov.line_rate == 0.75

    def test_total_lines(self) -> None:
        report = parse_cobertura_xml(FIXTURES / "coverage.xml")
        assert report is not None
        assert report.total_lines == 14
        assert report.covered_lines == 12

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert parse_cobertura_xml(tmp_path / "nope.xml") is None

    def test_invalid_xml_returns_none(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.xml"
        bad.write_text("not xml at all{{{")
        assert parse_cobertura_xml(bad) is None

    def test_line_rate_pct(self) -> None:
        report = parse_cobertura_xml(FIXTURES / "coverage.xml")
        assert report is not None
        assert report.line_rate_pct == 85.0


class TestLCOVParser:
    def test_parse_valid_lcov(self) -> None:
        report = parse_lcov(FIXTURES / "coverage.lcov")
        assert report is not None
        assert len(report.files) == 2

    def test_file_coverage(self) -> None:
        report = parse_lcov(FIXTURES / "coverage.lcov")
        assert report is not None
        main_cov = next(f for f in report.files if "main.py" in f.file)
        assert main_cov.lines_valid == 5
        assert main_cov.lines_covered == 4
        assert main_cov.line_rate == 0.8

    def test_utils_coverage(self) -> None:
        report = parse_lcov(FIXTURES / "coverage.lcov")
        assert report is not None
        utils_cov = next(f for f in report.files if "utils.py" in f.file)
        assert utils_cov.lines_valid == 4
        assert utils_cov.lines_covered == 2
        assert utils_cov.line_rate == 0.5

    def test_totals(self) -> None:
        report = parse_lcov(FIXTURES / "coverage.lcov")
        assert report is not None
        assert report.total_lines == 9
        assert report.covered_lines == 6

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert parse_lcov(tmp_path / "nope.lcov") is None

    def test_line_rate_pct_computed(self) -> None:
        report = parse_lcov(FIXTURES / "coverage.lcov")
        assert report is not None
        pct = report.line_rate_pct
        assert pct is not None
        assert 66.0 < pct < 67.0
