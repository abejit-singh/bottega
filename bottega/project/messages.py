"""The message log: how agents (and you, the founder) talk to each other.

Messages are not delivered by one agent calling another. They are written to
this shared log, and each agent is shown the messages meant for it the next
time it picks up a task. That keeps agents independent of each other and gives
the dashboard a complete record of who said what.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

EVERYONE = "all"
MAX_MESSAGE_CHARS = 1500


@dataclass(frozen=True)
class Message:
    id: int
    sender: str  # display name: "CEO", "Developer", "Founder"
    to: str  # a display name, or "all"
    text: str
    ts: float


class MessageLog:
    def __init__(self, on_post: Callable[[Message], None] | None = None) -> None:
        self._on_post = on_post
        self._lock = threading.Lock()
        self._messages: list[Message] = []

    def post(self, sender: str, to: str, text: str) -> Message:
        text = text.strip()
        if not text:
            raise ValueError("A message must not be empty.")
        with self._lock:
            message = Message(len(self._messages) + 1, sender, to, text[:MAX_MESSAGE_CHARS], time.time())
            self._messages.append(message)
        if self._on_post is not None:
            self._on_post(message)
        return message

    def all(self) -> list[Message]:
        with self._lock:
            return list(self._messages)

    def for_recipient(self, name: str) -> list[Message]:
        """What `name` should read: messages to everyone or to them, not their own."""
        return [m for m in self.all() if m.sender != name and m.to in (EVERYONE, name)]

    def render(self, name: str, limit: int = 12) -> str:
        """Recent messages for `name` as text, for agents' prompts."""
        recent = self.for_recipient(name)[-limit:]
        if not recent:
            return "(no messages)"
        return "\n".join(
            f"- {m.sender} to {'everyone' if m.to == EVERYONE else m.to}: {m.text}" for m in recent
        )

    def to_list(self) -> list[dict[str, Any]]:
        return [asdict(m) for m in self.all()]

    def load(self, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            self._messages = [Message(**row) for row in rows]
