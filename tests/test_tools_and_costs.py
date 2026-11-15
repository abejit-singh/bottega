from __future__ import annotations

import pytest

from agentco.costs import CostTracker, TokenUsage, cost_of
from agentco.tools import TOOL_REGISTRY, ToolError

WRITE = TOOL_REGISTRY["write_file"]


def test_validate_accepts_good_input() -> None:
    assert WRITE.validate({"path": "a.md", "content": "hi"}) == {"path": "a.md", "content": "hi"}


@pytest.mark.parametrize(
    ("bad_input", "message"),
    [
        ({"path": "a.md"}, "Missing required"),
        ({"path": "a.md", "content": "x", "mode": "w"}, "Unknown field"),
        ({"path": 42, "content": "x"}, "must be of type string"),
        ({"path": True, "content": "x"}, "must be of type string"),
        ("not a dict", "JSON object"),
    ],
)
def test_validate_rejects_bad_input(bad_input: object, message: str) -> None:
    with pytest.raises(ToolError, match=message):
        WRITE.validate(bad_input)


def test_api_definition_shape() -> None:
    api = WRITE.to_api()
    assert api["name"] == "write_file"
    assert api["input_schema"]["required"] == ["path", "content"]


def test_cost_of_known_model() -> None:
    # 1M input at $2 + 1M output at $10 = $12 for Sonnet 5.5
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost_of("claude-sonnet-5-5", usage) == pytest.approx(12.0)


def test_cache_tokens_are_priced() -> None:
    usage = TokenUsage(cache_write_tokens=1_000_000, cache_read_tokens=1_000_000)
    # write: 2.00 * 1.25 = 2.50 ; read: 0.10
    assert cost_of("claude-sonnet-5-5", usage) == pytest.approx(2.60)


def test_unknown_model_priced_conservatively() -> None:
    usage = TokenUsage(output_tokens=1_000_000)
    assert cost_of("some-future-model", usage) >= cost_of("claude-sonnet-5-5", usage)


def test_tracker_accumulates_per_agent() -> None:
    class U:  # minimal stand-in for the SDK usage object
        input_tokens, output_tokens = 1000, 100
        cache_creation_input_tokens = cache_read_input_tokens = None

    tracker = CostTracker()
    tracker.record("Researcher", "claude-sonnet-5-5", U())
    tracker.record("Researcher", "claude-sonnet-5-5", U())
    tracker.record("CEO", "claude-sonnet-5-5", U())
    spend = tracker.by_agent()
    assert spend["Researcher"].calls == 2
    assert spend["Researcher"].usage.input_tokens == 2000
    assert tracker.total_usd == pytest.approx(3 * (1000 * 2 + 100 * 10) / 1e6)
