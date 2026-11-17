"""The Company: the long-lived object that owns projects and puts the team to work.

The orchestrator runs one project. The Company is what stays alive around it:
it accepts goals, runs each project in the background, remembers the results,
answers the dashboard's questions and reloads past projects from disk.

Design decisions:
  * No web code here. The Company reports progress by calling a `publish`
    function it is given. The web server passes in its event hub; the CLI
    passes a function that prints; tests pass `list.append`.
  * Each project runs in a background *thread*, so the dashboard stays
    responsive while agents wait for the API.
  * One project at a time: the six agents are one team, and the budget and the
    Stop button are per project.
  * Shared state is guarded by a lock, because worker threads write results
    while web requests read them.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic

from bottega.agents.base import AgentError
from bottega.config import Settings
from bottega.costs import AgentSpend, CostTracker
from bottega.errors import explain
from bottega.memory import MemoryVault
from bottega.orchestrator import Orchestrator, OrchestratorError
from bottega.project import BRIEF_FILE, ProjectState
from bottega.sandbox import INTERNAL_DIR, SandboxError, Workspace, new_project_id
from bottega.team import TEAM

logger = logging.getLogger(__name__)

ProjectStatus = Literal["running", "done", "incomplete", "stopped", "over_budget", "failed"]

META_FILE = f"{INTERNAL_DIR}/project.json"

Publish = Callable[[dict[str, Any]], Any]


class CompanyBusy(Exception):
    """Raised when a project is started while another one is still running."""


@dataclass
class Project:
    id: str
    goal: str
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
    orchestrator: Orchestrator | None = None  # only while the project is live
    state: ProjectState | None = None  # board, messages, brief

    def to_dict(self) -> dict[str, Any]:
        """Short JSON-friendly view, for the list of projects."""
        tasks = self.state.board.tasks() if self.state else []
        return {
            "id": self.id,
            "goal": self.goal,
            "status": self.status,
            "phase": self.state.phase if self.state else "closed",
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cost_usd": self.costs.total_usd,
            "tasks_done": sum(1 for t in tasks if t.status == "done"),
            "tasks_total": len(tasks),
        }

    def detail(self) -> dict[str, Any]:
        """Everything about one project, for the dashboard's main view."""
        state = self.state
        return {
            **self.to_dict(),
            "summary": self.summary,
            "error": self.error,
            "files": self.workspace.list_files(),
            "new_notes": list(self.new_notes),
            "costs": self.costs.to_dict(),  # spend per agent, for this project
            "tasks": state.board.to_list() if state else [],
            "messages": state.messages.to_list() if state else [],
            "brief": state.brief.to_dict() if state else {},
        }


class Company:
    def __init__(
        self,
        settings: Settings,
        *,
        publish: Publish = lambda event: None,
        client_factory: Callable[[], Any] | None = None,
        demo: bool = False,
    ) -> None:
        self.settings = settings
        self.memory = MemoryVault(settings.memory_dir)
        self.demo = demo
        self._publish = publish
        # max_retries: the SDK retries rate limits and server errors by itself.
        self._client_factory = client_factory or (
            lambda: anthropic.Anthropic(max_retries=settings.api_retries)
        )
        self._lock = threading.Lock()
        self._projects: dict[str, Project] = {}  # dicts keep insertion order: oldest first
        self.connection: dict[str, Any]
        # Is Claude reachable? Filled in by check_connection(); demo needs no check.
        if demo:
            self.connection = {"status": "demo", "message": "Demo mode: scripted answers.", "blocking": False}
        else:
            self.connection = {"status": "checking", "message": "Checking the connection…", "blocking": False}
        self._load_history()

    # ---------------------------------------------------------------- commands

    def start_project(self, goal: str) -> Project:
        """Give the team a goal. Returns immediately; the work happens in the background."""
        goal = goal.strip()
        if not goal:
            raise ValueError("The goal must not be empty.")
        if self.connection.get("blocking"):
            raise ValueError(f"Claude is not reachable: {self.connection['message']}")

        with self._lock:
            if any(p.status == "running" for p in self._projects.values()):
                raise CompanyBusy("The team is already working on a project.")
            project_id = self._unique_id(goal)
            project = Project(
                id=project_id,
                goal=goal,
                # The brief is reserved: agents change it with update_brief, not write_file.
                workspace=Workspace(self.settings.output_dir / project_id, reserved=(BRIEF_FILE,)),
                started_at=time.time(),
            )
            self._projects[project_id] = project

        self._emit(project, "Bottega", "project_start", {"goal": goal})
        # daemon=True: the thread won't keep the program alive after Ctrl+C.
        project.thread = threading.Thread(
            target=self._run, args=(project,), name=f"project-{project_id}", daemon=True
        )
        project.thread.start()
        return project

    def check_connection(self) -> None:
        """Run the free preflight check (see check.py) and publish the result.
        Called once in the background when the dashboard starts."""
        from bottega.check import run_checks, summarize  # imported here: check imports agents

        results = run_checks(self.settings, self._client_factory, roles=("ceo",))
        self.connection = summarize(results)
        self._publish({"project_id": None, "agent": "Bottega", "kind": "connection",
                       "data": dict(self.connection), "ts": time.time()})

    def stop_project(self, project_id: str) -> bool:
        """Ask a running project to stop. Returns False if there is nothing to stop."""
        project = self.get_project(project_id)
        if project is None or project.status != "running":
            return False
        project.cancel.set()  # every agent checks this flag before its next model call
        self._emit(project, "Bottega", "stop_requested", {})
        return True

    def post_message(self, project_id: str, text: str) -> None:
        """A message from you to the team. Agents read it when they start their next task."""
        project = self.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        if project.status != "running" or project.orchestrator is None:
            raise ValueError("You can only message the team while a project is running.")
        project.orchestrator.post_founder_message(text)

    # ----------------------------------------------------------------- queries

    def get_project(self, project_id: str) -> Project | None:
        with self._lock:
            return self._projects.get(project_id)

    def wait(self, project_id: str, timeout: float | None = None) -> Project:
        """Block until a project finishes (used by the CLI and the tests)."""
        project = self.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        if project.thread is not None:
            project.thread.join(timeout)
        return project

    def snapshot(self) -> dict[str, Any]:
        """The state of the whole company, as plain JSON data."""
        with self._lock:
            projects = list(self._projects.values())
        running = next((p for p in projects if p.status == "running"), None)
        activity = running.orchestrator.activity() if running and running.orchestrator else {}

        # Add up what each agent has spent across all projects.
        spend: dict[str, AgentSpend] = {}
        for project in projects:
            for agent, s in project.costs.by_agent().items():
                total = spend.setdefault(agent, AgentSpend())
                total.calls += s.calls
                total.usage += s.usage
                total.cost_usd += s.cost_usd

        agents = []
        for seat in TEAM:
            s = spend.get(seat.title, AgentSpend())
            doing = activity.get(seat.name)
            agents.append({
                "name": seat.name,
                "title": seat.title,
                "description": seat.description,
                "status": "working" if doing else "idle",
                "doing": doing["doing"] if doing else None,
                "task_id": doing["task_id"] if doing else None,
                "task_title": doing["task_title"] if doing else None,
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
                "max_revisions": self.settings.max_revisions,
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
            "connection": dict(self.connection),
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
        """Worker thread: run the orchestrator and record the outcome. Must never raise."""
        notes_before = set(self.memory.list_notes())
        status: ProjectStatus
        summary, error = "", None
        try:
            orchestrator = Orchestrator(
                project_id=project.id,
                goal=project.goal,
                workspace=project.workspace,
                settings=self.settings,
                client=self._client_factory(),
                costs=project.costs,
                memory=self.memory,
                cancel=project.cancel,
                emit=lambda agent, kind, data: self._emit(project, agent, kind, data),
            )
            with self._lock:
                project.orchestrator = orchestrator
                project.state = orchestrator.state
            self._save_meta(project)
            outcome = orchestrator.run()
            status, summary = outcome.status, outcome.summary
        except Exception as exc:  # noqa: BLE001 - a crash here must become a visible failure
            expected = (AgentError, OrchestratorError, anthropic.AnthropicError)
            if not isinstance(exc, expected):
                logger.exception("Project %s crashed", project.id)
            status, error = "failed", explain(exc)

        with self._lock:
            project.status = status
            project.summary = summary
            project.error = error
            project.finished_at = time.time()
            project.new_notes = sorted(set(self.memory.list_notes()) - notes_before)
            project.orchestrator = None
        self._save_meta(project)
        self._emit(project, "Bottega", "project_done", {
            "status": status, "error": error, "cost_usd": project.costs.total_usd,
            "files": len(project.workspace.list_files()),
        })

    def _emit(self, project: Project, agent: str, kind: str, data: dict[str, Any]) -> None:
        self._publish({
            "project_id": project.id, "agent": agent, "kind": kind, "data": data, "ts": time.time(),
        })

    # ------------------------------------------------------- history on disk

    def _save_meta(self, project: Project) -> None:
        """Remember the project's status and cost in its own folder."""
        meta = {
            "id": project.id,
            "goal": project.goal,
            "status": project.status,
            "started_at": project.started_at,
            "finished_at": project.finished_at,
            "summary": project.summary,
            "error": project.error,
            "new_notes": project.new_notes,
            "costs": project.costs.to_dict(),
        }
        try:
            project.workspace.write_text(META_FILE, json.dumps(meta, indent=2), internal=True)
        except (OSError, SandboxError):
            logger.exception("Could not save project %s", project.id)

    def _load_history(self) -> None:
        """Load projects from earlier sessions, so a restart doesn't forget them."""
        if not self.settings.output_dir.is_dir():
            return
        loaded = []
        for folder in self.settings.output_dir.iterdir():
            try:
                workspace = Workspace(folder, reserved=(BRIEF_FILE,))
                meta = json.loads(workspace.read_text(META_FILE))
                project = Project(
                    id=meta["id"], goal=meta["goal"], workspace=workspace,
                    started_at=meta["started_at"], finished_at=meta.get("finished_at"),
                    summary=meta.get("summary", ""), error=meta.get("error"),
                    new_notes=meta.get("new_notes", []),
                    costs=CostTracker.from_dict(meta.get("costs", {})),
                    # "running" on disk means the program was closed mid-project.
                    status="incomplete" if meta["status"] == "running" else meta["status"],
                )
                if meta["status"] == "running":
                    project.error = "Interrupted: Bottega was closed while this project was running."
                # If this fails, the project stopped before any state was saved.
                with contextlib.suppress(SandboxError, ValueError, KeyError):
                    project.state = ProjectState.load(workspace, self.settings.max_revisions)
                loaded.append(project)
            except (SandboxError, ValueError, KeyError, TypeError, OSError):
                continue  # not a Bottega project folder (or a damaged one): skip it
        for project in sorted(loaded, key=lambda p: p.started_at):
            self._projects[project.id] = project
