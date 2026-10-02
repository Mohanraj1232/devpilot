"""Structured LLM calls used by DevPilot: issue triage and implementation planning."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from ai_hub.devpilot.trigger import IssueSnapshot, TriageResult
from ai_hub.safety.untrusted import wrap_untrusted

if TYPE_CHECKING:
    from ai_hub.llm.bedrock import BedrockClient

_PROMPTS = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    return (_PROMPTS / f"{name}.md").read_text(encoding="utf-8")


class TriageDecision(BaseModel):
    actionable: bool
    missing_info: list[str] = Field(default_factory=list)
    already_resolved_hint: bool = False
    rationale: str = ""


class ImplementationPlan(BaseModel):
    approach: str
    files_to_touch: list[str] = Field(default_factory=list)
    test_strategy: str = ""


def _tool(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "description": description, "inputSchema": {"json": schema}}


TRIAGE_TOOL = _tool(
    "report_triage",
    "Report whether the issue is actionable.",
    {
        "type": "object",
        "required": ["actionable"],
        "properties": {
            "actionable": {"type": "boolean"},
            "missing_info": {"type": "array", "items": {"type": "string"}},
            "already_resolved_hint": {"type": "boolean"},
            "rationale": {"type": "string"},
        },
    },
)

PLAN_TOOL = _tool(
    "submit_plan",
    "Submit the implementation plan.",
    {
        "type": "object",
        "required": ["approach", "files_to_touch"],
        "properties": {
            "approach": {"type": "string"},
            "files_to_touch": {"type": "array", "items": {"type": "string"}},
            "test_strategy": {"type": "string"},
        },
    },
)


def _issue_block(issue: IssueSnapshot) -> str:
    return wrap_untrusted(
        "untrusted_issue", f"Title: {issue.title}\n\n{issue.body}", max_chars=10_000
    )


def triage_issue_llm(client: BedrockClient, issue: IssueSnapshot) -> TriageResult:
    """Ask the model whether the issue is actionable. LLM failures propagate as LLMError."""
    decision: TriageDecision = client.converse_with_tool_retry(
        [{"role": "user", "content": [{"text": _issue_block(issue)}]}],
        load_prompt("triage"),
        TRIAGE_TOOL,
        TriageDecision,
    )
    return TriageResult(
        actionable=decision.actionable,
        missing_info=decision.missing_info,
        already_resolved_hint=decision.already_resolved_hint,
        rationale=decision.rationale,
    )


def make_plan(client: BedrockClient, issue: IssueSnapshot, repo_map: str) -> ImplementationPlan:
    """Ask the model for a structured implementation plan."""
    repo_block = wrap_untrusted("untrusted_repo_map", repo_map, max_chars=16_000)
    text = f"{_issue_block(issue)}\n\n{repo_block}"
    plan: ImplementationPlan = client.converse_with_tool_retry(
        [{"role": "user", "content": [{"text": text}]}],
        load_prompt("plan"),
        PLAN_TOOL,
        ImplementationPlan,
    )
    return plan
