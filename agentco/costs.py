"""Token accounting and cost tracking, per agent.

Every API response carries a `usage` object with token counts. We convert
those to dollars with a pricing table and accumulate them per agent, so the
CLI/dashboard can show "the Developer spent $0.12" and phase 5 can stop a run
that goes over budget.

Prices are USD per 1 million tokens. Prompt caching has its own prices:
  * cache *write* (first time a prefix is cached) costs 1.25x normal input,
  * cache *read* (reusing it) is much cheaper than normal input.
Keep this table in sync with https://www.anthropic.com/pricing.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

CACHE_WRITE_MULTIPLIER = 1.25


@dataclass(frozen=True)
class ModelPrice:
    input: float  # $ per 1M input tokens
    output: float  # $ per 1M output tokens
    cache_read: float  # $ per 1M cached input tokens read


PRICING: dict[str, ModelPrice] = {
    "claude-sonnet-5-5": ModelPrice(input=2.00, output=10.00, cache_read=0.10),
    "claude-sonnet-5": ModelPrice(input=2.00, output=10.00, cache_read=0.20),
    "claude-opus-5-5": ModelPrice(input=4.00, output=20.00, cache_read=0.20),
    "claude-opus-5": ModelPrice(input=5.00, output=25.00, cache_read=0.50),
    "claude-haiku-5-5": ModelPrice(input=0.10, output=0.50, cache_read=0.01),
}

# If we meet a model we don't know (e.g. a fallback model), we price it at the
# most expensive known rate. Over-estimating is the safe choice for a budget limit.
_CONSERVATIVE_PRICE = max(PRICING.values(), key=lambda p: p.output)


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    @classmethod
    def from_api(cls, usage: Any) -> TokenUsage:
        """Convert the SDK's usage object. `or 0` handles fields that may be None."""
        return cls(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )

    def __iadd__(self, other: TokenUsage) -> TokenUsage:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.cache_read_tokens += other.cache_read_tokens
        return self


def price_for(model: str) -> ModelPrice:
    price = PRICING.get(model)
    if price is None:
        logger.warning("No price for model %r; using conservative estimate", model)
        return _CONSERVATIVE_PRICE
    return price


def cost_of(model: str, usage: TokenUsage) -> float:
    """Dollar cost of one API call."""
    p = price_for(model)
    return (
        usage.input_tokens * p.input
        + usage.output_tokens * p.output
        + usage.cache_write_tokens * p.input * CACHE_WRITE_MULTIPLIER
        + usage.cache_read_tokens * p.cache_read
    ) / 1_000_000


@dataclass
class AgentSpend:
    calls: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: float = 0.0


class CostTracker:
    """Accumulates spend per agent. Thread-safe, because in later phases
    several agents may run at the same time while the dashboard reads totals."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_agent: dict[str, AgentSpend] = {}

    def record(self, agent: str, model: str, api_usage: Any) -> float:
        """Record one API call and return its cost in USD.

        We price by `response.model` (passed in as `model`) rather than the
        configured model: if a refusal fallback kicked in, a different model
        actually served the request.
        """
        usage = TokenUsage.from_api(api_usage)
        cost = cost_of(model, usage)
        with self._lock:
            spend = self._by_agent.setdefault(agent, AgentSpend())
            spend.calls += 1
            spend.usage += usage
            spend.cost_usd += cost
        return cost

    def by_agent(self) -> dict[str, AgentSpend]:
        with self._lock:
            # Return copies so callers can't mutate our internal state.
            return {
                name: AgentSpend(s.calls, TokenUsage(**vars(s.usage)), s.cost_usd)
                for name, s in self._by_agent.items()
            }

    @property
    def total_usd(self) -> float:
        with self._lock:
            return sum(s.cost_usd for s in self._by_agent.values())
