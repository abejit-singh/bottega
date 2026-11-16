"""The project brief: the team's shared memory for one project.

`project_brief.json` lives in the project folder. It holds the client's goal
plus short notes the agents agree on (who the audience is, the tone of voice,
the color palette...). Every agent sees the brief at the top of every task, so
a decision made once does not have to be rediscovered by everybody else.

Agents change it only through the `update_brief` tool: `write_file` refuses to
overwrite it, so a careless agent can't wipe the team's shared decisions.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from bottega.sandbox import Workspace

BRIEF_FILE = "project_brief.json"
MAX_NOTE_CHARS = 2000
_KEY = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


class ProjectBrief:
    def __init__(self, workspace: Workspace, project_id: str, goal: str,
                 on_change: Callable[[str, str], None] | None = None) -> None:
        self._workspace = workspace
        self._on_change = on_change
        self._lock = threading.Lock()
        self.data: dict[str, Any] = {
            "project_id": project_id,
            "goal": goal,
            "created": datetime.now().isoformat(timespec="seconds"),
            "notes": {},  # key -> {"text": ..., "by": ...}
        }

    def set_note(self, key: str, text: str, author: str) -> None:
        if not _KEY.match(key):
            raise ValueError("Key must be short snake_case, e.g. 'audience' or 'tone_of_voice'.")
        text = text.strip()
        if not text:
            raise ValueError("The note text must not be empty.")
        with self._lock:
            self.data["notes"][key] = {"text": text[:MAX_NOTE_CHARS], "by": author}
            self._save_locked()
        if self._on_change is not None:
            self._on_change(key, author)

    def render(self) -> str:
        """The brief as text, for agents' prompts."""
        with self._lock:
            lines = [f"Client goal: {self.data['goal']}"]
            for key, note in self.data["notes"].items():
                lines.append(f"\n{key} (by {note['by']}):\n{note['text']}")
            return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self.data))  # deep copy

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        text = json.dumps(self.data, indent=2, ensure_ascii=False)
        self._workspace.write_text(BRIEF_FILE, text, internal=True)

    @classmethod
    def load(cls, workspace: Workspace) -> ProjectBrief:
        data = json.loads(workspace.read_text(BRIEF_FILE))
        brief = cls(workspace, data.get("project_id", ""), data.get("goal", ""))
        brief.data = data
        return brief
