"""Demo mode: a scripted stand-in for the Claude API.

`python -m bottega serve --demo` swaps the real Anthropic client for this
`DemoClient`. It replays a fixed script through the *real* agent loop, tools,
events and dashboard, so you can explore and develop the UI:
  * without an API key,
  * without spending money,
  * with the same result every time.

Nothing here is written by Claude: the "research" is a placeholder and the
costs shown are simulated. Demo files go to a temporary folder, so your real
`output/` and `memory/` are never touched.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from anthropic.types.beta import BetaMessage

from bottega.config import Settings


def demo_settings(settings: Settings) -> Settings:
    """Point output and memory at a throwaway folder (memory starts as a copy)."""
    root = Path(tempfile.mkdtemp(prefix="bottega-demo-"))
    if settings.memory_dir.is_dir():
        shutil.copytree(settings.memory_dir, root / "memory",
                        ignore=shutil.ignore_patterns(".obsidian"))
    return replace(settings, output_dir=root / "output", memory_dir=root / "memory")


def _goal_from(first_message: Any) -> str:
    text = first_message if isinstance(first_message, str) else ""
    return text.removeprefix("Client goal:").strip() or "your goal"


def _research_placeholder(goal: str) -> str:
    return f"""# Research (demo placeholder)

> **Demo mode.** This file was produced by a script, not by Claude.
> Add your API key and start the dashboard without `--demo` to get real research.

**Goal:** {goal}

## Business summary
- (In a real run, the Researcher describes the business and its local context here.)

## Target audience
- (2-3 personas with their goals and objections.)

## Competitors
- (Typical competitor archetypes for this kind of business.)

## Positioning
- (One-sentence positioning statement and 3 key messages.)

## Recommendations for the team
- Designer: (mood and references)
- Developer: see [[Landing page checklist]] for must-have sections
- Marketer: (tone of voice, channels, languages)
"""


def _script(step: int, goal: str) -> tuple[list[dict[str, Any]], str, int, int]:
    """Return (content blocks, stop_reason, input_tokens, output_tokens) for a step."""
    tool_id = f"toolu_demo_{step}"
    if step == 0:
        return ([
            {"type": "text", "text": "Let me check what Bottega already knows about this kind of project."},
            {"type": "tool_use", "id": tool_id, "name": "search_memory",
             "input": {"query": f"landing page checklist {goal}"}},
        ], "tool_use", 2100, 90)
    if step == 1:
        return ([
            {"type": "tool_use", "id": tool_id, "name": "read_memory",
             "input": {"note": "Landing page checklist"}},
        ], "tool_use", 2400, 60)
    if step == 2:
        return ([
            {"type": "text", "text": "The playbook applies. Writing the research brief for the team."},
            {"type": "tool_use", "id": tool_id, "name": "write_file",
             "input": {"path": "research.md", "content": _research_placeholder(goal)}},
        ], "tool_use", 3100, 820)
    if step == 3:
        return ([
            {"type": "tool_use", "id": tool_id, "name": "save_memory",
             "input": {
                 "folder": "lessons",
                 "title": "Demo run notes",
                 "content": ("Placeholder saved by demo mode to show how agents add to company "
                             f"memory.\n\nLast demo goal: {goal}\n\nRelated: [[Landing page checklist]]"),
                 "tags": ["demo"],
             }},
        ], "tool_use", 4000, 160)
    return ([
        {"type": "text", "text": ("Research brief saved to research.md and one note added to company "
                                  "memory. (Demo mode: placeholder content, no API calls were made.)")},
    ], "end_turn", 4200, 70)


class _DemoStream:
    def __init__(self, message: BetaMessage, delay: float) -> None:
        self._message = message
        self._delay = delay

    def __enter__(self) -> _DemoStream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_final_message(self) -> BetaMessage:
        time.sleep(self._delay)  # pretend the model is thinking
        return self._message


class DemoClient:
    """Mimics the one SDK call the agent loop uses: `client.beta.messages.stream()`."""

    def __init__(self, delay: float = 1.6) -> None:
        self.delay = delay
        self.beta = self
        self.messages = self

    def stream(self, **request: Any) -> _DemoStream:
        messages = request["messages"]
        # The step is derived from the conversation itself (how many times the
        # "model" has already answered), so this client needs no per-run state.
        step = sum(1 for m in messages if m["role"] == "assistant")
        content, stop_reason, tokens_in, tokens_out = _script(step, _goal_from(messages[0]["content"]))
        message = BetaMessage.model_validate({
            "id": f"msg_demo_{step}",
            "type": "message",
            "role": "assistant",
            "model": request["model"],
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
        })
        return _DemoStream(message, self.delay)
