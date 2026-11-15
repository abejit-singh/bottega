"""Shared test fixtures, including a fake Anthropic client.

Why a fake client? Real API calls are slow, cost money, need a key and give
different answers each time. The fake replays scripted responses, so we can
test our loop logic (tool dispatch, error handling, cost tracking)
deterministically and offline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from anthropic.types.beta import BetaMessage

from bottega.config import Settings
from bottega.costs import CostTracker
from bottega.tools import Workspace


def make_message(
    content: list[dict[str, Any]],
    stop_reason: str = "end_turn",
    model: str = "claude-sonnet-5-5",
    input_tokens: int = 1000,
    output_tokens: int = 500,
) -> BetaMessage:
    """Build a real SDK message object from plain dicts."""
    return BetaMessage.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
    )


def tool_use(name: str, tool_input: dict[str, Any], id_: str = "toolu_1") -> dict[str, Any]:
    return {"type": "tool_use", "id": id_, "name": name, "input": tool_input}


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


class _FakeStream:
    def __init__(self, message: BetaMessage) -> None:
        self._message = message

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_final_message(self) -> BetaMessage:
        return self._message


class FakeClient:
    """Mimics `client.beta.messages.stream(...)` and records every request."""

    def __init__(self, responses: list[BetaMessage]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.beta = self  # so `client.beta.messages.stream` resolves to self.stream
        self.messages = self

    def stream(self, **request: Any) -> _FakeStream:
        # Snapshot the message list: the agent keeps appending to the same list.
        self.requests.append({**request, "messages": list(request["messages"])})
        if not self._responses:
            raise AssertionError("FakeClient ran out of scripted responses")
        return _FakeStream(self._responses.pop(0))


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path / "project")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(output_dir=tmp_path, max_turns=5)


@pytest.fixture
def costs() -> CostTracker:
    return CostTracker()
