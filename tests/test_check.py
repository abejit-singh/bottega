"""Tests for the preflight check (python -m bottega check)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from bottega.check import run_checks, summarize
from bottega.config import Settings
from bottega.demo import DemoClient

_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages/count_tokens")


class CountingClient(DemoClient):
    """The demo client plus a fake token counter that records what it was sent."""

    def __init__(self, error: Exception | None = None) -> None:
        super().__init__(delay=0)
        self.error = error
        self.counted: list[dict[str, Any]] = []

    def count_tokens(self, **request: Any) -> Any:
        if self.error:
            raise self.error
        self.counted.append(request)
        return SimpleNamespace(input_tokens=1234)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(output_dir=tmp_path / "output", memory_dir=tmp_path / "memory")


def test_missing_key_is_reported_and_blocks(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    results = run_checks(settings, lambda: pytest.fail("no client should be built"))
    assert [(r.name, r.ok) for r in results] == [("API key", False)]
    assert summarize(results) == {"status": "missing_key", "message": results[0].detail, "blocking": True}


def test_every_agent_is_checked_for_free(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    client = CountingClient()
    results = run_checks(settings, lambda: client)
    assert all(r.ok for r in results) and len(results) == 7       # the key + six agents
    assert results[1].name == "CEO prompt and tools" and "1,234 tokens" in results[1].detail
    sent = client.counted[0]
    assert sent["model"] == "claude-sonnet-5-5" and sent["output_config"] == {"effort": "medium"}
    assert "fallbacks" not in sent and "max_tokens" not in sent  # generation-only fields removed
    assert all("eager_input_streaming" not in tool for tool in sent["tools"])
    assert summarize(results)["status"] == "ready"


def test_a_rejected_key_stops_the_check_at_once(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-wrong")
    error = anthropic.AuthenticationError("invalid x-api-key",
                                          response=httpx2.Response(401, request=_REQUEST), body=None)
    results = run_checks(settings, lambda: CountingClient(error))
    assert [r.ok for r in results] == [True, False]              # not six identical failures
    assert "rejected" in results[1].detail and results[1].fatal
    assert summarize(results)["blocking"] is True


def test_live_check_sends_one_real_shaped_request(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    results = run_checks(settings, lambda: CountingClient(), roles=("ceo",), live=True)
    live = results[-1]
    assert live.name == "Live request" and live.ok and "cost $" in live.detail
