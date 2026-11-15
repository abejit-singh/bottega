"""Events: how agents report what they're doing to the outside world.

Agents never print directly. Instead they emit `Event`s to a callback. Today the
CLI's callback prints them; in phase 4 the web server's callback pushes them to
the browser over Server-Sent Events. Same agent code, different listeners —
this decoupling is the "observer pattern".
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

EventKind = Literal["agent_start", "text", "tool_call", "tool_result", "agent_done", "error"]


@dataclass(frozen=True)
class Event:
    agent: str
    kind: EventKind
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


EventHandler = Callable[[Event], None]


def ignore_event(_: Event) -> None:
    """Default handler: do nothing."""
