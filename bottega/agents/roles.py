"""Role registry: what makes each agent different.

A role is pure data: a prompt file and the names of the tools it may use.
Adding a new agent to the company means adding one prompt file and one entry
here — no new classes. Restricting tools per role is "least privilege":
e.g. later the Researcher won't get `update_task`, only the CEO will.
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
from bottega.tools import TOOL_REGISTRY, Workspace

PROMPTS_DIR = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class AgentSpec:
    name: str  # machine name, e.g. "researcher"
    title: str  # display name, e.g. "Researcher"
    description: str  # one line shown on the dashboard
    tool_names: tuple[str, ...]

    def system_prompt(self) -> str:
        return (PROMPTS_DIR / f"{self.name}.md").read_text(encoding="utf-8")


# Agents that exist today and can be given work.
ROLES: dict[str, AgentSpec] = {
    "researcher": AgentSpec(
        name="researcher",
        title="Researcher",
        description="Analyzes the business, its audience and competitors.",
        tool_names=(
            "write_file", "read_file", "list_files",
            "search_memory", "read_memory", "save_memory",
        ),
    ),
}


@dataclass(frozen=True)
class RosterEntry:
    """One seat in the company, whether or not the agent is built yet."""

    name: str
    title: str
    description: str
    available: bool
    note: str = ""  # shown when the seat is still empty


# The full team in display order. Seats not in ROLES yet are shown as "offline"
# on the dashboard, so the screen is honest about what really works today.
_PLANNED: tuple[tuple[str, str, str, str], ...] = (
    ("ceo", "CEO", "Breaks the goal into tasks, assigns them, decides when it's done.", "Joins in phase 2"),
    ("researcher", "Researcher", "", ""),
    ("designer", "Designer", "Defines style, colors, typography and page structure.", "Joins in phase 2"),
    ("developer", "Developer", "Writes the actual HTML, CSS and JavaScript.", "Joins in phase 2"),
    ("marketer", "Marketer", "Writes copy, an outreach email and social posts.", "Joins in phase 3"),
    ("qa", "QA", "Reviews deliverables, approves or sends them back.", "Joins in phase 3"),
)


def roster() -> list[RosterEntry]:
    entries = []
    for name, title, description, note in _PLANNED:
        spec = ROLES.get(name)
        if spec is not None:
            entries.append(RosterEntry(name, spec.title, spec.description, available=True))
        else:
            entries.append(RosterEntry(name, title, description, available=False, note=note))
    return entries


def build_agent(
    role: str,
    *,
    client: anthropic.Anthropic,
    settings: Settings,
    workspace: Workspace,
    costs: CostTracker,
    memory: MemoryVault | None = None,
    cancel: threading.Event | None = None,
    on_event: EventHandler = ignore_event,
) -> Agent:
    try:
        spec = ROLES[role]
    except KeyError:
        raise ValueError(f"Unknown role {role!r}. Available: {', '.join(ROLES)}") from None
    return Agent(
        name=spec.title,
        system_prompt=spec.system_prompt(),
        tools=[TOOL_REGISTRY[n] for n in spec.tool_names],
        client=client,
        settings=settings,
        workspace=workspace,
        costs=costs,
        memory=memory,
        cancel=cancel,
        on_event=on_event,
    )
