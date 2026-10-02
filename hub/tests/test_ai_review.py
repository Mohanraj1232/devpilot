"""Tests for ai_hub.review.ai_review — AI review orchestrator."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from ai_hub.analysis.diff import parse_unified_diff
from ai_hub.config.schema import HubConfig
from ai_hub.llm.schemas import AIFinding, ReviewFindings
from ai_hub.models import CheckStatus, FindingSource
from ai_hub.review.ai_review import run_ai_review

FIXTURES = Path(__file__).parent / "fixtures"


def _make_mock_client(
    findings: list[dict] | None = None, summary: str = "Review done"
) -> MagicMock:
    client = MagicMock()
    ai_findings = []
    if findings:
        ai_findings = [AIFinding(**f) for f in findings]
    result = ReviewFindings(findings=ai_findings, summary=summary)
    client.converse_with_tool_retry.return_value = result
    return client


class TestRunAIReview:
    def test_disabled_returns_skipped(self) -> None:
        config = HubConfig.model_validate(
            {
                "version": 1,
                "ai_review": {"enabled": False},
            }
        )
        client = MagicMock()
        findings, check = run_ai_review(client, [], config)
        assert check.status == CheckStatus.SKIPPED
        assert findings == []
        client.converse_with_tool_retry.assert_not_called()

    def test_no_diffs_returns_success(self) -> None:
        config = HubConfig()
        client = MagicMock()
        findings, check = run_ai_review(client, [], config)
        assert check.status == CheckStatus.SUCCESS
        assert findings == []

    def test_successful_review_with_findings(self) -> None:
        config = HubConfig()
        diff_text = (FIXTURES / "sample.diff").read_text()
        file_diffs = parse_unified_diff(diff_text)

        client = _make_mock_client(
            findings=[
                {
                    "category": "security",
                    "severity": "high",
                    "file": "app.py",
                    "line_start": 15,
                    "title": "Debug mode enabled",
                    "explanation": "Running with debug=True in production is dangerous",
                }
            ],
            summary="Found security issue",
        )

        findings, check = run_ai_review(client, file_diffs, config)
        assert len(findings) == 1
        assert findings[0].source == FindingSource.AI
        assert findings[0].tool == "ai_review"
        assert check.status == CheckStatus.FAILED

    def test_clean_review(self) -> None:
        config = HubConfig()
        diff_text = (FIXTURES / "sample.diff").read_text()
        file_diffs = parse_unified_diff(diff_text)

        client = _make_mock_client(findings=[], summary="All clean")

        findings, check = run_ai_review(client, file_diffs, config)
        assert len(findings) == 0
        assert check.status == CheckStatus.SUCCESS

    def test_respects_max_files(self) -> None:
        config = HubConfig.model_validate(
            {
                "version": 1,
                "ai_review": {"enabled": True, "max_files": 1},
            }
        )
        diff_text = (FIXTURES / "sample.diff").read_text()
        file_diffs = parse_unified_diff(diff_text)

        client = _make_mock_client()
        run_ai_review(client, file_diffs, config)
        client.converse_with_tool_retry.assert_called_once()
