"""The team roster: who works at Bottega.

Pure data with no imports from the rest of the package, so every layer (tools,
agents, dashboard) can use it without import cycles.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Seat:
    name: str  # machine name, e.g. "developer"
    title: str  # display name, e.g. "Developer"
    description: str  # one line shown on the dashboard
    owns_tasks: bool  # can the CEO assign tasks to this seat?


TEAM: tuple[Seat, ...] = (
    Seat("ceo", "CEO", "Plans the work, assigns it, decides when it's done.", owns_tasks=False),
    Seat("researcher", "Researcher", "Studies the business, its audience and competitors.", owns_tasks=True),
    Seat("designer", "Designer", "Defines colors, typography and page structure.", owns_tasks=True),
    Seat("developer", "Developer", "Writes the HTML, CSS and JavaScript.", owns_tasks=True),
    Seat("marketer", "Marketer", "Writes the copy, the outreach email and social posts.", owns_tasks=True),
    Seat("qa", "QA", "Reviews every deliverable; approves it or sends it back.", owns_tasks=False),
)

SEATS: dict[str, Seat] = {seat.name: seat for seat in TEAM}
WORKER_ROLES: tuple[str, ...] = tuple(seat.name for seat in TEAM if seat.owns_tasks)


def title_of(role: str) -> str:
    return SEATS[role].title
