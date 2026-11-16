"""The `Tool` abstraction: a JSON schema Claude sees + a Python function we run.

How tool use works with the Claude API:
  1. We send tool *definitions* (name, description, JSON schema) with the request.
  2. Claude replies with a `tool_use` block: "call write_file with {...}".
  3. WE run the function and send back a `tool_result` block.
Claude never executes anything itself; our code is always in control. That's
why tools are the right place to enforce rules (sandboxing, validation, limits).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bottega.sandbox import Workspace

if TYPE_CHECKING:  # import only for type hints, avoids a runtime import cycle
    from bottega.memory import MemoryVault
    from bottega.project import ProjectState


class ToolError(Exception):
    """An expected failure we report back to the model (not a crash).

    The message is sent to Claude as an error tool_result, so it should tell
    the model how to fix its call, e.g. "File 'x.md' does not exist."
    """


@dataclass
class ToolContext:
    """Everything a tool handler may need to do its job."""

    agent_name: str  # display name, e.g. "Developer"
    workspace: Workspace  # this project's files
    memory: MemoryVault | None = None  # company-wide long-term memory
    role: str = ""  # machine name, e.g. "developer"
    project: ProjectState | None = None  # task board, messages, brief (None in solo runs)
    task_id: str | None = None  # the task this agent is working on or reviewing
    files_written: list[str] = field(default_factory=list)


ToolHandler = Callable[[ToolContext, dict[str, Any]], str]

_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler

    def to_api(self) -> dict[str, Any]:
        """The tool definition in the shape the Messages API expects."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            # Stream large tool inputs (whole HTML files!) as they're generated
            # instead of in one burst at the end. Trade-off: the API no longer
            # validates the input for us, so `validate()` below is mandatory.
            "eager_input_streaming": True,
        }

    def validate(self, tool_input: Any) -> dict[str, Any]:
        """Check the model's input against our (flat) schema. Raises ToolError."""
        if not isinstance(tool_input, dict):
            raise ToolError("Tool input must be a JSON object.")
        props: dict[str, Any] = self.input_schema.get("properties", {})
        missing = [k for k in self.input_schema.get("required", []) if k not in tool_input]
        if missing:
            raise ToolError(f"Missing required field(s): {', '.join(missing)}.")
        unknown = [k for k in tool_input if k not in props]
        if unknown:
            raise ToolError(f"Unknown field(s): {', '.join(unknown)}.")
        for key, value in tool_input.items():
            json_type = props[key].get("type", "")
            expected = _JSON_TYPES.get(json_type, object)
            # Gotcha: in Python `True` is an int, so check booleans explicitly.
            is_wrong_bool = isinstance(value, bool) and json_type != "boolean"
            if is_wrong_bool or not isinstance(value, expected):
                raise ToolError(f"Field {key!r} must be of type {json_type}.")
        return tool_input
