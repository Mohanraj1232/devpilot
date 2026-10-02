"""Publish a ReviewOutcome to GitHub (sticky comment, inline review, Check Run).

Publishing is best-effort and degrades gracefully: on a read-only token (pull requests
from forks) nothing can be posted, so the caller falls back to the job summary. The
gate verdict never depends on whether publishing succeeded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ai_hub.errors import FailureReason, HubError
from ai_hub.report.checkrun import build_check_run_payload
from ai_hub.report.inline import build_inline_comments, fingerprints_in
from ai_hub.report.markdown import COMMENT_MARKER, generate_summary_comment

if TYPE_CHECKING:
    from ai_hub.github.client import GitHubClient
    from ai_hub.review.outcome import ReviewOutcome

logger = logging.getLogger("ai_hub.report")

DEFAULT_COMMENT_AUTHORS = ("github-actions[bot]",)


@dataclass
class PublishResult:
    comment_posted: bool = False
    inline_posted: int = 0
    check_run_created: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def read_only(self) -> bool:
        return any("permission" in p.lower() for p in self.problems)


def render_summary(outcome: ReviewOutcome) -> str:
    return generate_summary_comment(
        outcome.findings,
        outcome.checks,
        outcome.gate_result,
        outcome.gate_reasons,
        risk_score=outcome.risk_score,
        quality_score=outcome.quality_score,
        notes=outcome.notes,
        off_diff_count=len(outcome.off_diff_findings),
        coverage_pct=outcome.coverage_pct,
    )


def _find_own_comment(
    comments: list[dict[str, Any]], authors: tuple[str, ...]
) -> dict[str, Any] | None:
    """Our sticky comment: has the marker AND was written by one of our identities."""
    for comment in comments:
        login = (comment.get("user") or {}).get("login")
        if COMMENT_MARKER in (comment.get("body") or "") and login in authors:
            return comment
    return None


def publish(
    gh: GitHubClient,
    outcome: ReviewOutcome,
    *,
    pr_number: int,
    head_sha: str,
    details_url: str | None = None,
    comment_authors: tuple[str, ...] = DEFAULT_COMMENT_AUTHORS,
) -> PublishResult:
    """Post everything; each step is independent so one failure does not block the others."""
    result = PublishResult()
    body = render_summary(outcome)

    def attempt(label: str, action: Any) -> Any:
        try:
            return action()
        except HubError as exc:
            reason = (
                "permission denied"
                if exc.reason in (FailureReason.PERMISSION_DENIED, FailureReason.AUTH_FAILURE)
                else exc.message
            )
            logger.warning("Could not %s: %s", label, exc.message)
            result.problems.append(f"{label}: {reason}")
            return None

    # 1. Sticky summary comment (created once, then updated in place).
    def upsert_comment() -> bool:
        existing = _find_own_comment(gh.list_issue_comments(pr_number), comment_authors)
        if existing:
            gh.update_issue_comment(int(existing["id"]), body)
        else:
            gh.create_issue_comment(pr_number, body)
        return True

    result.comment_posted = bool(attempt("post the summary comment", upsert_comment))

    # 2. Inline comments for new findings only.
    def post_inline() -> int:
        previous = fingerprints_in([c.get("body", "") for c in gh.list_review_comments(pr_number)])
        comments = build_inline_comments(
            outcome.findings,
            min_severity=outcome.inline_min_severity,
            existing_fingerprints=previous,
        )
        if not comments:
            return 0
        review_body = f"AI Hub found {len(comments)} new issue(s) on the changed lines."
        try:
            gh.create_review(pr_number, commit_id=head_sha, body=review_body, comments=comments)
            return len(comments)
        except HubError as exc:
            # One bad anchor (422) rejects the whole review; retry one by one.
            if getattr(exc, "status_code", None) != 422:
                raise
            posted = 0
            for comment in comments:
                try:
                    gh.create_review(
                        pr_number, commit_id=head_sha, body="AI Hub finding", comments=[comment]
                    )
                    posted += 1
                except HubError:
                    logger.info("Skipped an inline comment that could not be anchored")
            return posted

    result.inline_posted = int(attempt("post inline comments", post_inline) or 0)

    # 3. The Check Run that branch protection requires.
    def create_check() -> bool:
        reasons = "\n".join(f"- {r}" for r in outcome.gate_reasons)
        payload = build_check_run_payload(
            outcome.gate_result,
            outcome.gate_reasons,
            head_sha=head_sha,
            summary=reasons[:60000],
            details_url=details_url,
        )
        gh.create_check_run(payload)
        return True

    result.check_run_created = bool(attempt("create the check run", create_check))
    return result
