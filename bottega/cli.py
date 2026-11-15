"""Command-line interface.

    python -m bottega serve            # the command center dashboard
    python -m bottega serve --demo     # same, with scripted responses (no API key, no cost)
    python -m bottega solo researcher "Build a landing page for a pizzeria in Mantova, Italy"

Phase 2+ adds: python -m bottega run "goal"   (the whole company, in the terminal)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import anthropic

from bottega.agents import ROLES, AgentError, build_agent
from bottega.config import Settings
from bottega.costs import CostTracker
from bottega.errors import explain
from bottega.events import Event
from bottega.memory import MemoryVault
from bottega.tools import Workspace, new_project_id

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
    if event.kind == "usage":
        return  # cost ticks are for the dashboard; the CLI prints a summary at the end
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
    memory = MemoryVault(settings.memory_dir)
    notes_before = set(memory.list_notes())
    agent = build_agent(
        args.role,
        client=anthropic.Anthropic(),  # reads ANTHROPIC_API_KEY from the environment
        settings=settings,
        workspace=workspace,
        costs=costs,
        memory=memory,
        on_event=print_event,
    )
    print(f"Project {project_id}  |  model {settings.model}  |  effort {settings.effort}\n")
    try:
        result = agent.run(f"Client goal: {args.goal}")
    except AgentError as exc:
        print(f"\nAgent failed: {explain(exc)}", file=sys.stderr)
        return 1

    print(f"\nFiles in {workspace.root}:")
    for path in workspace.list_files():
        print(f"  - {path}")
    new_notes = sorted(set(memory.list_notes()) - notes_before)
    if new_notes:
        print(f"\nNew notes in company memory ({memory.root}):")
        for note in new_notes:
            print(f"  + {note}")
    for name, spend in costs.by_agent().items():
        u = spend.usage
        print(
            f"\n{name}: {spend.calls} API calls, {u.input_tokens} in / {u.output_tokens} out "
            f"tokens (cache read {u.cache_read_tokens}), ${spend.cost_usd:.4f}"
        )
    if not result.completed:
        print(f"\nNot finished: {result.final_text}", file=sys.stderr)
    return 0 if result.completed else 2


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    # Imported here so `solo` keeps working even if the web extras are missing.
    try:
        import uvicorn

        from bottega.company import Company
        from bottega.web import EventHub, create_app
    except ImportError as exc:
        print(f"The dashboard needs FastAPI and uvicorn ({exc}).\n"
              "Install them with: pip install -r requirements.txt", file=sys.stderr)
        return 1

    hub = EventHub()
    if args.demo:
        from bottega.demo import DemoClient, demo_settings

        company = Company(demo_settings(settings), publish=hub.publish,
                          client_factory=DemoClient, demo=True)
        print("DEMO MODE: scripted responses, no API calls, nothing is billed.")
    else:
        company = Company(settings, publish=hub.publish)
        if not os.getenv("ANTHROPIC_API_KEY"):
            print("Warning: ANTHROPIC_API_KEY is not set. Projects will fail until you add it "
                  "to .env (or use --demo to explore the dashboard for free).", file=sys.stderr)

    shown_host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print(f"Bottega command center: http://{shown_host}:{args.port}   (Ctrl+C to stop)")
    uvicorn.run(create_app(company, hub), host=args.host, port=args.port,
                log_level="warning", timeout_graceful_shutdown=2)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bottega", description="Bottega: a workshop run by AI agents.")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    solo = sub.add_parser("solo", help="run a single agent on a goal (phase 1)")
    solo.add_argument("role", choices=sorted(ROLES), help="which agent to run")
    solo.add_argument("goal", help='e.g. "Build a landing page for a pizzeria in Mantova, Italy"')
    solo.set_defaults(func=cmd_solo)

    serve = sub.add_parser("serve", help="start the command center dashboard")
    serve.add_argument("--demo", action="store_true",
                       help="scripted responses: no API key needed, nothing is billed")
    serve.add_argument("--host", default="127.0.0.1",
                       help="address to listen on (default: this computer only)")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)
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
    except anthropic.AnthropicError as exc:
        print(explain(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
