"""The task board: the one place where work is planned and tracked.

Agents never call each other. The CEO adds tasks here, the orchestrator hands
each task to its owner when its dependencies are done, QA approves it or sends
it back. Everything anyone needs to know about progress is on this board.

A task moves through these states:

    todo ──start──▶ in_progress ──submit──▶ review ──approve──▶ done
      ▲                  │                    │
      │               release                 │
      └──────────────────┴── request_revision ┘        (any state) ──fail──▶ failed

Design decisions:
  * The board enforces the rules (legal moves, the revision limit), so no agent
    or tool can put it in an impossible state.
  * A task can only depend on tasks that already exist, which makes dependency
    cycles impossible by construction.
  * Several agents work at once, each in its own thread, so every method takes
    a lock, and readers get copies instead of the live objects.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

TaskStatus = Literal["todo", "in_progress", "review", "done", "failed"]

MAX_TASKS = 10  # a plan longer than this is a sign of a confused CEO (and a big bill)


class BoardError(Exception):
    """An illegal move on the board. The message is written for the agent that made it."""


@dataclass
class Task:
    id: str  # "T1", "T2", ...
    title: str
    description: str
    owner: str  # role name, e.g. "developer"
    depends_on: list[str] = field(default_factory=list)
    status: TaskStatus = "todo"
    output: str = ""  # the owner's summary of what was delivered
    revisions: int = 0  # how many times QA has sent it back
    attempts: int = 0  # how many times work on it has started
    feedback: list[str] = field(default_factory=list)  # QA's revision requests, oldest first
    review_note: str = ""  # QA's comment when approving
    error: str = ""  # why it failed, if it did

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Called after every change with (what happened, a copy of the task).
Listener = Callable[[str, Task], None]


class TaskBoard:
    def __init__(self, max_revisions: int = 2, on_change: Listener | None = None) -> None:
        self.max_revisions = max_revisions
        self._on_change = on_change
        self._lock = threading.RLock()
        self._tasks: dict[str, Task] = {}  # insertion order = creation order

    # ------------------------------------------------------------------ reads

    def get(self, task_id: str) -> Task:
        with self._lock:
            return _copy(self._require(task_id))

    def tasks(self) -> list[Task]:
        with self._lock:
            return [_copy(t) for t in self._tasks.values()]

    def ready(self, busy_owners: Iterable[str] = ()) -> list[Task]:
        """Tasks that can start now: not started, every dependency done, owner free.
        At most one task per owner, because each agent does one thing at a time."""
        busy = set(busy_owners)
        picked: list[Task] = []
        with self._lock:
            for task in self._tasks.values():
                if task.status != "todo" or task.owner in busy:
                    continue
                if all(self._tasks[dep].status == "done" for dep in task.depends_on):
                    picked.append(_copy(task))
                    busy.add(task.owner)
        return picked

    def awaiting_review(self) -> list[Task]:
        with self._lock:
            return [_copy(t) for t in self._tasks.values() if t.status == "review"]

    def is_settled(self) -> bool:
        """True when nothing is left to do: every task is done or failed."""
        with self._lock:
            return all(t.status in ("done", "failed") for t in self._tasks.values())

    def can_revise(self, task_id: str) -> bool:
        with self._lock:
            return self._require(task_id).revisions < self.max_revisions

    def render(self) -> str:
        """The board as text, for agents' prompts."""
        with self._lock:
            if not self._tasks:
                return "(the board is empty)"
            lines = []
            for t in self._tasks.values():
                deps = f" (after {', '.join(t.depends_on)})" if t.depends_on else ""
                lines.append(f"- {t.id} [{t.status}] {t.title} — owner: {t.owner}{deps}")
                if t.output:
                    lines.append(f"    delivered: {t.output}")
                if t.review_note:
                    lines.append(f"    QA note: {t.review_note}")
                if t.error:
                    lines.append(f"    failed: {t.error}")
            return "\n".join(lines)

    # ------------------------------------------------------------------ moves

    def add(self, title: str, description: str, owner: str,
            depends_on: Iterable[str] = ()) -> Task:
        deps = list(dict.fromkeys(depends_on))  # drop duplicates, keep order
        with self._lock:
            if len(self._tasks) >= MAX_TASKS:
                raise BoardError(f"The board is full ({MAX_TASKS} tasks). Work with the tasks you have.")
            unknown = [d for d in deps if d not in self._tasks]
            if unknown:
                raise BoardError(
                    f"Unknown dependency {', '.join(unknown)}. A task can only depend on tasks "
                    f"that already exist ({', '.join(self._tasks) or 'none yet'}): create those first."
                )
            task = Task(id=f"T{len(self._tasks) + 1}", title=title.strip(),
                        description=description.strip(), owner=owner, depends_on=deps)
            self._tasks[task.id] = task
            snapshot = _copy(task)
        self._notify("created", snapshot)
        return snapshot

    def start(self, task_id: str) -> Task:
        return self._move(task_id, "started", ("todo",), status="in_progress", count_attempt=True)

    def submit(self, task_id: str, output: str) -> Task:
        """The owner says the work is finished: it now waits for QA."""
        return self._move(task_id, "submitted", ("in_progress",), status="review", output=output)

    def approve(self, task_id: str, note: str = "") -> Task:
        return self._move(task_id, "approved", ("review",), status="done", review_note=note)

    def request_revision(self, task_id: str, feedback: str) -> Task:
        """QA sends the task back to its owner, up to `max_revisions` times."""
        with self._lock:
            task = self._require(task_id)
            if task.status != "review":
                raise BoardError(f"{task_id} is not waiting for review (it is {task.status}).")
            if task.revisions >= self.max_revisions:
                raise BoardError(
                    f"{task_id} has already been revised {task.revisions} times, which is the "
                    "limit. Approve it and list the remaining issues in your feedback."
                )
            task.status = "todo"
            task.revisions += 1
            task.feedback.append(feedback.strip())
            snapshot = _copy(task)
        self._notify("revision", snapshot)
        return snapshot

    def release(self, task_id: str) -> Task:
        """Put an interrupted task back in the queue (stop button, retry)."""
        return self._move(task_id, "released", ("in_progress",), status="todo")

    def fail(self, task_id: str, error: str) -> Task:
        return self._move(task_id, "failed", ("todo", "in_progress", "review"),
                          status="failed", error=error)

    def fail_blocked(self) -> list[Task]:
        """Fail tasks that can never start because something they depend on failed.
        Repeats until stable, so a failure propagates down a chain of dependencies."""
        failed: list[Task] = []
        while True:
            with self._lock:
                blocked = next(
                    (t for t in self._tasks.values() if t.status == "todo"
                     and any(self._tasks[d].status == "failed" for d in t.depends_on)), None)
                if blocked is None:
                    return failed
                culprit = next(d for d in blocked.depends_on if self._tasks[d].status == "failed")
            failed.append(self.fail(blocked.id, f"Blocked: it depends on {culprit}, which failed."))

    # ------------------------------------------------------------- persistence

    def to_list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [t.to_dict() for t in self._tasks.values()]

    def load(self, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            self._tasks = {row["id"]: Task(**row) for row in rows}

    # --------------------------------------------------------------- internals

    def _require(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise BoardError(f"There is no task {task_id!r} on the board.")
        return task

    def _move(self, task_id: str, change: str, allowed_from: tuple[str, ...], *,
              count_attempt: bool = False, **fields: Any) -> Task:
        with self._lock:
            task = self._require(task_id)
            if task.status not in allowed_from:
                raise BoardError(f"{task_id} is {task.status}; it cannot be {change} now.")
            for name, value in fields.items():
                setattr(task, name, value)
            if count_attempt:
                task.attempts += 1
            snapshot = _copy(task)
        self._notify(change, snapshot)
        return snapshot

    def _notify(self, change: str, task: Task) -> None:
        # Called outside the lock, so a slow listener can't block other agents.
        if self._on_change is not None:
            self._on_change(change, task)


def _copy(task: Task) -> Task:
    return replace(task, depends_on=list(task.depends_on), feedback=list(task.feedback))
