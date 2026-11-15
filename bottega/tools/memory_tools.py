"""Tools that let agents consult and grow the company memory (memory/ vault)."""

from __future__ import annotations

from typing import Any

from bottega.memory import FOLDERS, MemoryVault
from bottega.tools.base import Tool, ToolContext, ToolError
from bottega.sandbox import SandboxError


def _vault(ctx: ToolContext) -> MemoryVault:
    if ctx.memory is None:
        raise ToolError("Company memory is not available in this run.")
    return ctx.memory


def _search_memory(ctx: ToolContext, args: dict[str, Any]) -> str:
    hits = _vault(ctx).search(args["query"])
    if not hits:
        return "No notes matched. Try other keywords, or proceed without memory."
    lines = [f"{len(hits)} note(s) found (use read_memory with the title to open one):"]
    for h in hits:
        tags = f" [tags: {', '.join(h.tags)}]" if h.tags else ""
        lines.append(f"- {h.title} ({h.path}){tags}\n  {h.snippet}")
    return "\n".join(lines)


def _read_memory(ctx: ToolContext, args: dict[str, Any]) -> str:
    try:
        return _vault(ctx).read(args["note"])
    except SandboxError as exc:
        raise ToolError(str(exc)) from exc


def _save_memory(ctx: ToolContext, args: dict[str, Any]) -> str:
    tags = args.get("tags", [])
    # Our validator only checks top-level types, so check list items here.
    if not all(isinstance(t, str) for t in tags):
        raise ToolError("Tags must be a list of strings.")
    try:
        rel = _vault(ctx).save(
            folder=args["folder"], title=args["title"], content=args["content"],
            tags=tags, author=ctx.agent_name,
        )
    except SandboxError as exc:
        raise ToolError(str(exc)) from exc
    return f"Saved to company memory: {rel}"


SEARCH_MEMORY = Tool(
    name="search_memory",
    description=(
        "Search Bottega's long-term company memory (notes from past projects: client notes, "
        "playbooks, lessons learned) by keywords. Do this at the start of a task to reuse what "
        "the company already knows. Returns matching note titles with short excerpts."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords, e.g. 'pizzeria Mantova landing page'."}
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    handler=_search_memory,
)

READ_MEMORY = Tool(
    name="read_memory",
    description="Read one note from company memory, by title (e.g. 'Landing page checklist') or path.",
    input_schema={
        "type": "object",
        "properties": {"note": {"type": "string", "description": "Note title or relative path."}},
        "required": ["note"],
        "additionalProperties": False,
    },
    handler=_read_memory,
)

SAVE_MEMORY = Tool(
    name="save_memory",
    description=(
        "Save knowledge worth reusing in FUTURE projects to company memory. Only save durable, "
        "reusable insights (facts about a client or market, a working checklist, a lesson "
        "learned), never temporary task details. Saving with an existing title overwrites that "
        "note, so read it first and merge. Link related notes with [[Note title]]. "
        f"Folders: {', '.join(FOLDERS)}."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "folder": {"type": "string", "enum": list(FOLDERS)},
            "title": {"type": "string", "description": "Short, unique title, e.g. 'Mantova food scene'."},
            "content": {"type": "string", "description": "Markdown body (no frontmatter needed)."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "2-5 lowercase tags."},
        },
        "required": ["folder", "title", "content"],
        "additionalProperties": False,
    },
    handler=_save_memory,
)

MEMORY_TOOLS = [SEARCH_MEMORY, READ_MEMORY, SAVE_MEMORY]
