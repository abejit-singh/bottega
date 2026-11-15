"""The Company: the long-lived object that owns projects and puts agents to work.

The CLI runs one agent and exits. The dashboard needs something that stays
alive: it accepts goals, runs each one in the background, remembers the
results and reports progress. That is this class.

Design decisions:
  * No web code here. The Company reports progress by calling a `publish`
    function it is given. The web server passes in its event hub; tests pass in
    `list.append`. Keeping the core free of FastAPI makes it easy to test.
  * Each project runs in a background *thread*. The agent loop is blocking
    (it waits on the network), so running it on the web server's own thread
    would freeze the dashboard for everyone.
  * One project at a time. With a single agent, two simultaneous projects would
    just fight over the same seat. Phase 2's orchestrator will schedule several
    agents properly.
  * Shared state is guarded by a lock, because the worker thread writes project
    results while web requests read them.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic

from bottega.agents import ROLES, AgentError, build_agent
from bottega.agents.roles import roster
from bottega.config import Settings
from bottega.costs import AgentSpend, CostTracker
from bottega.errors import explain
from bottega.events import Event
from bottega.memory import MemoryVault
from bottega.sandbox import Workspace, new_project_id

logger = logging.getLogger(__name__)

ProjectStatus = Literal["running", "done", "incomplete", "stopped", "over_budget", "failed"]

# How an agent's stop_reason maps to a project status. Anything else
# (max_turns, max_tokens) counts as "incomplete".
_STATUS_BY_STOP_REASON: dict[str, ProjectStatus] = {
    "end_turn": "done",
    "cancelled": "stopped",
    "budget_exceeded": "over_budget",
}

Publish = Callable[[dict[str, Any]], Any]


class CompanyBusy(Exception):
    """Raised when a project is started while another one is still running."""


@dataclass
class Project:
    id: str
    goal: str
    role: str
    agent: str  # display name of the agent doing the work
    workspace: Workspace
    started_at: float
    status: ProjectStatus = "running"
    finished_at: float | None = None
    summary: str = ""
    error: str | None = None
    new_notes: list[str] = field(default_factory=list)
    costs: CostTracker = field(default_factory=CostTracker)
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly view for the dashboard."""
        return {
            "id": self.id,
            "goal": self.goal,
            "role": self.role,
            "agent": self.agent,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": self.summary,
            "error": self.error,
            "cost_usd": self.costs.total_usd,
            "files": self.workspace.list_files(),
            "new_notes": list(self.new_notes),
        }


class Company:
    def __init__(
        self,
        settings: Settings,
        *,
        publish: Publish = lambda event: None,
        client_factory: Callable[[], Any] = anthropic.Anthropic,
        demo: bool = False,
    ) -> None:
        self.settings = settings
        self.memory = MemoryVault(settings.memory_dir)
        self.demo = demo
        self._publish = publish
        self._client_factory = client_factory
        self._lock = threading.Lock()
        self._projects: dict[str, Project] = {}  # dicts keep insertion order: oldest first

    # ---------------------------------------------------------------- commands

    def start_project(self, goal: str, role: str = "researcher") -> Project:
        """Start working on `goal` in the background and return immediately."""
        goal = goal.strip()
        if not goal:
            raise ValueError("The goal must not be empty.")
        if role not in ROLES:
            raise ValueError(f"Unknown role {role!r}. Available: {', '.join(ROLES)}")

        with self._lock:
            if any(p.status == "running" for p in self._projects.values()):
                raise CompanyBusy("The company is already working on a project.")
            project_id = self._unique_id(goal)
            project = Project(
                id=project_id,
                goal=goal,
                role=role,
                agent=ROLES[role].title,
                workspace=Workspace(self.settings.output_dir / project_id),
                started_at=time.time(),
            )
            self._projects[project_id] = project

        self._publish_system(project, "project_start", goal=goal, agent=project.agent)
        # daemon=True: the thread won't keep the program alive after Ctrl+C.
        project.thread = threading.Thread(
            target=self._run, args=(project,), name=f"project-{project_id}", daemon=True
        )
        project.thread.start()
        return project

    def stop_project(self, project_id: str) -> bool:
        """Ask a running project to stop. Returns False if there is nothing to stop."""
        project = self.get_project(project_id)
        if project is None or project.status != "running":
            return False
        project.cancel.set()  # the agent checks this flag before its next model call
        self._publish_system(project, "stop_requested")
        return True

    # ----------------------------------------------------------------- queries

    def get_project(self, project_id: str) -> Project | None:
        with self._lock:
            return self._projects.get(project_id)

    def wait(self, project_id: str, timeout: float | None = None) -> Project:
        """Block until a project finishes (used by tests)."""
        project = self.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        if project.thread is not None:
            project.thread.join(timeout)
        return project

    def snapshot(self) -> dict[str, Any]:
        """Everything the dashboard needs to draw itself, as plain JSON data."""
        with self._lock:
            projects = list(self._projects.values())
            running = next((p for p in projects if p.status == "running"), None)

            # Add up what each agent has spent across all projects.
            spend: dict[str, AgentSpend] = {}
            for project in projects:
                for agent, s in project.costs.by_agent().items():
                    total = spend.setdefault(agent, AgentSpend())
                    total.calls += s.calls
                    total.usage += s.usage
                    total.cost_usd += s.cost_usd

            agents = []
            for seat in roster():
                s = spend.get(seat.title, AgentSpend())
                if not seat.available:
                    status = "offline"
                elif running is not None and running.agent == seat.title:
                    status = "working"
                else:
                    status = "idle"
                agents.append({
                    "name": seat.name,
                    "title": seat.title,
                    "description": seat.description,
                    "available": seat.available,
                    "note": seat.note,
                    "status": status,
                    "calls": s.calls,
                    "input_tokens": s.usage.input_tokens + s.usage.cache_read_tokens
                                    + s.usage.cache_write_tokens,
                    "output_tokens": s.usage.output_tokens,
                    "cost_usd": s.cost_usd,
                })

            return {
                "demo": self.demo,
                "busy": running is not None,
                "settings": {
                    "model": self.settings.model,
                    "effort": self.settings.effort,
                    "max_cost_usd": self.settings.max_cost_usd,
                },
                "agents": agents,
                "projects": [p.to_dict() for p in reversed(projects)],  # newest first
                "totals": {
                    "cost_usd": sum(a["cost_usd"] for a in agents),
                    "calls": sum(a["calls"] for a in agents),
                    "input_tokens": sum(a["input_tokens"] for a in agents),
                    "output_tokens": sum(a["output_tokens"] for a in agents),
                },
                "memory_notes": len(self.memory.list_notes()),
            }

    # ---------------------------------------------------------------- internals

    def _unique_id(self, goal: str) -> str:
        base = candidate = new_project_id(goal)
        n = 2
        while candidate in self._projects or (self.settings.output_dir / candidate).exists():
            candidate = f"{base}-{n}"
            n += 1
        return candidate

    def _run(self, project: Project) -> None:
        """Worker thread: run the agent and record the outcome. Must never raise."""
        notes_before = set(self.memory.list_notes())
        status: ProjectStatus
        summary, error = "", None
        try:
            agent = build_agent(
                project.role,
                client=self._client_factory(),
                settings=self.settings,
                workspace=project.workspace,
                costs=project.costs,
                memory=self.memory,
                cancel=project.cancel,
                on_event=lambda event: self._publish_agent_event(project, event),
            )
            result = agent.run(f"Client goal: {project.goal}")
            status = _STATUS_BY_STOP_REASON.get(result.stop_reason, "incomplete")
            summary = result.final_text
        except Exception as exc:  # noqa: BLE001 - a crash here must become a visible failure
            if not isinstance(exc, (AgentError, anthropic.AnthropicError)):
                logger.exception("Project %s crashed", project.id)
            status, error = "failed", explain(exc)

        with self._lock:
            project.status = status
            project.summary = summary
            project.error = error
            project.finished_at = time.time()
            project.new_notes = sorted(set(self.memory.list_notes()) - notes_before)
        self._publish_system(
            project, "project_done",
            status=status, error=error, cost_usd=project.costs.total_usd,
            files=len(project.workspace.list_files()),
        )

    def _publish_agent_event(self, project: Project, event: Event) -> None:
        self._publish({
            "project_id": project.id,
            "agent": event.agent,
            "kind": event.kind,
            "data": event.data,
            "ts": event.timestamp,
        })

    def _publish_system(self, project: Project, kind: str, **data: Any) -> None:
        self._publish({
            "project_id": project.id,
            "agent": "Bottega",
            "kind": kind,
            "data": data,
            "ts": time.time(),
        })
