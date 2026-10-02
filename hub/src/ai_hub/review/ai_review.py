"""AI review orchestrator — sends chunked diffs to Bedrock and collects findings."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ai_hub.errors import FailureReason, LLMError
from ai_hub.llm.schemas import REPORT_FINDINGS_TOOL, ReviewFindings
from ai_hub.models import (
    CheckResult,
    CheckStatus,
    Finding,
    FindingSource,
)

if TYPE_CHECKING:
    from ai_hub.analysis.diff import FileDiff
    from ai_hub.config.schema import HubConfig
    from ai_hub.llm.bedrock import BedrockClient

logger = logging.getLogger("ai_hub.review")

_REVIEW_PROMPT_PATH = "ai_hub/llm/prompts/review.md"


def _load_review_prompt(review_mode: str) -> str:
    import importlib.resources

    prompt_text = importlib.resources.files("ai_hub.llm.prompts").joinpath("review.md").read_text()
    return prompt_text.replace("{review_mode}", review_mode)


def _ai_finding_to_finding(af: Any, tool: str = "ai_review") -> Finding:
    return Finding(
        source=FindingSource.AI,
        tool=tool,
        rule_id=None,
        category=af.to_category(),
        severity=af.to_severity(),
        file=af.file,
        line_start=af.line_start,
        line_end=af.line_end,
        title=af.title,
        explanation=af.explanation,
        suggested_fix=af.suggested_fix,
        confidence=af.confidence,
    )


def run_ai_review(
    client: BedrockClient,
    file_diffs: list[FileDiff],
    config: HubConfig,
) -> tuple[list[Finding], CheckResult]:
    """Run AI review on chunked diffs and return findings + check result."""
    from ai_hub.review.chunker import chunk_diffs

    if not config.ai_review.enabled:
        return [], CheckResult(
            name="ai_review",
            status=CheckStatus.SKIPPED,
            summary="AI review disabled in config",
        )

    chunks = chunk_diffs(
        file_diffs,
        max_files=config.ai_review.max_files,
        ignore_patterns=config.paths.ignore,
        generated_patterns=config.paths.generated,
    )

    if not chunks:
        return [], CheckResult(
            name="ai_review",
            status=CheckStatus.SUCCESS,
            summary="No reviewable changes found",
        )

    system_prompt = _load_review_prompt(config.review_mode.value)
    all_findings: list[Finding] = []

    for i, chunk in enumerate(chunks):
        logger.info("Reviewing chunk %d/%d", i + 1, len(chunks))

        messages = [
            {
                "role": "user",
                "content": [{"text": f"Review the following code changes:\n\n{chunk}"}],
            }
        ]

        try:
            result = client.converse_with_tool_retry(
                messages=messages,
                system=system_prompt,
                tool_schema=REPORT_FINDINGS_TOOL,
                response_model=ReviewFindings,
            )

            for af in result.findings:
                all_findings.append(_ai_finding_to_finding(af))

        except LLMError as exc:
            if exc.reason == FailureReason.INVALID_RESPONSE:
                logger.error("AI review chunk %d failed validation: %s", i + 1, exc.message)
                return all_findings, CheckResult(
                    name="ai_review",
                    status=CheckStatus.ERROR,
                    summary=f"AI review failed: {exc.message}",
                )
            raise

    status = CheckStatus.FAILED if all_findings else CheckStatus.SUCCESS
    return all_findings, CheckResult(
        name="ai_review",
        status=status,
        summary=f"{len(all_findings)} finding(s) from AI review",
        details={"chunks_reviewed": len(chunks)},
    )
