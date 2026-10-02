"""DevPilot agent — tool-use loop with budgets and sandboxing."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ai_hub.devpilot.tools import (
    ToolPolicy,
    tool_list_dir,
    tool_read_file,
    tool_search_code,
    tool_write_file,
)
from ai_hub.errors import FailureReason, LLMError

if TYPE_CHECKING:
    from ai_hub.llm.bedrock import BedrockClient

logger = logging.getLogger("ai_hub.devpilot")

_MAX_TOOL_CALLS = 50
_MAX_UNKNOWN_TOOLS = 3


@dataclass
class AgentResult:
    finished: bool
    summary: str
    tool_calls: int
    files_changed: list[str]
    failure_reason: str | None = None


def _handle_apply_edit(
    path: str, old_text: str, new_text: str, policy: ToolPolicy
) -> dict[str, Any]:
    """Apply a targeted edit to a file."""
    from ai_hub.devpilot.tools import _resolve_safe_path

    safe = _resolve_safe_path(path, policy)
    if safe is None:
        return {"error": f"Path denied: {path}"}
    if not safe.is_file():
        return {"error": f"File not found: {path}"}

    try:
        content = safe.read_text()
        if old_text not in content:
            return {"error": "Old text not found in file"}
        new_content = content.replace(old_text, new_text, 1)
        safe.write_text(new_content)
        return {"success": True, "path": path}
    except OSError as exc:
        return {"error": f"Edit failed: {exc}"}


def run_agent_loop(
    client: BedrockClient,
    system_prompt: str,
    initial_message: str,
    policy: ToolPolicy,
    *,
    max_tool_calls: int = _MAX_TOOL_CALLS,
) -> AgentResult:
    """Run the tool-use agent loop with budgets and sandboxing."""
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"text": initial_message}]},
    ]

    tool_call_count = 0
    unknown_tool_count = 0
    files_changed: list[str] = []

    while tool_call_count < max_tool_calls:
        try:
            response = client.converse(messages, system=system_prompt)
        except LLMError as exc:
            return AgentResult(
                finished=False,
                summary=f"LLM error: {exc.message}",
                tool_calls=tool_call_count,
                files_changed=files_changed,
                failure_reason=exc.reason.value,
            )

        output = response.get("output", {})
        content_blocks = output.get("message", {}).get("content", [])
        stop_reason = response.get("stopReason", "")

        messages.append({"role": "assistant", "content": content_blocks})

        if stop_reason == "end_turn":
            text = " ".join(b.get("text", "") for b in content_blocks if "text" in b)
            return AgentResult(
                finished=True,
                summary=text[:500] or "Agent finished without summary",
                tool_calls=tool_call_count,
                files_changed=files_changed,
            )

        tool_results: list[dict[str, Any]] = []
        for block in content_blocks:
            if "toolUse" not in block:
                continue

            tool_use = block["toolUse"]
            tool_name = tool_use.get("name", "")
            tool_id = tool_use.get("toolUseId", "")
            tool_input = tool_use.get("input", {})
            tool_call_count += 1

            result: dict[str, Any]

            if tool_name == "list_dir":
                result = tool_list_dir(tool_input.get("path", "."), policy)
            elif tool_name == "read_file":
                result = tool_read_file(tool_input.get("path", ""), policy)
            elif tool_name == "write_file":
                result = tool_write_file(
                    tool_input.get("path", ""),
                    tool_input.get("content", ""),
                    policy,
                )
                if result.get("success"):
                    files_changed.append(tool_input.get("path", ""))
            elif tool_name == "apply_edit":
                result = _handle_apply_edit(
                    tool_input.get("path", ""),
                    tool_input.get("old_text", ""),
                    tool_input.get("new_text", ""),
                    policy,
                )
                if result.get("success"):
                    files_changed.append(tool_input.get("path", ""))
            elif tool_name == "search_code":
                result = tool_search_code(tool_input.get("query", ""), policy)
            elif tool_name == "run_tests":
                result = {"status": "test_runner_placeholder"}
            elif tool_name == "finish":
                summary = tool_input.get("summary", "Implementation complete")
                return AgentResult(
                    finished=True,
                    summary=summary,
                    tool_calls=tool_call_count,
                    files_changed=files_changed,
                )
            else:
                unknown_tool_count += 1
                result = {"error": f"Unknown tool: {tool_name}"}
                if unknown_tool_count >= _MAX_UNKNOWN_TOOLS:
                    return AgentResult(
                        finished=False,
                        summary="Too many unknown tool calls",
                        tool_calls=tool_call_count,
                        files_changed=files_changed,
                        failure_reason=FailureReason.CANNOT_SOLVE.value,
                    )

            tool_results.append(
                {
                    "toolResult": {
                        "toolUseId": tool_id,
                        "content": [{"json": result}],
                    }
                }
            )

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

    return AgentResult(
        finished=False,
        summary=f"Budget exhausted after {max_tool_calls} tool calls",
        tool_calls=tool_call_count,
        files_changed=files_changed,
        failure_reason=FailureReason.BUDGET_EXHAUSTED.value,
    )
