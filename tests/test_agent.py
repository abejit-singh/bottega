"""Tests for the agent loop, driven by the scripted FakeClient."""

from __future__ import annotations

import pytest

from bottega.agents import AgentError, build_agent
from bottega.config import Settings
from bottega.costs import CostTracker
from bottega.events import Event
from bottega.tools import Workspace
from tests.conftest import FakeClient, make_message, text, tool_use


def _researcher(client: FakeClient, settings: Settings, workspace: Workspace,
                costs: CostTracker, events: list[Event] | None = None):
    return build_agent(
        "researcher", client=client, settings=settings, workspace=workspace, costs=costs,
        on_event=(events.append if events is not None else lambda e: None),
    )


def test_agent_writes_file_then_finishes(settings, workspace, costs) -> None:
    client = FakeClient([
        make_message([text("Writing research."),
                      tool_use("write_file", {"path": "research.md", "content": "# Research"})],
                     stop_reason="tool_use"),
        make_message([text("Done: pizzeria research written.")]),
    ])
    events: list[Event] = []
    result = _researcher(client, settings, workspace, costs, events).run("Client goal: pizzeria")

    assert result.completed
    assert result.turns == 2
    assert result.files_written == ["research.md"]
    assert workspace.read_text("research.md") == "# Research"
    assert result.final_text == "Done: pizzeria research written."
    # Two calls of 1000 in / 500 out on Sonnet 5.5
    assert result.cost_usd == pytest.approx(2 * (1000 * 2 + 500 * 10) / 1e6)
    assert costs.by_agent()["Researcher"].calls == 2
    assert [e.kind for e in events] == [
        "agent_start", "text", "tool_call", "tool_result", "text", "agent_done"
    ]


def test_second_request_contains_tool_result(settings, workspace, costs) -> None:
    client = FakeClient([
        make_message([tool_use("list_files", {}, id_="toolu_42")], stop_reason="tool_use"),
        make_message([text("ok")]),
    ])
    _researcher(client, settings, workspace, costs).run("task")

    last_user_turn = client.requests[1]["messages"][-1]
    assert last_user_turn["role"] == "user"
    assert last_user_turn["content"][0]["tool_use_id"] == "toolu_42"
    assert "empty" in last_user_turn["content"][0]["content"]


def test_tool_errors_are_reported_not_raised(settings, workspace, costs) -> None:
    client = FakeClient([
        make_message([tool_use("write_file", {"path": "../evil.txt", "content": "x"}, "t1"),
                      tool_use("delete_everything", {}, "t2")],
                     stop_reason="tool_use"),
        make_message([text("Sorry, fixed.")]),
    ])
    result = _researcher(client, settings, workspace, costs).run("task")

    results = client.requests[1]["messages"][-1]["content"]
    assert len(results) == 2  # both results in ONE user message
    assert all(r["is_error"] for r in results)
    assert "escapes" in results[0]["content"]
    assert "Unknown tool" in results[1]["content"]
    assert result.completed and result.files_written == []


def test_truncated_tool_call_is_not_executed(settings, workspace, costs) -> None:
    client = FakeClient([
        make_message([tool_use("write_file", {"path": "a.md", "content": "half"})],
                     stop_reason="max_tokens"),
        make_message([text("ok")]),
    ])
    _researcher(client, settings, workspace, costs).run("task")
    assert workspace.list_files() == []
    assert "max_tokens" in client.requests[1]["messages"][-1]["content"][0]["content"]


def test_stops_at_max_turns(workspace, costs, tmp_path) -> None:
    settings = Settings(output_dir=tmp_path, max_turns=3)
    loop = [make_message([tool_use("list_files", {})], stop_reason="tool_use") for _ in range(3)]
    result = _researcher(FakeClient(loop), settings, workspace, costs).run("task")
    assert result.stop_reason == "max_turns"
    assert not result.completed


def test_refusal_raises(settings, workspace, costs) -> None:
    client = FakeClient([make_message([], stop_reason="refusal")])
    with pytest.raises(AgentError, match="declined"):
        _researcher(client, settings, workspace, costs).run("task")


def test_request_shape(settings, workspace, costs) -> None:
    client = FakeClient([make_message([text("ok")])])
    _researcher(client, settings, workspace, costs).run("task")
    req = client.requests[0]
    assert req["model"] == "claude-sonnet-5-5"
    assert req["output_config"] == {"effort": "medium"}
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert {t["name"] for t in req["tools"]} == {
        "write_file", "read_file", "list_files", "search_memory", "read_memory", "save_memory"
    }
    assert req["fallbacks"] == "default"


def test_fallback_can_be_disabled(workspace, costs, tmp_path) -> None:
    client = FakeClient([make_message([text("ok")])])
    settings = Settings(output_dir=tmp_path, refusal_fallback=False)
    _researcher(client, settings, workspace, costs).run("task")
    assert "fallbacks" not in client.requests[0] and "betas" not in client.requests[0]
