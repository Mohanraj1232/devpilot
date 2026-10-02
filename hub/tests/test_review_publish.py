"""Tests for publishing a review outcome and the markdown/inline output."""

from __future__ import annotations

from typing import Any

from ai_hub.errors import FailureReason
from ai_hub.github.client import GitHubError
from ai_hub.models import CheckResult, CheckStatus, GateResult, Severity
from ai_hub.report.inline import build_inline_comments, fingerprints_in
from ai_hub.report.markdown import COMMENT_MARKER, generate_summary_comment, sanitize_text
from ai_hub.report.publisher import publish
from ai_hub.review.outcome import ReviewOutcome
from tests.review_helpers import check, make_finding


def outcome(
    *,
    gate: GateResult = GateResult.PASS,
    findings: list | None = None,
    notes: list[str] | None = None,
    checks: list[CheckResult] | None = None,
) -> ReviewOutcome:
    return ReviewOutcome(
        gate_result=gate,
        gate_reasons=["All checks passed"]
        if gate == GateResult.PASS
        else ["1 critical finding(s)"],
        checks=checks or [check("static", "success")],
        findings=findings or [],
        off_diff_findings=[],
        risk_score=12.0,
        quality_score=88.0,
        coverage_pct=91.0,
        config_valid=True,
        config_version="abc123",
        notes=notes or [],
    )


class FakeGitHub:
    """Records what would be posted and stores comments like GitHub does."""

    def __init__(self) -> None:
        self.issue_comments: list[dict[str, Any]] = []
        self.review_comments: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []
        self.check_runs: list[dict[str, Any]] = []
        self.error: dict[str, GitHubError] = {}
        self._next_id = 100

    def _maybe_fail(self, op: str) -> None:
        if op in self.error:
            raise self.error[op]

    def list_issue_comments(self, number: int) -> list[dict[str, Any]]:
        self._maybe_fail("list_issue_comments")
        return list(self.issue_comments)

    def create_issue_comment(self, number: int, body: str) -> dict[str, Any]:
        self._maybe_fail("create_issue_comment")
        self._next_id += 1
        comment = {"id": self._next_id, "body": body, "user": {"login": "github-actions[bot]"}}
        self.issue_comments.append(comment)
        return comment

    def update_issue_comment(self, comment_id: int, body: str) -> dict[str, Any]:
        self._maybe_fail("update_issue_comment")
        for comment in self.issue_comments:
            if comment["id"] == comment_id:
                comment["body"] = body
                return comment
        raise AssertionError("updated a comment that does not exist")

    def list_review_comments(self, number: int) -> list[dict[str, Any]]:
        return list(self.review_comments)

    def create_review(
        self, number: int, *, commit_id: str, body: str, comments: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self._maybe_fail("create_review")
        for comment in comments:
            if comment["path"] == "unanchorable.py":
                raise GitHubError(FailureReason.INTERNAL_ERROR, "422", status_code=422)
        self.reviews.append({"commit_id": commit_id, "comments": comments})
        self.review_comments.extend({"body": c["body"]} for c in comments)
        return {}

    def create_check_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._maybe_fail("create_check_run")
        self.check_runs.append(payload)
        return {}


class TestStickyComment:
    def test_created_then_updated_in_place(self) -> None:
        gh = FakeGitHub()
        publish(gh, outcome(), pr_number=1, head_sha="abc")  # type: ignore[arg-type]
        publish(gh, outcome(gate=GateResult.FAIL), pr_number=1, head_sha="abc")  # type: ignore[arg-type]
        assert len(gh.issue_comments) == 1
        assert "FAIL" in gh.issue_comments[0]["body"]

    def test_someone_elses_comment_with_our_marker_is_never_edited(self) -> None:
        gh = FakeGitHub()
        gh.issue_comments.append(
            {"id": 1, "body": f"{COMMENT_MARKER} spoof", "user": {"login": "mallory"}}
        )
        publish(gh, outcome(), pr_number=1, head_sha="abc")  # type: ignore[arg-type]
        assert gh.issue_comments[0]["body"].endswith("spoof")  # untouched
        assert len(gh.issue_comments) == 2  # ours was created separately

    def test_custom_author_for_a_bot_token(self) -> None:
        gh = FakeGitHub()
        gh.issue_comments.append(
            {"id": 1, "body": f"{COMMENT_MARKER} old", "user": {"login": "devpilot-bot"}}
        )
        publish(gh, outcome(), pr_number=1, head_sha="abc", comment_authors=("devpilot-bot",))  # type: ignore[arg-type]
        assert len(gh.issue_comments) == 1
        assert "Quality Gate Report" in gh.issue_comments[0]["body"]


class TestInlineReview:
    def test_new_findings_are_posted_once_across_reruns(self) -> None:
        gh = FakeGitHub()
        out = outcome(findings=[make_finding("a.py", 5, Severity.HIGH)])
        first = publish(gh, out, pr_number=1, head_sha="abc")  # type: ignore[arg-type]
        second = publish(gh, out, pr_number=1, head_sha="abc")  # type: ignore[arg-type]
        assert first.inline_posted == 1
        assert second.inline_posted == 0
        assert len(gh.reviews) == 1
        assert gh.reviews[0]["commit_id"] == "abc"

    def test_below_the_minimum_severity_is_not_posted_inline(self) -> None:
        gh = FakeGitHub()
        out = outcome(findings=[make_finding("a.py", 5, Severity.LOW)])
        assert publish(gh, out, pr_number=1, head_sha="abc").inline_posted == 0  # type: ignore[arg-type]
        assert "low" in gh.issue_comments[0]["body"]  # still in the summary

    def test_one_unanchorable_comment_does_not_lose_the_others(self) -> None:
        gh = FakeGitHub()
        out = outcome(
            findings=[
                make_finding("unanchorable.py", 3, Severity.CRITICAL),
                make_finding("good.py", 7, Severity.HIGH),
            ]
        )
        result = publish(gh, out, pr_number=1, head_sha="abc")  # type: ignore[arg-type]
        assert result.inline_posted == 1
        assert gh.reviews[0]["comments"][0]["path"] == "good.py"


class TestCheckRun:
    def test_name_and_conclusion(self) -> None:
        gh = FakeGitHub()
        publish(gh, outcome(gate=GateResult.FAIL), pr_number=1, head_sha="abc123")  # type: ignore[arg-type]
        run = gh.check_runs[0]
        assert run["name"] == "AI Hub / Quality Gate"
        assert run["head_sha"] == "abc123"
        assert run["conclusion"] == "failure"
        publish(gh, outcome(), pr_number=1, head_sha="abc123")  # type: ignore[arg-type]
        assert gh.check_runs[1]["conclusion"] == "success"

    def test_details_url(self) -> None:
        gh = FakeGitHub()
        publish(gh, outcome(), pr_number=1, head_sha="a", details_url="https://x/runs/1")  # type: ignore[arg-type]
        assert gh.check_runs[0]["details_url"] == "https://x/runs/1"


class TestGracefulDegradation:
    def test_read_only_token_reports_problems_but_does_not_raise(self) -> None:
        denied = GitHubError(
            FailureReason.PERMISSION_DENIED, "Resource not accessible", status_code=403
        )
        gh = FakeGitHub()
        gh.error = {
            "list_issue_comments": denied,
            "create_review": denied,
            "create_check_run": denied,
        }
        result = publish(
            gh,
            outcome(findings=[make_finding("a.py", 1)]),
            pr_number=1,
            head_sha="abc",  # type: ignore[arg-type]
        )
        assert result.comment_posted is False
        assert result.check_run_created is False
        assert result.read_only is True
        assert len(result.problems) == 3

    def test_one_failure_does_not_block_the_remaining_steps(self) -> None:
        gh = FakeGitHub()
        gh.error = {"list_issue_comments": GitHubError(FailureReason.INTERNAL_ERROR, "boom")}
        result = publish(gh, outcome(), pr_number=1, head_sha="abc")  # type: ignore[arg-type]
        assert result.comment_posted is False
        assert result.check_run_created is True


class TestMarkdown:
    def test_untrusted_text_cannot_break_the_table_or_ping_people(self) -> None:
        nasty = make_finding(
            "a|b.py", 3, Severity.HIGH, title="x | y\nz @octocat `code`", tool="ruff"
        )
        text = generate_summary_comment(
            [nasty], [check("static", "failed")], GateResult.FAIL, ["r"]
        )
        row = next(line for line in text.splitlines() if "a\\|b.py" in line)
        assert row.count("|") - row.count("\\|") == 5  # 4 columns -> 5 unescaped pipes
        assert "@octocat" not in text
        assert "\nz" not in text

    def test_secrets_in_tool_output_are_redacted(self) -> None:
        token = "ghp_" + "z" * 36
        bad = check("tests", "failed", f"auth failed with {token}\nsecond line")
        text = generate_summary_comment([], [bad], GateResult.FAIL, ["tests failed"])
        assert token not in text

    def test_multiline_check_output_goes_into_a_details_block(self) -> None:
        bad = check("tests", "failed", "Tests failed:\n```\nFAILED test_a\n```")
        text = generate_summary_comment([], [bad], GateResult.FAIL, ["x"])
        assert "<details><summary>tests output</summary>" in text
        # Backtick fences in the output cannot close our own fence early.
        assert text.count("```") == 2

    def test_notes_and_off_diff_counts(self) -> None:
        text = generate_summary_comment(
            [],
            [check("static", "success")],
            GateResult.PASS,
            ["ok"],
            notes=["This PR changes the config"],
            risk_score=5.0,
            off_diff_count=4,
            coverage_pct=80.0,
        )
        assert "This PR changes the config" in text
        assert "4 additional finding(s)" in text
        assert "Coverage:** 80.0%" in text

    def test_table_is_capped(self) -> None:
        findings = [make_finding(f"f{i}.py", 1, Severity.LOW) for i in range(120)]
        text = generate_summary_comment(
            findings, [check("static", "failed")], GateResult.FAIL, ["x"]
        )
        assert "and 70 more" in text

    def test_comment_size_is_bounded(self) -> None:
        huge = check("tests", "failed", "x\n" * 100_000)
        text = generate_summary_comment([], [huge], GateResult.FAIL, ["x"])
        assert len(text) <= 60_100

    def test_marker_present_for_upserts(self) -> None:
        assert generate_summary_comment([], [], GateResult.PASS, []).startswith(COMMENT_MARKER)

    def test_sanitize_text_limits_length(self) -> None:
        assert len(sanitize_text("a" * 1000, limit=50)) == 50


class TestInlineComments:
    def test_fingerprint_marker_round_trips(self) -> None:
        finding = make_finding("a.py", 4, Severity.HIGH)
        comment = build_inline_comments([finding])[0]
        assert fingerprints_in([str(comment["body"])]) == {finding.fingerprint}

    def test_fingerprints_in_ignores_noise(self) -> None:
        assert fingerprints_in(["hello", "", "<!-- ai-hub:fp:ZZZ -->"]) == set()

    def test_text_is_sanitised(self) -> None:
        token = "ghp_" + "w" * 36
        finding = make_finding("a.py", 4, Severity.HIGH, title=f"ping @octocat {token}")
        body = str(build_inline_comments([finding])[0]["body"])
        assert "@octocat" not in body
        assert token not in body

    def test_findings_without_a_line_are_not_inline(self) -> None:
        assert build_inline_comments([make_finding("requirements.txt", 0, Severity.CRITICAL)]) == []

    def test_multi_line_comments_use_start_line(self) -> None:
        finding = make_finding("a.py", 4, Severity.HIGH, line_end=9)
        comment = build_inline_comments([finding])[0]
        assert (comment["start_line"], comment["line"], comment["side"]) == (4, 9, "RIGHT")

    def test_cap_and_ordering(self) -> None:
        findings = [make_finding("a.py", i + 1, Severity.MEDIUM) for i in range(40)]
        findings.append(make_finding("a.py", 99, Severity.CRITICAL))
        comments = build_inline_comments(findings, max_comments=5)
        assert len(comments) == 5
        assert comments[0]["line"] == 99  # most severe first


def test_checkstatus_used() -> None:
    assert CheckStatus.ERROR.value == "error"
