"""Role registry: what makes each agent different.

A role is pure data: a prompt file and the names of the tools it may use.
Adding a new agent to the company means adding one prompt file and one entry
here — no new classes. Restricting tools per role is "least privilege":
e.g. later the Researcher won't get `update_task`, only the CEO will.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import anthropic

from bottega.agents.base import Agent
from bottega.config import Settings
from bottega.costs import CostTracker
from bottega.events import EventHandler, ignore_event
from bottega.tools import TOOL_REGISTRY, Workspace

PROMPTS_DIR = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class AgentSpec:
    name: str  # machine name, e.g. "researcher"
    title: str  # display name, e.g. "Researcher"
    tool_names: tuple[str, ...]

    def system_prompt(self) -> str:
        return (PROMPTS_DIR / f"{self.name}.md").read_text(encoding="utf-8")


ROLES: dict[str, AgentSpec] = {
    "researcher": AgentSpec(
        name="researcher",
        title="Researcher",
        tool_names=("write_file", "read_file", "list_files"),
    ),
    # Phase 2+: ceo, designer, developer, marketer, qa
}


def build_agent(
    role: str,
    *,
    client: anthropic.Anthropic,
    settings: Settings,
    workspace: Workspace,
    costs: CostTracker,
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
        on_event=on_event,
    )
