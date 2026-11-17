"""The base `Agent`: a system prompt, a set of tools, and the tool-use loop.

Every Bottega role (CEO, Developer, QA, ...) is this same class with a
different prompt and tool list. The loop:

    messages = [task]
    repeat:
        response = Claude(system_prompt, tools, messages)
        if Claude asked for tools -> run them, append results, loop again
        else                      -> Claude is done, return its final text

Design decision: we write this loop by hand instead of using the SDK's beta
"tool runner" helper. The helper is less code, but owning the loop lets us
(a) emit an event for every step (for the live dashboard), (b) record cost
after every single call (for the budget limit), and (c) see exactly how
agents work — which is the point of this project.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

import anthropic

from bottega.config import Settings
from bottega.costs import CostTracker
from bottega.events import Event, EventHandler, EventKind, ignore_event
from bottega.memory import MemoryVault
from bottega.project import ProjectState
from bottega.sandbox import Workspace
from bottega.tools.base import Tool, ToolContext, ToolError

logger = logging.getLogger(__name__)

# Beta flag that enables server-side refusal fallbacks (see Settings.refusal_fallback).
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AgentError(Exception):
    """The agent could not complete its task (e.g. the model refused)."""


@dataclass
class AgentResult:
    agent: str
    final_text: str
    # "end_turn" = finished normally. Others: "max_tokens", "max_turns",
    # "cancelled" (stop button), "budget_exceeded" (cost limit).
    stop_reason: str
    turns: int
    files_written: list[str] = field(default_factory=list)
    cost_usd: float = 0.0

    @property
    def completed(self) -> bool:
        return self.stop_reason == "end_turn"


class Agent:
    def __init__(
        self,
        *,
        name: str,
        system_prompt: str,
        tools: list[Tool],
        client: anthropic.Anthropic,
        settings: Settings,
        workspace: Workspace,
        costs: CostTracker,
        role: str = "",
        memory: MemoryVault | None = None,
        project: ProjectState | None = None,
        cancel: threading.Event | None = None,
        on_event: EventHandler = ignore_event,
    ) -> None:
        self.name = name  # display name, e.g. "Developer"
        self.role = role  # machine name, e.g. "developer"
        self.project = project  # shared board/messages/brief; None when working solo
        self.system_prompt = system_prompt
        self.tools = {tool.name: tool for tool in tools}
        self.client = client
        self.settings = settings
        self.workspace = workspace
        self.costs = costs
        self.memory = memory
        # A flag another thread can set to ask this agent to stop (the dashboard's
        # Stop button). threading.Event is the standard thread-safe way to do that.
        self.cancel = cancel
        self.on_event = on_event

    # ------------------------------------------------------------------ public

    def run(self, task: str, task_id: str | None = None) -> AgentResult:
        """Work on `task` until Claude stops calling tools (or limits are hit).

        `task_id` is the board task this run belongs to, if any. Tools use it
        to make sure an agent only hands in (or reviews) its own task.
        """
        self._emit("agent_start", task=task, task_id=task_id)
        ctx = ToolContext(
            agent_name=self.name, workspace=self.workspace, memory=self.memory,
            role=self.role, project=self.project, task_id=task_id,
        )
        # The API is stateless: we resend the whole conversation every call.
        messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
        cost_usd = 0.0

        for turn in range(1, self.settings.max_turns + 1):
            # Safety checks run *between* model calls. Tools requested in the
            # previous turn have already run, so finished work is never lost.
            stop = self._should_stop()
            if stop is not None:
                reason, message = stop
                return self._finish(reason, message, turn - 1, ctx, cost_usd)

            response = self._call_model(messages)
            cost_usd += self.costs.record(self.name, response.model, response.usage)
            self._emit("usage", turn=turn, cost_usd=cost_usd)

            # Append Claude's reply *unchanged* (including thinking blocks): the
            # API needs them back on the next call to keep its reasoning valid.
            messages.append({"role": "assistant", "content": response.content})

            for block in response.content:
                if block.type == "text" and block.text.strip():
                    self._emit("text", text=block.text)

            if response.stop_reason == "refusal":
                detail = getattr(response.stop_details, "explanation", None) or "no details"
                self._emit("error", message=f"Model refused: {detail}")
                raise AgentError(f"{self.name}: the model declined this task ({detail}).")

            if response.stop_reason == "pause_turn":
                # The server paused a long turn; resending lets it continue.
                continue

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                # No tool calls means Claude considers the task finished.
                return self._finish(
                    response.stop_reason or "unknown",
                    _joined_text(response.content), turn, ctx, cost_usd,
                )

            if response.stop_reason == "max_tokens":
                # The reply was cut off mid tool call, so its input is incomplete.
                # Don't run it; tell Claude what happened so it can retry smaller.
                tool_results = [
                    _error_result(b.id, "Your output hit the max_tokens limit and this tool "
                                        "call was cut off. Retry with a smaller file or split "
                                        "the content across several files.")
                    for b in tool_uses
                ]
            else:
                tool_results = [self._run_tool(b, ctx) for b in tool_uses]

            # All results go back in ONE user message (required for parallel tool calls).
            messages.append({"role": "user", "content": tool_results})

        logger.warning("%s hit max_turns=%d", self.name, self.settings.max_turns)
        return self._finish(
            "max_turns", "Stopped: reached the maximum number of turns.",
            self.settings.max_turns, ctx, cost_usd,
        )

    # ----------------------------------------------------------------- helpers

    def _should_stop(self) -> tuple[str, str] | None:
        """Return (stop_reason, message) if the agent must stop now, else None."""
        if self.cancel is not None and self.cancel.is_set():
            return "cancelled", "Stopped by the user."
        # The tracker is shared by every agent on a project, so this is a
        # per-project budget: a runaway loop can't quietly burn your credits.
        if self.costs.total_usd >= self.settings.max_cost_usd:
            limit = self.settings.max_cost_usd
            return "budget_exceeded", f"Stopped: the ${limit:.2f} budget for this project is used up."
        return None

    def _finish(self, stop_reason: str, final_text: str, turns: int,
                ctx: ToolContext, cost_usd: float) -> AgentResult:
        """Every exit from the loop goes through here, so `agent_done` is always emitted."""
        self._emit("agent_done", stop_reason=stop_reason, cost_usd=cost_usd)
        return AgentResult(
            agent=self.name,
            final_text=final_text,
            stop_reason=stop_reason,
            turns=turns,
            files_written=list(ctx.files_written),
            cost_usd=cost_usd,
        )

    def _call_model(self, messages: list[dict[str, Any]]) -> Any:
        """One call to the Messages API. Returns the final BetaMessage."""
        request = self.build_request(messages)
        # Streaming avoids HTTP timeouts on long outputs (an agent writing a big
        # HTML file). get_final_message() waits and hands us the assembled reply.
        with self.client.beta.messages.stream(**request) as stream:
            return stream.get_final_message()

    def build_request(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """The exact request this agent sends. Public so `bottega check` can
        test the real request shape before a project spends anything."""
        request: dict[str, Any] = {
            "model": self.settings.model,
            "max_tokens": self.settings.max_tokens,
            # cache_control marks the system prompt (and the tool list before it)
            # for prompt caching: later calls in this loop re-read it at ~5% cost.
            "system": [
                {"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}}
            ],
            "tools": [tool.to_api() for tool in self.tools.values()],
            "messages": messages,
            # Thinking is adaptive by default on current models; effort tunes depth/cost.
            "output_config": {"effort": self.settings.effort},
        }
        if self.settings.refusal_fallback:
            request["betas"] = [FALLBACK_BETA]
            request["fallbacks"] = "default"
        return request

    def _run_tool(self, block: Any, ctx: ToolContext) -> dict[str, Any]:
        """Execute one tool_use block and build the matching tool_result block."""
        preview = _preview_input(block.input)
        self._emit("tool_call", tool=block.name, input=preview)
        tool = self.tools.get(block.name)
        try:
            if tool is None:
                raise ToolError(f"Unknown tool {block.name!r}. Available: {', '.join(self.tools)}.")
            output = tool.handler(ctx, tool.validate(block.input))
        except ToolError as exc:
            self._emit("tool_result", tool=block.name, input=preview, ok=False, output=str(exc))
            return _error_result(block.id, str(exc))
        except Exception as exc:  # a bug in a tool must not crash the whole company
            logger.exception("Tool %s crashed", block.name)
            self._emit("tool_result", tool=block.name, input=preview, ok=False, output=repr(exc))
            return _error_result(block.id, f"Internal tool error: {exc!r}")

        self._emit("tool_result", tool=block.name, input=preview, ok=True, output=output[:300])
        return {"type": "tool_result", "tool_use_id": block.id, "content": output}

    def _emit(self, kind: EventKind, **data: Any) -> None:
        try:
            self.on_event(Event(agent=self.name, kind=kind, data=data))
        except Exception:  # a broken listener must not break the agent
            logger.exception("Event handler failed")


def _error_result(tool_use_id: str, message: str) -> dict[str, Any]:
    # is_error tells Claude the call failed so it can correct itself.
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": message, "is_error": True}


def _joined_text(content: list[Any]) -> str:
    return "\n".join(b.text for b in content if b.type == "text").strip()


def _preview_input(tool_input: Any) -> Any:
    """Shorten long string values (file contents) so events stay readable."""
    if not isinstance(tool_input, dict):
        return tool_input
    return {
        k: (v[:120] + f"... [{len(v)} chars]" if isinstance(v, str) and len(v) > 120 else v)
        for k, v in tool_input.items()
    }
