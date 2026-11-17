"""Command-line interface.

    python -m bottega serve               # the dashboard, at http://localhost:8000
    python -m bottega serve --demo        # same, with scripted responses (no key, no cost)
    python -m bottega run "goal"          # the whole team, in the terminal
    python -m bottega run --demo "goal"   # scripted, to see how it works for free
    python -m bottega check               # is the key, the model and every agent set up right?
    python -m bottega solo researcher "goal"   # one agent alone (handy for tuning a prompt)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

import anthropic

from bottega.agents import AgentError, build_agent
from bottega.company import Company
from bottega.config import Settings
from bottega.costs import CostTracker
from bottega.errors import explain
from bottega.events import Event
from bottega.memory import MemoryVault
from bottega.team import WORKER_ROLES
from bottega.tools import Workspace, new_project_id

_ICONS = {
    "agent_start": "▶",
    "text": "💬",
    "tool_call": "🔧",
    "tool_result": "  ↳",
    "agent_done": "✔",
    "error": "✖",
}

_TASK_CHANGES = {
    "created": "📌 new task",
    "started": "▶ started",
    "submitted": "📤 handed in",
    "approved": "✅ approved",
    "revision": "↩ sent back",
    "released": "⏸ back in the queue",
    "failed": "✖ failed",
}


def print_event(event: Event) -> None:
    """CLI listener for one agent's events: one readable line per event."""
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


def print_team_event(event: dict[str, Any]) -> None:
    """CLI listener for a whole-team run. Shows the story (tasks, messages,
    files, problems) and leaves out the play-by-play of every tool call."""
    agent, kind, d = event["agent"], event["kind"], event["data"]
    if kind == "project_start":
        line = f"🎯 New project: {d['goal']}"
    elif kind == "task":
        task = d["task"]
        line = f"{_TASK_CHANGES.get(d['change'], d['change'])}: {task['id']} {task['title']}"
        if d["change"] == "revision":
            line += f"\n      feedback: {task['feedback'][-1]}"
        elif d["change"] == "failed":
            line += f"\n      reason: {task['error']}"
    elif kind == "message":
        line = f"💬 to {d['to']}: {d['text']}"
    elif kind == "tool_result" and d["tool"] == "write_file" and d["ok"]:
        line = f"📄 {d['output']}"
    elif kind == "tool_result" and not d["ok"]:
        line = f"⚠ {d['tool']} failed: {d['output'].splitlines()[0]}"
    elif kind == "error":
        line = f"✖ {d.get('message', '')}"
    elif kind == "project_done":
        line = f"🏁 Project {d['status']} (${d['cost_usd']:.4f}, {d['files']} files)"
    else:
        return
    print(f"[{agent}] {line}", flush=True)


def _warn_if_no_key() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Warning: ANTHROPIC_API_KEY is not set. Projects will fail until you add it "
              "to .env (or use --demo to try Bottega for free).", file=sys.stderr)


def _build_company(args: argparse.Namespace, settings: Settings, publish: Any) -> Company:
    if args.demo:
        from bottega.demo import DemoClient, demo_settings

        print("DEMO MODE: scripted responses, no API calls, nothing is billed.")
        delay = 1.0 if args.command == "serve" else 0.15
        return Company(demo_settings(settings), publish=publish,
                       client_factory=lambda: DemoClient(delay=delay), demo=True)
    _warn_if_no_key()
    return Company(settings, publish=publish)


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    company = _build_company(args, settings, print_team_event)
    project = company.start_project(args.goal)
    print(f"Project {project.id}  |  model {settings.model}  |  budget ${settings.max_cost_usd:.2f}\n")
    try:
        while project.thread is not None and project.thread.is_alive():
            project.thread.join(0.2)  # short waits keep Ctrl+C responsive
    except KeyboardInterrupt:
        print("\nStopping after the current step...", file=sys.stderr)
        company.stop_project(project.id)
        company.wait(project.id)

    if project.state is not None:
        print("\nTask board:")
        for task in project.state.board.tasks():
            revised = f", revised {task.revisions}x" if task.revisions else ""
            print(f"  {task.id} [{task.status}{revised}] {task.title} ({task.owner})")
    print(f"\nFiles in {project.workspace.root}:")
    for path in project.workspace.list_files():
        print(f"  - {path}")
    print("\nCost by agent:")
    for name, spend in project.costs.by_agent().items():
        print(f"  {name:<11} {spend.calls:>3} calls  ${spend.cost_usd:.4f}")
    print(f"  {'Total':<11} {'':>9}  ${project.costs.total_usd:.4f}")
    if project.summary:
        print(f"\nSummary for the client:\n{project.summary}")
    if project.error:
        print(f"\nError: {project.error}", file=sys.stderr)
    return 0 if project.status == "done" else 2


def cmd_solo(args: argparse.Namespace, settings: Settings) -> int:
    _warn_if_no_key()
    project_id = new_project_id(args.goal)
    workspace = Workspace(settings.output_dir / project_id)
    costs = CostTracker()
    memory = MemoryVault(settings.memory_dir)
    notes_before = set(memory.list_notes())
    agent = build_agent(
        args.role,
        client=anthropic.Anthropic(max_retries=settings.api_retries),  # reads ANTHROPIC_API_KEY
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


def cmd_check(args: argparse.Namespace, settings: Settings) -> int:
    from bottega.check import run_checks

    print(f"Checking Bottega with model {settings.model}…\n")
    results = run_checks(settings, lambda: anthropic.Anthropic(max_retries=settings.api_retries),
                         live=args.live)
    for result in results:
        print(f"  {'✓' if result.ok else '✕'} {result.name}: {result.detail}")
    if all(r.ok for r in results):
        hint = "" if args.live else " Add --live to also send one tiny real request (under a cent)."
        print(f"\nAll good: the team is ready to work.{hint}")
        return 0
    print("\nFix the problem above, then run the check again.", file=sys.stderr)
    return 1


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    # Imported here so `run` and `solo` keep working even if the web extras are missing.
    try:
        import uvicorn

        from bottega.web import EventHub, create_app
    except ImportError as exc:
        print(f"The dashboard needs FastAPI and uvicorn ({exc}).\n"
              "Install them with: pip install -r requirements.txt", file=sys.stderr)
        return 1

    hub = EventHub()
    company = _build_company(args, settings, hub.publish)
    if not args.demo:
        import threading

        # Check the connection to Claude in the background; the page shows the result.
        threading.Thread(target=company.check_connection, name="check", daemon=True).start()
    shown_host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print(f"Bottega is open: http://{shown_host}:{args.port}   (Ctrl+C to stop)")
    uvicorn.run(create_app(company, hub), host=args.host, port=args.port,
                log_level="warning", timeout_graceful_shutdown=2)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bottega", description="Bottega: a studio run by AI agents.")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)
    demo_help = "scripted responses: no API key needed, nothing is billed"

    run = sub.add_parser("run", help="give the whole team a goal, in the terminal")
    run.add_argument("goal", help='e.g. "Build a landing page for a pizzeria in Mantova, Italy"')
    run.add_argument("--demo", action="store_true", help=demo_help)
    run.set_defaults(func=cmd_run)

    serve = sub.add_parser("serve", help="start the dashboard")
    serve.add_argument("--demo", action="store_true", help=demo_help)
    serve.add_argument("--host", default="127.0.0.1",
                       help="address to listen on (default: this computer only)")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    check = sub.add_parser("check", help="check the API key, the model and every agent's setup")
    check.add_argument("--live", action="store_true",
                       help="also send one tiny real request (costs under a cent)")
    check.set_defaults(func=cmd_check)

    solo = sub.add_parser("solo", help="run one agent alone on a goal (no task board)")
    solo.add_argument("role", choices=WORKER_ROLES, help="which agent to run")
    solo.add_argument("goal")
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
    except anthropic.AnthropicError as exc:
        print(explain(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
