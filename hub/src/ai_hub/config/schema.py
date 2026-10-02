"""Pydantic v2 schema for .ai-review/config.yml — strict validation."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator

STATIC_TOOLS = ("ruff", "eslint", "semgrep")
SECURITY_TOOLS = ("semgrep", "gitleaks", "bandit", "deps")
SEVERITIES = ("info", "low", "medium", "high", "critical")
CHECK_NAMES = ("static", "security", "tests", "ai_review")


def _validate_tools(value: list[str], allowed: tuple[str, ...]) -> list[str]:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ValueError(f"unknown tool(s) {unknown}; allowed: {list(allowed)}")
    return list(dict.fromkeys(value))


class ReviewMode(StrEnum):
    LIGHT = "light"
    STANDARD = "standard"
    STRICT = "strict"


class PathsConfig(BaseModel, extra="forbid"):
    ignore: list[str] = Field(default_factory=list)
    generated: list[str] = Field(default_factory=list)


class AIReviewConfig(BaseModel, extra="forbid"):
    enabled: bool = True
    max_files: Annotated[int, Field(ge=1, le=200)] = 50
    inline_min_severity: str = "medium"

    @field_validator("inline_min_severity")
    @classmethod
    def _known_severity(cls, value: str) -> str:
        if value not in SEVERITIES:
            raise ValueError(f"must be one of {list(SEVERITIES)}")
        return value


class StaticAnalysisConfig(BaseModel, extra="forbid"):
    enabled: bool = True
    tools: list[str] = Field(default_factory=lambda: ["ruff", "semgrep"])

    @field_validator("tools")
    @classmethod
    def _known_tools(cls, value: list[str]) -> list[str]:
        return _validate_tools(value, STATIC_TOOLS)


class SecurityConfig(BaseModel, extra="forbid"):
    enabled: bool = True
    tools: list[str] = Field(default_factory=lambda: ["semgrep", "gitleaks", "deps"])

    @field_validator("tools")
    @classmethod
    def _known_tools(cls, value: list[str]) -> list[str]:
        return _validate_tools(value, SECURITY_TOOLS)


class TestsConfig(BaseModel, extra="forbid"):
    __test__ = False  # not a pytest class

    command: str | None = None
    coverage_report: str | None = None
    timeout_minutes: Annotated[int, Field(ge=1, le=60)] = 15

    @field_validator("coverage_report")
    @classmethod
    def _inside_workspace(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.replace("\\", "/")
        if (
            normalized.startswith("/")
            or ":" in normalized.split("/")[0]
            or ".." in normalized.split("/")
        ):
            raise ValueError("must be a relative path inside the repository")
        return value


class FailOnConfig(BaseModel, extra="forbid"):
    critical: Annotated[int, Field(ge=0)] = 1
    high: Annotated[int, Field(ge=0)] = 1


class QualityGateConfig(BaseModel, extra="forbid"):
    enabled: bool = True
    coverage_threshold: Annotated[int, Field(ge=0, le=100)] = 80
    fail_on: FailOnConfig = Field(default_factory=FailOnConfig)
    require_tests_pass: bool = True
    required_checks: list[str] = Field(default_factory=lambda: ["static", "security", "ai_review"])

    @field_validator("required_checks")
    @classmethod
    def _known_checks(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(CHECK_NAMES))
        if unknown:
            raise ValueError(f"unknown check(s) {unknown}; allowed: {list(CHECK_NAMES)}")
        return list(dict.fromkeys(value))


class DevPilotConfig(BaseModel, extra="forbid"):
    enabled: bool = True
    base_branch: str = "main"
    test_command: str | None = None
    max_fix_attempts: Annotated[int, Field(ge=0, le=10)] = 3
    max_changed_files: Annotated[int, Field(ge=1, le=100)] = 20
    max_changed_lines: Annotated[int, Field(ge=1, le=5000)] = 800
    allow_workflow_changes: bool = False


class HubConfig(BaseModel, extra="forbid"):
    """Root configuration schema — maps directly to .ai-review/config.yml."""

    version: int = 1
    review_mode: ReviewMode = ReviewMode.STANDARD
    paths: PathsConfig = Field(default_factory=PathsConfig)
    ai_review: AIReviewConfig = Field(default_factory=AIReviewConfig)
    static_analysis: StaticAnalysisConfig = Field(default_factory=StaticAnalysisConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    tests: TestsConfig = Field(default_factory=TestsConfig)
    quality_gate: QualityGateConfig = Field(default_factory=QualityGateConfig)
    devpilot: DevPilotConfig = Field(default_factory=DevPilotConfig)

    @model_validator(mode="after")
    def validate_version(self) -> HubConfig:
        if self.version != 1:
            raise ValueError(
                f"Unsupported config version: {self.version}. Only version 1 is supported."
            )
        return self
