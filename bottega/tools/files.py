"""File tools: the way agents produce real deliverables.

Tool descriptions are prompts too: Claude decides when and how to call a tool
largely from its description, so we write them like instructions to a colleague.
"""

from __future__ import annotations

from typing import Any

from bottega.tools.base import Tool, ToolContext, ToolError
from bottega.sandbox import SandboxError


def _write_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    try:
        path = ctx.workspace.write_text(args["path"], args["content"])
    except SandboxError as exc:
        raise ToolError(str(exc)) from exc
    rel = ctx.workspace.relative(path)
    if rel not in ctx.files_written:
        ctx.files_written.append(rel)
    return f"Wrote {rel} ({len(args['content'])} characters)."


def _read_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    try:
        return ctx.workspace.read_text(args["path"])
    except SandboxError as exc:
        raise ToolError(str(exc)) from exc


def _list_files(ctx: ToolContext, args: dict[str, Any]) -> str:
    files = ctx.workspace.list_files()
    return "\n".join(files) if files else "(the project folder is empty)"


WRITE_FILE = Tool(
    name="write_file",
    description=(
        "Create or overwrite a text file in the project folder. Use this for every "
        "deliverable (markdown, HTML, CSS, JS, JSON). Paths are relative to the project "
        "folder, e.g. 'research.md' or 'site/index.html'. Write the complete file content; "
        "partial edits are not supported."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path, e.g. 'research.md'."},
            "content": {"type": "string", "description": "The full file content."},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
    handler=_write_file,
)

READ_FILE = Tool(
    name="read_file",
    description=(
        "Read a text file from the project folder, e.g. another agent's deliverable. "
        "Use list_files first if you are not sure which files exist."
    ),
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Relative path to read."}},
        "required": ["path"],
        "additionalProperties": False,
    },
    handler=_read_file,
)

LIST_FILES = Tool(
    name="list_files",
    description="List every file currently in the project folder.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    handler=_list_files,
)

FILE_TOOLS = [WRITE_FILE, READ_FILE, LIST_FILES]
