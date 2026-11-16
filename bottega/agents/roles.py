"""Role registry: what makes each agent different.

A role is pure data: a prompt file and the names of the tools it may use.
Adding an agent to the company means adding a seat in team.py, a prompt file
and an entry here — no new classes.

The tool lists are "least privilege" in action. Only the CEO can create tasks
or close the project, only QA can pass a verdict, and QA cannot write files:
a reviewer that could quietly fix the work would hide problems instead of
reporting them.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import anthropic

from bottega.agents.base import Agent
from bottega.config import Settings
from bottega.costs import CostTracker
from bottega.events import EventHandler, ignore_event
from bottega.memory import MemoryVault
from bottega.project import ProjectState
from bottega.team import SEATS, TEAM
from bottega.tools import BOARD_TOOL_NAMES, TOOL_REGISTRY, Workspace

PROMPTS_DIR = Path(__file__).parent / "prompts"

_READ = ("read_file", "list_files", "search_memory", "read_memory")

_TOOLS_BY_ROLE: dict[str, tuple[str, ...]] = {
    "ceo": ("create_task", "update_brief", "post_message", "finish_project", *_READ),
    "researcher": ("write_file", *_READ, "save_memory", "update_brief", "post_message", "update_task"),
    "designer": ("write_file", *_READ, "update_brief", "post_message", "update_task"),
    "developer": ("write_file", *_READ, "post_message", "update_task"),
    "marketer": ("write_file", *_READ, "post_message", "update_task"),
    "qa": (*_READ, "save_memory", "post_message", "review_task"),
}


@dataclass(frozen=True)
class AgentSpec:
    name: str  # machine name, e.g. "researcher"
    title: str  # display name, e.g. "Researcher"
    description: str
    tool_names: tuple[str, ...]

    def system_prompt(self) -> str:
        return (PROMPTS_DIR / f"{self.name}.md").read_text(encoding="utf-8")


ROLES: dict[str, AgentSpec] = {
    seat.name: AgentSpec(seat.name, seat.title, seat.description, _TOOLS_BY_ROLE[seat.name])
    for seat in TEAM
}


def build_agent(
    role: str,
    *,
    client: anthropic.Anthropic,
    settings: Settings,
    workspace: Workspace,
    costs: CostTracker,
    memory: MemoryVault | None = None,
    project: ProjectState | None = None,
    cancel: threading.Event | None = None,
    on_event: EventHandler = ignore_event,
) -> Agent:
    """Create the agent for `role`. Without a `project` (solo run) the agent
    gets no board tools, because there is no board to use them on."""
    if role not in SEATS:
        raise ValueError(f"Unknown role {role!r}. Available: {', '.join(SEATS)}")
    spec = ROLES[role]
    tool_names = [
        name for name in spec.tool_names if project is not None or name not in BOARD_TOOL_NAMES
    ]
    return Agent(
        name=spec.title,
        role=spec.name,
        system_prompt=spec.system_prompt(),
        tools=[TOOL_REGISTRY[name] for name in tool_names],
        client=client,
        settings=settings,
        workspace=workspace,
        costs=costs,
        memory=memory,
        project=project,
        cancel=cancel,
        on_event=on_event,
    )
