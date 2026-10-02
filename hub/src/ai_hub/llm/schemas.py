"""Tool JSON schemas for Bedrock Converse API tool use."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ai_hub.models import FindingCategory, Severity


class AIFinding(BaseModel):
    """A single finding reported by the AI reviewer via tool use."""

    category: str = Field(description="One of: bug, security, performance, maintainability, style")
    severity: str = Field(description="One of: info, low, medium, high, critical")
    file: str = Field(description="File path relative to the repository root")
    line_start: int = Field(description="Starting line number of the finding")
    line_end: int | None = Field(default=None, description="Ending line number (optional)")
    title: str = Field(description="Short one-line title of the finding")
    explanation: str = Field(description="Detailed explanation of the issue")
    suggested_fix: str | None = Field(default=None, description="Suggested code fix or approach")
    confidence: float = Field(ge=0.0, le=1.0, default=0.8, description="Confidence level 0.0-1.0")

    def to_category(self) -> FindingCategory:
        try:
            return FindingCategory(self.category)
        except ValueError:
            return FindingCategory.MAINTAINABILITY

    def to_severity(self) -> Severity:
        try:
            return Severity(self.severity)
        except ValueError:
            return Severity.MEDIUM


class ReviewFindings(BaseModel):
    """Container for all AI review findings — the tool use response model."""

    findings: list[AIFinding] = Field(default_factory=list)
    summary: str = Field(description="Brief overall summary of the review")


REPORT_FINDINGS_TOOL: dict[str, Any] = {
    "name": "report_findings",
    "description": (
        "Report code review findings. Call this tool with all findings from "
        "your review of the code changes. Include a summary of the overall "
        "code quality."
    ),
    "inputSchema": {
        "json": {
            "type": "object",
            "required": ["findings", "summary"],
            "properties": {
                "findings": {
                    "type": "array",
                    "description": "List of code review findings",
                    "items": {
                        "type": "object",
                        "required": [
                            "category",
                            "severity",
                            "file",
                            "line_start",
                            "title",
                            "explanation",
                        ],
                        "properties": {
                            "category": {
                                "type": "string",
                                "enum": [
                                    "bug",
                                    "security",
                                    "performance",
                                    "maintainability",
                                    "style",
                                ],
                            },
                            "severity": {
                                "type": "string",
                                "enum": ["info", "low", "medium", "high", "critical"],
                            },
                            "file": {"type": "string"},
                            "line_start": {"type": "integer"},
                            "line_end": {"type": "integer"},
                            "title": {"type": "string"},
                            "explanation": {"type": "string"},
                            "suggested_fix": {"type": "string"},
                            "confidence": {
                                "type": "number",
                                "minimum": 0.0,
                                "maximum": 1.0,
                            },
                        },
                    },
                },
                "summary": {
                    "type": "string",
                    "description": "Brief overall summary of the review",
                },
            },
        }
    },
}
