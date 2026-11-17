"""Preflight check: is everything ready for a real run?

`python -m bottega check` answers that in a few seconds, before a project
spends anything:

  1. Is there an API key?
  2. For every agent: do the key, the model name, the system prompt and the
     tool definitions work? This uses the token-counting endpoint, which is
     free and generates nothing.
  3. With `--live`: one tiny real request with exactly the shape the agents
     send (streaming, effort, fallbacks, tools). It costs a fraction of a cent.

The dashboard runs the free part at startup and shows the result.
"""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from bottega.agents.roles import ROLES, build_agent
from bottega.config import Settings
from bottega.costs import CostTracker
from bottega.errors import explain, is_fatal
from bottega.sandbox import Workspace


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    fatal: bool = False  # True when nothing will work until this is fixed


def has_credentials() -> bool:
    """The SDK reads ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) by itself."""
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


def _agent_for(role: str, settings: Settings, client: Any) -> Any:
    folder = Path(tempfile.mkdtemp(prefix="bottega-check-"))
    return build_agent(role, client=client, settings=settings, workspace=Workspace(folder),
                       costs=CostTracker())


def run_checks(
    settings: Settings,
    client_factory: Callable[[], Any],
    *,
    roles: Iterable[str] = tuple(ROLES),
    live: bool = False,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    if not has_credentials():
        results.append(CheckResult(
            "API key", False,
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key.", fatal=True))
        return results
    results.append(CheckResult("API key", True, "found"))

    client = client_factory()
    hello = [{"role": "user", "content": "Hello"}]
    for role in roles:
        agent = _agent_for(role, settings, client)
        request = agent.build_request(hello)
        # Token counting takes the same prompt and tools, but not the
        # generation-only fields (max_tokens, fallbacks, streaming options).
        for key in ("max_tokens", "fallbacks"):
            request.pop(key, None)
        request["tools"] = [{k: v for k, v in t.items() if k != "eager_input_streaming"}
                            for t in request["tools"]]
        request.pop("betas", None)
        try:
            counted = client.beta.messages.count_tokens(**request)
        except Exception as exc:  # noqa: BLE001 - every failure becomes a readable result
            results.append(CheckResult(f"{agent.name} prompt and tools", False, explain(exc),
                                       fatal=is_fatal(exc)))
            if is_fatal(exc):
                return results  # a wrong key or model fails every agent the same way
            continue
        results.append(CheckResult(f"{agent.name} prompt and tools", True,
                                   f"{counted.input_tokens:,} tokens, {len(agent.tools)} tools"))

    if live:
        results.append(_live_check(settings, client))
    return results


def _live_check(settings: Settings, client: Any) -> CheckResult:
    """One real request with the exact shape the agents use, kept tiny."""
    small = replace(settings, max_tokens=512)
    agent = _agent_for("ceo", small, client)
    started = time.monotonic()
    try:
        message = agent._call_model([{
            "role": "user",
            "content": "This is a connection test. Reply with the single word OK. Do not call any tools.",
        }])
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Live request", False, explain(exc), fatal=is_fatal(exc))
    cost = agent.costs.record("check", message.model, message.usage)
    seconds = time.monotonic() - started
    if message.stop_reason == "refusal":
        return CheckResult("Live request", False, "The model declined the test request.")
    return CheckResult(
        "Live request", True,
        f"{message.model} answered in {seconds:.1f}s (stop: {message.stop_reason}), cost ${cost:.4f}")


def summarize(results: list[CheckResult]) -> dict[str, Any]:
    """A short status for the dashboard."""
    failed = [r for r in results if not r.ok]
    if not failed:
        return {"status": "ready", "message": "Claude is ready.", "blocking": False}
    first = failed[0]
    status = "missing_key" if first.name == "API key" else "error"
    return {"status": status, "message": first.detail, "blocking": first.fatal}
