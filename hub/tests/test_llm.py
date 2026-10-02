"""Tests for ai_hub.llm — Bedrock client, schemas, and AI review orchestration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from ai_hub.errors import FailureReason, LLMError
from ai_hub.llm.bedrock import BedrockClient
from ai_hub.llm.schemas import REPORT_FINDINGS_TOOL, AIFinding, ReviewFindings
from ai_hub.models import FindingCategory, Severity


class TestAIFinding:
    def test_valid_finding(self) -> None:
        f = AIFinding(
            category="bug",
            severity="high",
            file="main.py",
            line_start=10,
            title="Null pointer",
            explanation="Variable may be None",
        )
        assert f.to_category() == FindingCategory.BUG
        assert f.to_severity() == Severity.HIGH

    def test_unknown_category_defaults(self) -> None:
        f = AIFinding(
            category="unknown",
            severity="high",
            file="x.py",
            line_start=1,
            title="t",
            explanation="e",
        )
        assert f.to_category() == FindingCategory.MAINTAINABILITY

    def test_unknown_severity_defaults(self) -> None:
        f = AIFinding(
            category="bug",
            severity="extreme",
            file="x.py",
            line_start=1,
            title="t",
            explanation="e",
        )
        assert f.to_severity() == Severity.MEDIUM

    def test_confidence_bounds(self) -> None:
        with pytest.raises(ValidationError):
            AIFinding(
                category="bug",
                severity="high",
                file="x.py",
                line_start=1,
                title="t",
                explanation="e",
                confidence=1.5,
            )


class TestReviewFindings:
    def test_empty_findings(self) -> None:
        rf = ReviewFindings(findings=[], summary="All clean")
        assert len(rf.findings) == 0

    def test_with_findings(self) -> None:
        rf = ReviewFindings(
            findings=[
                AIFinding(
                    category="security",
                    severity="critical",
                    file="auth.py",
                    line_start=5,
                    title="SQL injection",
                    explanation="Unsanitized input in query",
                )
            ],
            summary="Found 1 critical issue",
        )
        assert len(rf.findings) == 1
        assert rf.findings[0].to_severity() == Severity.CRITICAL


class TestReportFindingsTool:
    def test_schema_structure(self) -> None:
        assert REPORT_FINDINGS_TOOL["name"] == "report_findings"
        schema = REPORT_FINDINGS_TOOL["inputSchema"]["json"]
        assert "findings" in schema["properties"]
        assert "summary" in schema["properties"]
        assert schema["type"] == "object"


class TestBedrockClient:
    def _make_client(self) -> BedrockClient:
        with patch("boto3.Session"):
            client = BedrockClient(model_id="test-model", region="us-east-1")
        return client

    def test_converse_success(self) -> None:
        client = self._make_client()
        mock_response = {
            "output": {
                "message": {
                    "content": [{"text": "Review complete."}],
                }
            },
            "stopReason": "end_turn",
        }
        client.client.converse = MagicMock(return_value=mock_response)

        result = client.converse(messages=[{"role": "user", "content": [{"text": "hello"}]}])
        assert result["stopReason"] == "end_turn"

    def test_converse_with_tool_success(self) -> None:
        client = self._make_client()
        mock_response = {
            "output": {
                "message": {
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "123",
                                "name": "report_findings",
                                "input": {
                                    "findings": [
                                        {
                                            "category": "bug",
                                            "severity": "high",
                                            "file": "app.py",
                                            "line_start": 10,
                                            "title": "Bug found",
                                            "explanation": "Null check missing",
                                        }
                                    ],
                                    "summary": "One bug found",
                                },
                            }
                        }
                    ],
                }
            },
        }
        client.client.converse = MagicMock(return_value=mock_response)

        result = client.converse_with_tool(
            messages=[{"role": "user", "content": [{"text": "review"}]}],
            system="You are a reviewer.",
            tool_schema=REPORT_FINDINGS_TOOL,
            response_model=ReviewFindings,
        )
        assert isinstance(result, ReviewFindings)
        assert len(result.findings) == 1
        assert result.summary == "One bug found"

    def test_converse_with_tool_no_tool_use(self) -> None:
        client = self._make_client()
        mock_response = {
            "output": {
                "message": {
                    "content": [{"text": "I won't use the tool."}],
                }
            },
        }
        client.client.converse = MagicMock(return_value=mock_response)

        with pytest.raises(LLMError) as exc_info:
            client.converse_with_tool(
                messages=[{"role": "user", "content": [{"text": "review"}]}],
                system="System",
                tool_schema=REPORT_FINDINGS_TOOL,
                response_model=ReviewFindings,
            )
        assert exc_info.value.reason == FailureReason.INVALID_RESPONSE

    def test_converse_with_tool_invalid_response(self) -> None:
        client = self._make_client()
        mock_response = {
            "output": {
                "message": {
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "123",
                                "name": "report_findings",
                                "input": {"bad_field": "invalid"},
                            }
                        }
                    ],
                }
            },
        }
        client.client.converse = MagicMock(return_value=mock_response)

        with pytest.raises(LLMError) as exc_info:
            client.converse_with_tool(
                messages=[{"role": "user", "content": [{"text": "review"}]}],
                system="System",
                tool_schema=REPORT_FINDINGS_TOOL,
                response_model=ReviewFindings,
            )
        assert exc_info.value.reason == FailureReason.INVALID_RESPONSE

    def test_converse_with_tool_retry_success(self) -> None:
        client = self._make_client()

        bad_response = {
            "output": {
                "message": {
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "1",
                                "name": "report_findings",
                                "input": {"bad": "data"},
                            }
                        }
                    ],
                }
            },
        }
        good_response = {
            "output": {
                "message": {
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "2",
                                "name": "report_findings",
                                "input": {
                                    "findings": [],
                                    "summary": "All clean",
                                },
                            }
                        }
                    ],
                }
            },
        }
        client.client.converse = MagicMock(side_effect=[bad_response, good_response])

        result = client.converse_with_tool_retry(
            messages=[{"role": "user", "content": [{"text": "review"}]}],
            system="System",
            tool_schema=REPORT_FINDINGS_TOOL,
            response_model=ReviewFindings,
        )
        assert isinstance(result, ReviewFindings)
        assert result.summary == "All clean"
        assert client.client.converse.call_count == 2
