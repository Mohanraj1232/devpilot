"""Tests for AgentSession: tool wiring, multi-turn conversations and budgets."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from ai_hub.devpilot.agent import AgentSession
from ai_hub.devpilot.tools import ToolPolicy

if TYPE_CHECKING:
    from pathlib import Path


def _tool_turn(*uses: tuple[str, str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "output": {
            "message": {
                "content": [
                    {"toolUse": {"toolUseId": tid, "name": name, "input": inp}}
                    for tid, name, inp in uses
                ]
            }
        },
        "stopReason": "tool_use",
    }


def _text_turn(text: str, stop: str = "end_turn") -> dict[str, Any]:
    return {"output": {"message": {"content": [{"text": text}]}}, "stopReason": stop}


class FakeClient:
    """Scripted Bedrock stand-in that snapshots the messages it receives on every call."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def converse(
        self,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"messages": copy.deepcopy(messages), "system": system, "tools": tools})
        return self._responses.pop(0)


def _client(*responses: dict[str, Any]) -> FakeClient:
    return FakeClient(list(responses))


def test_tools_are_passed_to_bedrock(tmp_path: Path) -> None:
    client = _client(_text_turn("done"))
    AgentSession(client, "sys", ToolPolicy(workspace=tmp_path)).send("go")
    call = client.calls[0]
    assert call["system"] == "sys"
    assert {t["toolSpec"]["name"] for t in call["tools"]} >= {"read_file", "run_tests", "finish"}


def test_run_tests_tool_uses_supplied_runner(tmp_path: Path) -> None:
    client = _client(
        _tool_turn(("t1", "run_tests", {})),
        _tool_turn(("t2", "finish", {"summary": "ok"})),
    )
    policy = ToolPolicy(workspace=tmp_path, test_runner=lambda: {"status": "success"})
    result = AgentSession(client, "sys", policy).send("go")
    assert result.finished is True
    results = client.calls[1]["messages"][-1]["content"]
    assert results[0]["toolResult"]["content"][0]["json"] == {"status": "success"}


def test_run_tests_tool_reports_unavailable(tmp_path: Path) -> None:
    client = _client(
        _tool_turn(("t1", "run_tests", {})),
        _tool_turn(("t2", "finish", {"summary": "ok"})),
    )
    AgentSession(client, "sys", ToolPolicy(workspace=tmp_path)).send("go")
    results = client.calls[1]["messages"][-1]["content"]
    assert "error" in results[0]["toolResult"]["content"][0]["json"]


def test_conversation_continues_after_finish(tmp_path: Path) -> None:
    """After `finish`, a follow-up message must merge into the tool-result user turn."""
    client = _client(
        _tool_turn(("t1", "finish", {"summary": "first"})),
        _tool_turn(("t2", "finish", {"summary": "fixed"})),
    )
    session = AgentSession(client, "sys", ToolPolicy(workspace=tmp_path))
    assert session.send("implement").summary == "first"
    assert session.send("tests failed").summary == "fixed"

    roles = [m["role"] for m in session.messages]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    # No two consecutive messages share a role and every toolUse got a toolResult.
    merged = session.messages[2]["content"]
    assert "toolResult" in merged[0]
    assert merged[1] == {"text": "tests failed"}


def test_every_tool_use_in_a_turn_gets_a_result(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hi")
    client = _client(
        _tool_turn(
            ("t1", "read_file", {"path": "a.txt"}),
            ("t2", "finish", {"summary": "done"}),
            ("t3", "read_file", {"path": "a.txt"}),
        ),
    )
    session = AgentSession(client, "sys", ToolPolicy(workspace=tmp_path))
    result = session.send("go")
    assert result.finished is True
    results = session.messages[-1]["content"]
    assert [r["toolResult"]["toolUseId"] for r in results] == ["t1", "t2", "t3"]
    # Calls after finish are skipped, not executed.
    assert "Skipped" in results[2]["toolResult"]["content"][0]["json"]["error"]


def test_model_stopping_without_finishing_is_a_failure(tmp_path: Path) -> None:
    client = _client(_text_turn("partial...", stop="max_tokens"))
    result = AgentSession(client, "sys", ToolPolicy(workspace=tmp_path)).send("go")
    assert result.finished is False
    assert result.failure_reason == "invalid_response"


def test_budget_is_shared_across_sends(tmp_path: Path) -> None:
    client = _client(
        _tool_turn(("t1", "list_dir", {"path": "."})),
        _tool_turn(("t2", "finish", {"summary": "ok"})),
        _tool_turn(("t3", "list_dir", {"path": "."})),
    )
    session = AgentSession(client, "sys", ToolPolicy(workspace=tmp_path), max_tool_calls=3)
    assert session.send("a").finished is True
    result = session.send("b")
    assert result.finished is False
    assert result.failure_reason == "budget_exhausted"


def test_context_budget(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("x" * 20_000)
    turn = _tool_turn(("t", "read_file", {"path": "big.txt"}))
    client = _client(*[turn] * 10)
    session = AgentSession(client, "sys", ToolPolicy(workspace=tmp_path), max_context_chars=30_000)
    result = session.send("go")
    assert result.finished is False
    assert result.failure_reason == "budget_exhausted"


def test_time_budget(tmp_path: Path) -> None:
    client = _client(_text_turn("never reached"))
    session = AgentSession(client, "sys", ToolPolicy(workspace=tmp_path), max_seconds=-1)
    result = session.send("go")
    assert result.failure_reason == "timeout"
    assert client.calls == []


def test_denied_path_is_reported_to_the_model_not_written(tmp_path: Path) -> None:
    client = _client(
        _tool_turn(("t1", "write_file", {"path": ".github/workflows/x.yml", "content": "on: x"})),
        _text_turn("done"),
    )
    session = AgentSession(client, "sys", ToolPolicy(workspace=tmp_path))
    result = session.send("go")
    assert result.files_changed == []
    assert not (tmp_path / ".github").exists()


def test_malformed_tool_input_does_not_crash(tmp_path: Path) -> None:
    client = _client(
        {
            "output": {
                "message": {
                    "content": [
                        {"toolUse": {"toolUseId": "t1", "name": "read_file", "input": "oops"}}
                    ]
                }
            },
            "stopReason": "tool_use",
        },
        _tool_turn(("t2", "finish", {"summary": "ok"})),
    )
    result = AgentSession(client, "sys", ToolPolicy(workspace=tmp_path)).send("go")
    assert result.finished is True
