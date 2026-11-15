"""Command-line interface.

Phase 1:  python -m agentco solo researcher "Build a landing page for a pizzeria in Mantova, Italy"
Phase 2+: python -m agentco run "goal"   (the whole company)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

import anthropic

from agentco.agents import ROLES, AgentError, build_agent
from agentco.config import Settings
from agentco.costs import CostTracker
from agentco.events import Event
from agentco.tools import Workspace, new_project_id

_ICONS = {
    "agent_start": "▶",
    "text": "💬",
    "tool_call": "🔧",
    "tool_result": "  ↳",
    "agent_done": "✔",
    "error": "✖",
}


def print_event(event: Event) -> None:
    """CLI event listener: one readable line per event."""
    icon = _ICONS.get(event.kind, "•")
    d = event.data
    if event.kind == "agent_start":
        line = f"{event.agent} starts working"
    elif event.kind == "text":
        line = d["text"].strip()
    elif event.kind == "tool_call":
        line = f"{d['tool']}({json.dumps(d['input'], ensure_ascii=False)})"
    elif event.kind == "tool_result":
        status = "ok" if d["ok"] else "ERROR"
        line = f"{status}: {d['output'].splitlines()[0] if d['output'] else ''}"
    elif event.kind == "agent_done":
        line = f"{event.agent} finished ({d['stop_reason']}, ${d['cost_usd']:.4f})"
    else:
        line = d.get("message", "")
    print(f"{icon} [{event.agent}] {line}", flush=True)


def cmd_solo(args: argparse.Namespace, settings: Settings) -> int:
    project_id = new_project_id(args.goal)
    workspace = Workspace(settings.output_dir / project_id)
    costs = CostTracker()
    agent = build_agent(
        args.role,
        client=anthropic.Anthropic(),  # reads ANTHROPIC_API_KEY from the environment
        settings=settings,
        workspace=workspace,
        costs=costs,
        on_event=print_event,
    )
    print(f"Project {project_id}  |  model {settings.model}  |  effort {settings.effort}\n")
    try:
        result = agent.run(f"Client goal: {args.goal}")
    except AgentError as exc:
        print(f"\nAgent failed: {exc}", file=sys.stderr)
        return 1

    print(f"\nFiles in {workspace.root}:")
    for path in workspace.list_files():
        print(f"  - {path}")
    for name, spend in costs.by_agent().items():
        u = spend.usage
        print(
            f"\n{name}: {spend.calls} API calls, {u.input_tokens} in / {u.output_tokens} out "
            f"tokens (cache read {u.cache_read_tokens}), ${spend.cost_usd:.4f}"
        )
    return 0 if result.completed else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentco", description="A company run by AI agents.")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    solo = sub.add_parser("solo", help="run a single agent on a goal (phase 1)")
    solo.add_argument("role", choices=sorted(ROLES), help="which agent to run")
    solo.add_argument("goal", help='e.g. "Build a landing page for a pizzeria in Mantova, Italy"')
    solo.set_defaults(func=cmd_solo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    try:
        return args.func(args, settings)
    except anthropic.AuthenticationError:
        print("Authentication failed: set ANTHROPIC_API_KEY in your .env file.", file=sys.stderr)
    except anthropic.APIConnectionError:
        print("Could not reach the Claude API. Check your internet connection.", file=sys.stderr)
    except anthropic.APIStatusError as exc:
        print(f"Claude API error {exc.status_code}: {exc.message}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    return 1
