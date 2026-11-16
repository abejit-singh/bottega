"""Tool registry: every tool any agent may be given, looked up by name."""

from bottega.sandbox import SandboxError, Workspace, new_project_id
from bottega.tools.base import Tool, ToolContext, ToolError
from bottega.tools.board_tools import BOARD_TOOLS
from bottega.tools.files import FILE_TOOLS
from bottega.tools.memory_tools import MEMORY_TOOLS

TOOL_REGISTRY: dict[str, Tool] = {
    tool.name: tool for tool in [*FILE_TOOLS, *MEMORY_TOOLS, *BOARD_TOOLS]
}
# Tools that only make sense when there is a task board (dropped in solo runs).
BOARD_TOOL_NAMES: frozenset[str] = frozenset(tool.name for tool in BOARD_TOOLS)

__all__ = [
    "BOARD_TOOL_NAMES",
    "TOOL_REGISTRY",
    "SandboxError",
    "Tool",
    "ToolContext",
    "ToolError",
    "Workspace",
    "new_project_id",
]
