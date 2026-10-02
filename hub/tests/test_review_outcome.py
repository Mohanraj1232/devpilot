"""Gate verdicts computed from real prepare output plus stage result files.

The gate must fail closed: anything missing, unreadable or untrustworthy is a FAIL.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from ai_hub.models import CheckStatus, GateResult, Severity
from ai_hub.review.inputs import prepare_review_inputs
from ai_hub.review.outcome import ReviewOutcome, compute_outcome
from ai_hub.review.results import (
    read_checks,
    read_coverage,
    read_findings,
    write_result,
)
from tests.review_helpers import check, make_finding, make_pr_repo

if TYPE_CHECKING:
    from pathlib import Path

# Lenient so each test turns on exactly one rule.
CONFIG = (
    "quality_gate:\n"
    "  coverage_threshold: 0\n"
    "  fail_on:\n    critical: 1\n    high: 1\n"
    "  require_tests_pass: true\n"
    "  required_checks: [static, security, ai_review]\n"
)
BASE = {"src/app.py": "a = 1\n", ".ai-review/config.yml": CONFIG}
PR = {"src/app.py": "a = 1\nb = 2\nc = 3\n"}  # lines 2-3 are added


@pytest.fixture
def inputs(tmp_path: Path) -> Path:
    repo = make_pr_repo(tmp_path, base=BASE, pr=PR)
    out = tmp_path / "inputs"
    prepare_review_inputs(repo, out)
    return out


def all_pass(results: Path) -> None:
    for name in ("static", "security", "tests", "ai_review"):
        write_result(results, check(name, "success"), [], coverage_pct=95.0)


@pytest.fixture
def results(tmp_path: Path) -> Path:
    path = tmp_path / "results"
    all_pass(path)
    return path


class TestVerdicts:
    def test_everything_passing(self, inputs: Path, results: Path) -> None:
        outcome = compute_outcome(inputs, results)
        assert outcome.gate_result == GateResult.PASS
        assert outcome.config_valid is True
        assert outcome.coverage_pct == 95.0
        assert outcome.risk_score is not None

    def test_critical_finding_on_a_changed_line_fails(self, inputs: Path, results: Path) -> None:
        finding = make_finding("src/app.py", 2, Severity.CRITICAL)
        write_result(results, check("security", "failed"), [finding])
        outcome = compute_outcome(inputs, results)
        assert outcome.gate_result == GateResult.FAIL
        assert any("critical" in r for r in outcome.gate_reasons)
        assert outcome.findings == [finding]

    def test_same_finding_on_an_untouched_line_does_not_block_the_pr(
        self, inputs: Path, results: Path
    ) -> None:
        old = make_finding("src/app.py", 1, Severity.CRITICAL)  # line 1 pre-exists
        write_result(results, check("security", "failed"), [old])
        outcome = compute_outcome(inputs, results)
        assert outcome.gate_result == GateResult.PASS
        assert outcome.findings == []
        assert outcome.off_diff_findings == [old]

    def test_high_finding_fails(self, inputs: Path, results: Path) -> None:
        write_result(results, check("static", "failed"), [make_finding("src/app.py", 3)])
        assert compute_outcome(inputs, results).gate_result == GateResult.FAIL

    def test_medium_findings_do_not_fail_the_default_thresholds(
        self, inputs: Path, results: Path
    ) -> None:
        write_result(
            results, check("static", "failed"), [make_finding("src/app.py", 3, Severity.MEDIUM)]
        )
        assert compute_outcome(inputs, results).gate_result == GateResult.PASS

    def test_failing_tests_fail_the_gate(self, inputs: Path, results: Path) -> None:
        write_result(results, check("tests", "failed", "2 failed"), [])
        outcome = compute_outcome(inputs, results)
        assert outcome.gate_result == GateResult.FAIL
        assert any("Tests" in r for r in outcome.gate_reasons)

    def test_tool_error_in_a_required_check_fails(self, inputs: Path, results: Path) -> None:
        write_result(results, check("security", "error", "gitleaks crashed"), [])
        outcome = compute_outcome(inputs, results)
        assert outcome.gate_result == GateResult.FAIL
        assert any("security" in r for r in outcome.gate_reasons)
        assert outcome.quality_score is None  # unavailable, not a made-up number

    def test_ai_review_failure_fails_the_gate(self, inputs: Path, results: Path) -> None:
        write_result(results, check("ai_review", "error", "Bedrock unavailable"), [])
        assert compute_outcome(inputs, results).gate_result == GateResult.FAIL

    def test_skipped_checks_do_not_fail(self, inputs: Path, results: Path) -> None:
        write_result(results, check("static", "skipped", "No Python files changed"), [])
        assert compute_outcome(inputs, results).gate_result == GateResult.PASS

    def test_coverage_below_threshold_fails(self, tmp_path: Path, results: Path) -> None:
        config = CONFIG.replace("coverage_threshold: 0", "coverage_threshold: 99")
        repo = make_pr_repo(tmp_path, base={**BASE, ".ai-review/config.yml": config}, pr=PR)
        prepare_review_inputs(repo, tmp_path / "in2")
        outcome = compute_outcome(tmp_path / "in2", results)  # results report 95%
        assert outcome.gate_result == GateResult.FAIL
        assert any("Coverage" in r for r in outcome.gate_reasons)

    def test_unknown_coverage_fails_when_a_threshold_is_set(
        self, tmp_path: Path, results: Path
    ) -> None:
        config = CONFIG.replace("coverage_threshold: 0", "coverage_threshold: 80")
        repo = make_pr_repo(tmp_path, base={**BASE, ".ai-review/config.yml": config}, pr=PR)
        prepare_review_inputs(repo, tmp_path / "in2")
        write_result(results, check("tests", "success"), [], coverage_pct=None)
        outcome = compute_outcome(tmp_path / "in2", results)
        assert outcome.gate_result == GateResult.FAIL
        assert any("unavailable" in r for r in outcome.gate_reasons)

    def test_gate_can_be_disabled_by_the_base_config(self, tmp_path: Path, results: Path) -> None:
        config = CONFIG + "  enabled: false\n"
        repo = make_pr_repo(tmp_path, base={**BASE, ".ai-review/config.yml": config}, pr=PR)
        prepare_review_inputs(repo, tmp_path / "in2")
        write_result(
            results, check("tests", "failed"), [make_finding("src/app.py", 3, Severity.CRITICAL)]
        )
        outcome = compute_outcome(tmp_path / "in2", results)
        assert outcome.gate_result == GateResult.PASS
        assert "disabled" in outcome.gate_reasons[0]


class TestFailClosed:
    def test_missing_required_result_fails(self, inputs: Path, tmp_path: Path) -> None:
        partial = tmp_path / "partial"
        write_result(partial, check("security", "success"), [])
        write_result(partial, check("ai_review", "success"), [])
        write_result(partial, check("tests", "success"), [], coverage_pct=95.0)
        # no static result at all: the job crashed before writing one
        outcome = compute_outcome(inputs, partial)
        assert outcome.gate_result == GateResult.FAIL
        assert any("static" in r and "missing" in r for r in outcome.gate_reasons)

    def test_missing_tests_result_fails_when_tests_are_required(
        self, inputs: Path, tmp_path: Path
    ) -> None:
        partial = tmp_path / "partial"
        for name in ("static", "security", "ai_review"):
            write_result(partial, check(name, "success"), [])
        outcome = compute_outcome(inputs, partial)
        assert outcome.gate_result == GateResult.FAIL
        assert any("tests" in r and "missing" in r for r in outcome.gate_reasons)

    def test_no_results_at_all_fails(self, inputs: Path, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        assert compute_outcome(inputs, empty).gate_result == GateResult.FAIL

    def test_missing_prepare_output_fails(self, tmp_path: Path, results: Path) -> None:
        nothing = tmp_path / "nothing"
        nothing.mkdir()
        outcome = compute_outcome(nothing, results)
        assert outcome.gate_result == GateResult.FAIL
        assert "prepare" in outcome.gate_reasons[0]

    def test_invalid_config_fails_even_with_perfect_results(
        self, tmp_path: Path, results: Path
    ) -> None:
        repo = make_pr_repo(
            tmp_path, base={".ai-review/config.yml": "ai_review:\n  max_files: 0\n"}, pr=PR
        )
        prepare_review_inputs(repo, tmp_path / "in2")
        outcome = compute_outcome(tmp_path / "in2", results)
        assert outcome.gate_result == GateResult.FAIL
        assert outcome.config_valid is False
        assert "max_files" in outcome.gate_reasons[0]

    def test_missing_diff_fails(self, inputs: Path, results: Path) -> None:
        (inputs / "diff.patch").unlink()
        outcome = compute_outcome(inputs, results)
        assert outcome.gate_result == GateResult.FAIL

    def test_unreadable_check_file_becomes_an_error(self, inputs: Path, results: Path) -> None:
        (results / "check-security.json").write_text("{corrupt")
        outcome = compute_outcome(inputs, results)
        assert outcome.gate_result == GateResult.FAIL
        security = next(c for c in outcome.checks if c.name == "security")
        assert security.status == CheckStatus.ERROR

    def test_unreadable_findings_file_becomes_an_error(self, inputs: Path, results: Path) -> None:
        (results / "findings-static.json").write_text('{"not": "a list"}')
        outcome = compute_outcome(inputs, results)
        assert outcome.gate_result == GateResult.FAIL
        assert next(c for c in outcome.checks if c.name == "static").status == CheckStatus.ERROR


class TestPullRequestsCannotWeakenTheirOwnGate:
    def test_pr_that_disables_the_gate_is_still_gated(self, tmp_path: Path, results: Path) -> None:
        repo = make_pr_repo(
            tmp_path,
            base=BASE,
            pr={".ai-review/config.yml": "quality_gate:\n  enabled: false\n", **PR},
        )
        prepare_review_inputs(repo, tmp_path / "in2")
        write_result(
            results, check("security", "failed"), [make_finding("src/app.py", 2, Severity.CRITICAL)]
        )
        outcome = compute_outcome(tmp_path / "in2", results)
        assert outcome.gate_result == GateResult.FAIL
        assert any("changes `.ai-review/config.yml`" in n for n in outcome.notes)

    def test_pr_that_ignores_its_own_files_is_still_gated(self, tmp_path: Path) -> None:
        # The ignore list also comes from the base branch, so the PR cannot hide findings.
        repo = make_pr_repo(
            tmp_path,
            base=BASE,
            pr={".ai-review/config.yml": 'paths:\n  ignore: ["src/**"]\n', **PR},
        )
        prepare_review_inputs(repo, tmp_path / "in2")
        config = (tmp_path / "in2" / "config.yml").read_text()
        assert "ignore" not in config


class TestForkPullRequests:
    def test_fork_pr_skips_ai_review_instead_of_failing(self, inputs: Path, tmp_path: Path) -> None:
        partial = tmp_path / "fork"
        for name in ("static", "security"):
            write_result(partial, check(name, "success"), [])
        write_result(partial, check("tests", "success"), [], coverage_pct=95.0)
        outcome = compute_outcome(inputs, partial, fork_pr=True)
        assert outcome.gate_result == GateResult.PASS
        ai = next(c for c in outcome.checks if c.name == "ai_review")
        assert ai.status == CheckStatus.SKIPPED
        assert any("fork" in n for n in outcome.notes)

    def test_non_fork_pr_with_missing_ai_review_fails(self, inputs: Path, tmp_path: Path) -> None:
        partial = tmp_path / "nofork"
        for name in ("static", "security"):
            write_result(partial, check(name, "success"), [])
        write_result(partial, check("tests", "success"), [], coverage_pct=95.0)
        assert compute_outcome(inputs, partial, fork_pr=False).gate_result == GateResult.FAIL

    def test_fork_flag_does_not_excuse_a_real_ai_review_error(
        self, inputs: Path, results: Path
    ) -> None:
        write_result(results, check("ai_review", "error", "boom"), [])
        assert compute_outcome(inputs, results, fork_pr=True).gate_result == GateResult.FAIL


class TestResultFiles:
    def test_round_trip(self, tmp_path: Path) -> None:
        finding = make_finding("a.py", 3, Severity.LOW)
        write_result(tmp_path, check("static", "failed", "1 finding(s)"), [finding])
        assert read_checks(tmp_path)[0].name == "static"
        found, unreadable = read_findings(tmp_path)
        assert found == [finding] and unreadable == []
        assert found[0].fingerprint == finding.fingerprint

    def test_files_are_found_in_nested_download_directories(self, tmp_path: Path) -> None:
        # download-artifact without merge-multiple puts each artifact in its own folder.
        write_result(tmp_path / "result-static", check("static", "success"), [])
        write_result(tmp_path / "result-tests", check("tests", "success"), [], coverage_pct=71.5)
        assert {c.name for c in read_checks(tmp_path)} == {"static", "tests"}
        assert read_coverage(tmp_path) == 71.5

    def test_unsafe_check_names_are_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            write_result(tmp_path, check("../evil", "success"), [])

    def test_coverage_garbage_is_unavailable(self, tmp_path: Path) -> None:
        (tmp_path / "coverage.json").write_text('{"line_rate_pct": "lots"}')
        assert read_coverage(tmp_path) is None

    def test_outcome_serialisation_round_trip(self, inputs: Path, results: Path) -> None:
        write_result(results, check("static", "failed"), [make_finding("src/app.py", 2)])
        outcome = compute_outcome(inputs, results)
        again = ReviewOutcome.from_dict(json.loads(json.dumps(outcome.to_dict())))
        assert again.gate_result == outcome.gate_result
        assert [f.fingerprint for f in again.findings] == [f.fingerprint for f in outcome.findings]
        assert again.risk_score == outcome.risk_score
