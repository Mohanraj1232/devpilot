"""Reading and writing the per-stage result files exchanged between workflow jobs.

Each analysis job writes ``check-<name>.json`` and ``findings-<name>.json``; the final
job reads them all. Anything unreadable becomes an ERROR check, never a silent omission.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ai_hub.models import CheckResult, CheckStatus, Finding

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("ai_hub.review")

_MAX_FILE_BYTES = 20_000_000
_SAFE_NAME = re.compile(r"^[a-z0-9_]+$")


def write_result(
    out_dir: Path, check: CheckResult, findings: list[Finding], *, coverage_pct: float | None = None
) -> None:
    """Persist one stage's check, findings and (for tests) the measured coverage."""
    if not _SAFE_NAME.match(check.name):
        raise ValueError(f"Unsafe check name: {check.name!r}")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"check-{check.name}.json").write_text(check.model_dump_json(indent=2), "utf-8")
    (out_dir / f"findings-{check.name}.json").write_text(
        json.dumps([f.model_dump(mode="json") for f in findings], indent=2), "utf-8"
    )
    if check.name == "tests":
        (out_dir / "coverage.json").write_text(json.dumps({"line_rate_pct": coverage_pct}), "utf-8")


def _load_json(path: Path) -> Any:
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError("file too large")
    return json.loads(path.read_text("utf-8"))


def read_checks(results_dir: Path) -> list[CheckResult]:
    """Load every check-*.json under ``results_dir``; an unreadable file is an ERROR check."""
    checks: dict[str, CheckResult] = {}
    for path in sorted(results_dir.rglob("check-*.json")):
        name = path.stem.removeprefix("check-")
        try:
            check = CheckResult.model_validate(_load_json(path))
        except (OSError, ValueError, ValidationError) as exc:
            logger.error("Unreadable result file %s: %s", path.name, exc)
            check = CheckResult(
                name=name,
                status=CheckStatus.ERROR,
                summary=f"Result for '{name}' could not be read",
            )
        checks[check.name] = check
    return list(checks.values())


def read_findings(results_dir: Path) -> tuple[list[Finding], list[str]]:
    """Load every findings-*.json. Returns (findings, names of unreadable files)."""
    findings: list[Finding] = []
    unreadable: list[str] = []
    for path in sorted(results_dir.rglob("findings-*.json")):
        try:
            raw = _load_json(path)
            if not isinstance(raw, list):
                raise ValueError("expected a list")
            findings.extend(Finding.model_validate(item) for item in raw)
        except (OSError, ValueError, ValidationError) as exc:
            logger.error("Unreadable findings file %s: %s", path.name, exc)
            unreadable.append(path.stem.removeprefix("findings-"))
    return findings, unreadable


def read_coverage(results_dir: Path) -> float | None:
    path = next(iter(sorted(results_dir.rglob("coverage.json"))), None)
    if path is None:
        return None
    try:
        value = _load_json(path).get("line_rate_pct")
        return float(value) if value is not None else None
    except (OSError, ValueError, AttributeError, TypeError):
        return None
