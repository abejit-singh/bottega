"""EventHub: carries events from agent threads to every open browser tab.

The problem it solves: agents run in ordinary *threads*, but the web server is
*async* (one event loop serving many connections). You cannot safely touch an
asyncio.Queue from another thread, so `publish()` hands each event to the loop
with `call_soon_threadsafe`, the official bridge between the two worlds.

The hub also keeps a bounded history and numbers every event. A browser that
connects late, or reconnects after a network blip, says "I have seen up to
event N" and receives everything after it. No event is lost or shown twice.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Any

Subscriber = tuple[asyncio.AbstractEventLoop, "asyncio.Queue[dict[str, Any]]"]


class EventHub:
    def __init__(self, history_size: int = 2000) -> None:
        self._lock = threading.Lock()
        self._history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self._next_id = 1
        self._subscribers: list[Subscriber] = []

    @property
    def last_id(self) -> int:
        with self._lock:
            return self._next_id - 1

    def publish(self, event: dict[str, Any]) -> dict[str, Any]:
        """Record an event and fan it out. Safe to call from any thread."""
        with self._lock:
            stamped = {**event, "id": self._next_id}
            self._next_id += 1
            self._history.append(stamped)
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, stamped)
            except RuntimeError:
                # That subscriber's event loop has shut down; forget it.
                self._remove(queue)
        return stamped

    def history(self, after_id: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._history if e["id"] > after_id]

    def subscribe(self, after_id: int = 0) -> tuple[asyncio.Queue[dict[str, Any]], list[dict[str, Any]]]:
        """Start receiving live events. Call from inside the event loop.

        Returns (queue, backlog). Taking the backlog and registering the queue
        happen under one lock, so an event published at that instant lands in
        exactly one of the two.
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        with self._lock:
            backlog = [e for e in self._history if e["id"] > after_id]
            self._subscribers.append((asyncio.get_running_loop(), queue))
        return queue, backlog

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._remove(queue)

    def _remove(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s[1] is not queue]
