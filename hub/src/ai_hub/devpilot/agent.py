"""DevPilot agent — tool-use loop with budgets and sandboxing.

``AgentSession`` keeps the conversation across several ``send()`` calls so the
orchestrator can feed test failures back to the model for a repair attempt
without losing context.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ai_hub.devpilot.tools import (
    ToolPolicy,
    bedrock_tool_specs,
    tool_apply_edit,
    tool_list_dir,
    tool_read_file,
    tool_run_tests,
    tool_search_code,
    tool_write_file,
)
from ai_hub.errors import FailureReason, LLMError

if TYPE_CHECKING:
    from ai_hub.llm.bedrock import BedrockClient

logger = logging.getLogger("ai_hub.devpilot")

_MAX_TOOL_CALLS = 50
_MAX_UNKNOWN_TOOLS = 3
_MAX_SECONDS = 20 * 60
_MAX_CONTEXT_CHARS = 600_000


@dataclass
class AgentResult:
    finished: bool
    summary: str
    tool_calls: int
    files_changed: list[str]
    failure_reason: str | None = None


class AgentSession:
    """A multi-turn tool-use conversation with a shared budget."""

    def __init__(
        self,
        client: BedrockClient,
        system_prompt: str,
        policy: ToolPolicy,
        *,
        max_tool_calls: int = _MAX_TOOL_CALLS,
        max_unknown_tools: int = _MAX_UNKNOWN_TOOLS,
        max_seconds: float = _MAX_SECONDS,
        max_context_chars: int = _MAX_CONTEXT_CHARS,
    ) -> None:
        self.client = client
        self.system_prompt = system_prompt
        self.policy = policy
        self.max_tool_calls = max_tool_calls
        self.max_unknown_tools = max_unknown_tools
        self.max_context_chars = max_context_chars
        self.messages: list[dict[str, Any]] = []
        self.tool_calls = 0
        self.unknown_tools = 0
        self.files_changed: list[str] = []
        self._deadline = time.monotonic() + max_seconds
        self._context_chars = 0
        self._tools = bedrock_tool_specs()

    # ── public API ────────────────────────────────────────────

    def send(self, text: str) -> AgentResult:
        """Add a user message and run the model until it finishes or a budget is hit."""
        self._append_user_content([{"text": text}])
        self._context_chars += len(text)
        return self._run()

    # ── internals ─────────────────────────────────────────────

    def _result(
        self, finished: bool, summary: str, failure_reason: FailureReason | str | None = None
    ) -> AgentResult:
        return AgentResult(
            finished=finished,
            summary=summary,
            tool_calls=self.tool_calls,
            files_changed=list(self.files_changed),
            failure_reason=str(failure_reason) if failure_reason else None,
        )

    def _append_user_content(self, content: list[dict[str, Any]]) -> None:
        # Bedrock requires alternating roles, so merge into a trailing user message.
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages[-1]["content"].extend(content)
        else:
            self.messages.append({"role": "user", "content": content})

    def _track_change(self, path: str) -> None:
        if path not in self.files_changed:
            self.files_changed.append(path)

    def _dispatch(self, name: str, tool_input: dict[str, Any]) -> dict[str, Any] | None:
        """Execute a known tool. Returns None for an unknown tool name."""
        policy = self.policy
        if name == "list_dir":
            return tool_list_dir(str(tool_input.get("path", ".")), policy)
        if name == "read_file":
            return tool_read_file(str(tool_input.get("path", "")), policy)
        if name == "write_file":
            path = str(tool_input.get("path", ""))
            result = tool_write_file(path, tool_input.get("content", ""), policy)
            if result.get("success"):
                self._track_change(path)
            return result
        if name == "apply_edit":
            path = str(tool_input.get("path", ""))
            result = tool_apply_edit(
                path, tool_input.get("old_text", ""), tool_input.get("new_text", ""), policy
            )
            if result.get("success"):
                self._track_change(path)
            return result
        if name == "search_code":
            return tool_search_code(str(tool_input.get("query", "")), policy)
        if name == "run_tests":
            return tool_run_tests(policy)
        return None

    def _run(self) -> AgentResult:
        while True:
            if self.tool_calls >= self.max_tool_calls:
                return self._result(
                    False,
                    f"Budget exhausted after {self.max_tool_calls} tool calls",
                    FailureReason.BUDGET_EXHAUSTED,
                )
            if time.monotonic() > self._deadline:
                return self._result(False, "Agent time budget exhausted", FailureReason.TIMEOUT)
            if self._context_chars > self.max_context_chars:
                return self._result(
                    False, "Agent context budget exhausted", FailureReason.BUDGET_EXHAUSTED
                )

            try:
                response = self.client.converse(
                    self.messages, system=self.system_prompt, tools=self._tools
                )
            except LLMError as exc:
                return self._result(False, f"LLM error: {exc.message}", exc.reason)

            output = response.get("output", {})
            blocks: list[dict[str, Any]] = output.get("message", {}).get("content", [])
            stop_reason = response.get("stopReason", "")

            # Bedrock rejects empty assistant content on the next turn.
            self.messages.append(
                {"role": "assistant", "content": blocks or [{"text": "(no output)"}]}
            )
            self._context_chars += len(json.dumps(blocks, default=str))

            tool_uses = [b["toolUse"] for b in blocks if "toolUse" in b]
            if not tool_uses:
                text = " ".join(b.get("text", "") for b in blocks if "text" in b)
                if stop_reason == "end_turn":
                    return self._result(True, text[:500] or "Agent finished without summary")
                return self._result(
                    False,
                    f"Model stopped without finishing (stopReason={stop_reason or 'unknown'})",
                    FailureReason.INVALID_RESPONSE,
                )

            tool_results: list[dict[str, Any]] = []
            finish_summary: str | None = None
            abort: AgentResult | None = None

            for tool_use in tool_uses:
                tool_name = str(tool_use.get("name", ""))
                tool_id = tool_use.get("toolUseId", "")
                raw_input = tool_use.get("input", {})
                tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
                self.tool_calls += 1

                result: dict[str, Any]
                if finish_summary is not None:
                    result = {"error": "Skipped: finish was already called"}
                elif self.tool_calls > self.max_tool_calls:
                    result = {"error": "Tool call budget exhausted"}
                elif tool_name == "finish":
                    finish_summary = str(tool_input.get("summary") or "Implementation complete")
                    result = {"success": True}
                else:
                    dispatched = self._dispatch(tool_name, tool_input)
                    if dispatched is None:
                        self.unknown_tools += 1
                        result = {"error": f"Unknown tool: {tool_name}"}
                        if self.unknown_tools >= self.max_unknown_tools and abort is None:
                            abort = self._result(
                                False,
                                "Too many unknown tool calls",
                                FailureReason.CANNOT_SOLVE,
                            )
                    else:
                        result = dispatched

                self._context_chars += len(json.dumps(result, default=str))
                tool_results.append(
                    {"toolResult": {"toolUseId": tool_id, "content": [{"json": result}]}}
                )

            # Every toolUse needs a toolResult or the conversation cannot continue.
            self._append_user_content(tool_results)

            if abort is not None:
                return abort
            if finish_summary is not None:
                return self._result(True, finish_summary)


def run_agent_loop(
    client: BedrockClient,
    system_prompt: str,
    initial_message: str,
    policy: ToolPolicy,
    *,
    max_tool_calls: int = _MAX_TOOL_CALLS,
) -> AgentResult:
    """Run a single-shot tool-use loop with budgets and sandboxing."""
    session = AgentSession(client, system_prompt, policy, max_tool_calls=max_tool_calls)
    return session.send(initial_message)
