"""Pydantic v2 schema for .ai-review/config.yml — strict validation."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, model_validator


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


class StaticAnalysisConfig(BaseModel, extra="forbid"):
    enabled: bool = True
    tools: list[str] = Field(default_factory=lambda: ["ruff", "semgrep"])


class SecurityConfig(BaseModel, extra="forbid"):
    enabled: bool = True
    tools: list[str] = Field(default_factory=lambda: ["semgrep", "gitleaks", "deps"])


class TestsConfig(BaseModel, extra="forbid"):
    command: str | None = None
    coverage_report: str | None = None
    timeout_minutes: Annotated[int, Field(ge=1, le=60)] = 15


class FailOnConfig(BaseModel, extra="forbid"):
    critical: Annotated[int, Field(ge=0)] = 1
    high: Annotated[int, Field(ge=0)] = 1


class QualityGateConfig(BaseModel, extra="forbid"):
    enabled: bool = True
    coverage_threshold: Annotated[int, Field(ge=0, le=100)] = 80
    fail_on: FailOnConfig = Field(default_factory=FailOnConfig)
    require_tests_pass: bool = True
    required_checks: list[str] = Field(default_factory=lambda: ["static", "security", "ai_review"])


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
