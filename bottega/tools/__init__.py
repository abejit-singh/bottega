"""Tool registry: every tool any agent may be given, looked up by name."""

from bottega.tools.base import Tool, ToolContext, ToolError
from bottega.tools.files import FILE_TOOLS
from bottega.tools.memory_tools import MEMORY_TOOLS
from bottega.sandbox import SandboxError, Workspace, new_project_id

TOOL_REGISTRY: dict[str, Tool] = {tool.name: tool for tool in [*FILE_TOOLS, *MEMORY_TOOLS]}

__all__ = [
    "TOOL_REGISTRY",
    "SandboxError",
    "Tool",
    "ToolContext",
    "ToolError",
    "Workspace",
    "new_project_id",
]
