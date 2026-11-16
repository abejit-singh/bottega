"""ProjectState: everything the team shares while working on one project."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from bottega.project.board import TaskBoard
from bottega.project.brief import ProjectBrief
from bottega.project.messages import MessageLog
from bottega.sandbox import INTERNAL_DIR, Workspace

STATE_FILE = f"{INTERNAL_DIR}/state.json"

# Which step of the project we are in. Tools use it to refuse actions that make
# no sense yet (e.g. the CEO declaring the project finished while planning).
Phase = Literal["planning", "working", "wrap_up", "closed"]


@dataclass
class ProjectState:
    board: TaskBoard
    messages: MessageLog
    brief: ProjectBrief
    phase: Phase = "planning"
    follow_ups_left: int = 1  # how many times the CEO may still add tasks after the first plan
    finished: bool = False  # set when the CEO calls finish_project
    final_summary: str = ""

    def save(self, workspace: Workspace) -> None:
        """Write the board and messages to disk, so a finished project can be
        shown again after the dashboard restarts."""
        data = {
            "tasks": self.board.to_list(),
            "messages": self.messages.to_list(),
            "finished": self.finished,
            "final_summary": self.final_summary,
        }
        workspace.write_text(STATE_FILE, json.dumps(data, indent=2, ensure_ascii=False), internal=True)

    @classmethod
    def load(cls, workspace: Workspace, max_revisions: int = 2) -> ProjectState:
        data = json.loads(workspace.read_text(STATE_FILE))
        state = cls(board=TaskBoard(max_revisions), messages=MessageLog(),
                    brief=ProjectBrief.load(workspace), phase="closed")
        state.board.load(data.get("tasks", []))
        state.messages.load(data.get("messages", []))
        state.finished = bool(data.get("finished"))
        state.final_summary = data.get("final_summary", "")
        return state
