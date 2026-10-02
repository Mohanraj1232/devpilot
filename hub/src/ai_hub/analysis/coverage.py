"""Coverage report parsers — Cobertura XML and LCOV formats."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class FileCoverage:
    """Coverage data for a single source file."""

    file: str
    lines_valid: int = 0
    lines_covered: int = 0
    branch_rate: float | None = None

    @property
    def line_rate(self) -> float:
        if self.lines_valid == 0:
            return 0.0
        return self.lines_covered / self.lines_valid


@dataclass
class CoverageReport:
    """Aggregated coverage report."""

    total_lines: int = 0
    covered_lines: int = 0
    line_rate: float | None = None
    branch_rate: float | None = None
    files: list[FileCoverage] = field(default_factory=list)

    @property
    def line_rate_pct(self) -> float | None:
        if self.line_rate is not None:
            return round(self.line_rate * 100, 2)
        if self.total_lines == 0:
            return None
        return round((self.covered_lines / self.total_lines) * 100, 2)


def parse_cobertura_xml(path: Path) -> CoverageReport | None:
    """Parse a Cobertura XML coverage report."""
    if not path.is_file():
        return None

    try:
        tree = ET.parse(path)  # noqa: S314
    except ET.ParseError:
        return None

    root = tree.getroot()
    report = CoverageReport()

    line_rate_str = root.get("line-rate")
    if line_rate_str:
        report.line_rate = float(line_rate_str)

    branch_rate_str = root.get("branch-rate")
    if branch_rate_str:
        report.branch_rate = float(branch_rate_str)

    for package in root.iter("package"):
        for cls in package.iter("class"):
            filename = cls.get("filename", "")
            lines = cls.findall(".//line")
            valid = len(lines)
            covered = sum(1 for ln in lines if int(ln.get("hits", "0")) > 0)

            fc = FileCoverage(
                file=filename,
                lines_valid=valid,
                lines_covered=covered,
            )
            report.files.append(fc)
            report.total_lines += valid
            report.covered_lines += covered

    return report


_LCOV_SF = re.compile(r"^SF:(.+)$")
_LCOV_DA = re.compile(r"^DA:(\d+),(\d+)")
_LCOV_END = re.compile(r"^end_of_record$")


def parse_lcov(path: Path) -> CoverageReport | None:
    """Parse an LCOV coverage report."""
    if not path.is_file():
        return None

    try:
        content = path.read_text()
    except OSError:
        return None

    report = CoverageReport()
    current_file: str | None = None
    file_valid = 0
    file_covered = 0

    for line in content.splitlines():
        sf_match = _LCOV_SF.match(line)
        if sf_match:
            current_file = sf_match.group(1)
            file_valid = 0
            file_covered = 0
            continue

        da_match = _LCOV_DA.match(line)
        if da_match:
            file_valid += 1
            if int(da_match.group(2)) > 0:
                file_covered += 1
            continue

        if _LCOV_END.match(line) and current_file is not None:
            fc = FileCoverage(
                file=current_file,
                lines_valid=file_valid,
                lines_covered=file_covered,
            )
            report.files.append(fc)
            report.total_lines += file_valid
            report.covered_lines += file_covered
            current_file = None

    return report
