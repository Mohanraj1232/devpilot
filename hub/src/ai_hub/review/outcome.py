"""Combine stage results into scores and the quality-gate verdict.

The gate fails closed: a missing required result, an unreadable result file, an invalid
configuration or a failed prepare stage all produce FAIL, never PASS.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ai_hub.analysis.diff import build_changed_line_map, parse_unified_diff
from ai_hub.config.loader import load_config
from ai_hub.errors import ConfigError
from ai_hub.gate.evaluator import evaluate_gate
from ai_hub.models import CheckResult, CheckStatus, Finding, GateResult
from ai_hub.report.dedup import deduplicate_findings
from ai_hub.review.pipeline import partition_findings
from ai_hub.review.results import read_checks, read_coverage, read_findings
from ai_hub.scoring.engine import compute_quality_score, compute_risk_score

if TYPE_CHECKING:
    from pathlib import Path

    from ai_hub.config.schema import HubConfig

EXPECTED_CHECKS = ("static", "security", "tests", "ai_review")


@dataclass
class ReviewOutcome:
    gate_result: GateResult
    gate_reasons: list[str]
    checks: list[CheckResult]
    findings: list[Finding]  # on the diff: these drive scores and the gate
    off_diff_findings: list[Finding]
    risk_score: float | None
    quality_score: float | None
    coverage_pct: float | None
    config_valid: bool
    config_version: str
    inline_min_severity: str = "medium"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_result": self.gate_result.value,
            "gate_reasons": self.gate_reasons,
            "checks": [c.model_dump(mode="json") for c in self.checks],
            "findings": [f.model_dump(mode="json") for f in self.findings],
            "off_diff_findings": [f.model_dump(mode="json") for f in self.off_diff_findings],
            "risk_score": self.risk_score,
            "quality_score": self.quality_score,
            "coverage_pct": self.coverage_pct,
            "config_valid": self.config_valid,
            "config_version": self.config_version,
            "inline_min_severity": self.inline_min_severity,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewOutcome:
        return cls(
            gate_result=GateResult(data["gate_result"]),
            gate_reasons=list(data["gate_reasons"]),
            checks=[CheckResult.model_validate(c) for c in data["checks"]],
            findings=[Finding.model_validate(f) for f in data["findings"]],
            off_diff_findings=[Finding.model_validate(f) for f in data["off_diff_findings"]],
            risk_score=data.get("risk_score"),
            quality_score=data.get("quality_score"),
            coverage_pct=data.get("coverage_pct"),
            config_valid=bool(data["config_valid"]),
            config_version=str(data.get("config_version", "")),
            inline_min_severity=str(data.get("inline_min_severity", "medium")),
            notes=list(data.get("notes", [])),
        )


def _read_status(inputs_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((inputs_dir / "config-status.json").read_text("utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _fail(reasons: list[str], checks: list[CheckResult], notes: list[str]) -> ReviewOutcome:
    return ReviewOutcome(
        gate_result=GateResult.FAIL,
        gate_reasons=reasons,
        checks=checks,
        findings=[],
        off_diff_findings=[],
        risk_score=None,
        quality_score=None,
        coverage_pct=None,
        config_valid=False,
        config_version="",
        notes=notes,
    )


def compute_outcome(inputs_dir: Path, results_dir: Path, *, fork_pr: bool = False) -> ReviewOutcome:
    """Merge stage results into a ReviewOutcome."""
    checks = read_checks(results_dir)
    notes: list[str] = []

    status = _read_status(inputs_dir)
    if status is None:
        return _fail(
            ["The prepare stage did not produce its result; the review cannot be trusted"],
            checks,
            notes,
        )
    if status.get("pr_modifies_config"):
        notes.append(
            "This PR changes `.ai-review/config.yml`. The gate used the configuration from the "
            "base branch; changes take effect once merged."
        )
    if not status.get("valid"):
        errors = status.get("errors") or ["unknown error"]
        return _fail(
            ["Configuration is invalid: " + "; ".join(str(e) for e in errors)], checks, notes
        )

    config_file = inputs_dir / "config.yml"
    try:
        config, version = load_config(config_file if config_file.is_file() else None)
    except ConfigError as exc:
        return _fail([f"Configuration is invalid: {exc.message}"], checks, notes)

    present = {c.name for c in checks}
    if fork_pr and "ai_review" not in present:
        checks.append(
            CheckResult(
                name="ai_review",
                status=CheckStatus.SKIPPED,
                summary="Skipped: pull requests from forks cannot access AI credentials",
            )
        )
        notes.append("AI review was skipped because this pull request comes from a fork.")

    findings, unreadable = read_findings(results_dir)
    for name in unreadable:
        if name not in {c.name for c in checks if c.status == CheckStatus.ERROR}:
            checks = [c for c in checks if c.name != name] + [
                CheckResult(
                    name=name,
                    status=CheckStatus.ERROR,
                    summary=f"Findings for '{name}' could not be read",
                )
            ]

    diff_path = inputs_dir / "diff.patch"
    try:
        file_diffs = parse_unified_diff(diff_path.read_text("utf-8"))
    except OSError:
        return _fail(["The PR diff is missing; the review cannot be trusted"], checks, notes)
    added = build_changed_line_map(file_diffs)
    changed_lines = sum(fd.changed_line_count for fd in file_diffs)

    deduped = deduplicate_findings(findings)
    on_diff, off_diff = partition_findings(deduped, added)

    coverage = read_coverage(results_dir)
    gate_cfg = config.quality_gate
    risk = compute_risk_score(
        on_diff,
        changed_lines,
        coverage_pct=coverage,
        coverage_threshold=gate_cfg.coverage_threshold,
        file_paths=list(added),
    )
    quality = compute_quality_score(
        on_diff,
        checks,
        coverage_pct=coverage,
        coverage_threshold=gate_cfg.coverage_threshold,
    )

    required = list(gate_cfg.required_checks)
    if gate_cfg.require_tests_pass and "tests" not in required:
        # Without this a crashed/missing tests job would let the gate pass.
        required.append("tests")

    if gate_cfg.enabled:
        result, reasons = evaluate_gate(
            on_diff,
            checks,
            coverage_pct=coverage,
            coverage_threshold=gate_cfg.coverage_threshold,
            fail_on_critical=gate_cfg.fail_on.critical,
            fail_on_high=gate_cfg.fail_on.high,
            require_tests_pass=gate_cfg.require_tests_pass,
            required_checks=required,
            config_valid=True,
        )
    else:
        result, reasons = GateResult.PASS, ["Quality gate is disabled in the configuration"]

    return ReviewOutcome(
        gate_result=result,
        gate_reasons=reasons,
        checks=sorted(
            checks, key=lambda c: EXPECTED_CHECKS.index(c.name) if c.name in EXPECTED_CHECKS else 99
        ),
        findings=on_diff,
        off_diff_findings=off_diff,
        risk_score=risk,
        quality_score=quality,
        coverage_pct=coverage,
        config_valid=True,
        config_version=version,
        inline_min_severity=config.ai_review.inline_min_severity,
        notes=notes,
    )


def load_config_for_stage(inputs_dir: Path, override: Path | None = None) -> HubConfig:
    """The trusted config for an analysis stage (from the prepare output, never the PR)."""
    path = override or (inputs_dir / "config.yml")
    config, _ = load_config(path if path.is_file() else None)
    return config
