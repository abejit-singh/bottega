"""Shared project state: task board, message log and brief.

This package depends only on the sandbox, never on agents or tools, so both of
those can import it freely.
"""

from bottega.project.board import MAX_TASKS, BoardError, Task, TaskBoard, TaskStatus
from bottega.project.brief import BRIEF_FILE, ProjectBrief
from bottega.project.messages import EVERYONE, Message, MessageLog
from bottega.project.state import STATE_FILE, ProjectState

__all__ = [
    "BRIEF_FILE",
    "EVERYONE",
    "MAX_TASKS",
    "STATE_FILE",
    "BoardError",
    "Message",
    "MessageLog",
    "ProjectBrief",
    "ProjectState",
    "Task",
    "TaskBoard",
    "TaskStatus",
]
