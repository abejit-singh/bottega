"""Tests for the orchestrator: the whole team working on a project.

The scripted DemoClient plays all six agents, so these tests exercise the real
orchestrator, board, tools and agent loop without calling the API.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from bottega.config import Settings
from bottega.costs import CostTracker
from bottega.demo import DemoClient
from bottega.memory import MemoryVault
from bottega.orchestrator import Orchestrator, OrchestratorError
from bottega.project import BRIEF_FILE
from bottega.sandbox import Workspace

GOAL = "A landing page for a pizzeria in Mantova, Italy"
Event = tuple[str, str, dict[str, Any]]


def sent_to(role: str, request: dict[str, Any]) -> bool:
    """Was this API request made by the agent with this role?"""
    return re.search(rf"You are (?:the )?{role} ", request["system"][0]["text"]) is not None


class RecordingClient(DemoClient):
    """The demo client, plus a record of every request it was sent."""

    def __init__(self) -> None:
        super().__init__(delay=0)
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def stream(self, **request: Any):  # type: ignore[override]
        with self._lock:
            self.requests.append({**request, "messages": list(request["messages"])})
        return super().stream(**request)

    def first_prompts(self, role: str) -> list[str]:
        """The task message of every run of one role."""
        return [r["messages"][0]["content"] for r in self.requests
                if sent_to(role, r) and len(r["messages"]) == 1]


def make(tmp_path: Path, client: Any = None, **settings: Any) -> tuple[Orchestrator, list[Event]]:
    config = Settings(output_dir=tmp_path / "output", memory_dir=tmp_path / "memory",
                      **{"max_cost_usd": 50.0, **settings})
    events: list[Event] = []
    orchestrator = Orchestrator(
        project_id="p1", goal=GOAL,
        workspace=Workspace(tmp_path / "output" / "p1", reserved=(BRIEF_FILE,)),
        settings=config, client=client or DemoClient(delay=0), costs=CostTracker(),
        memory=MemoryVault(tmp_path / "memory"),
        emit=lambda agent, kind, data: events.append((agent, kind, data)),
    )
    return orchestrator, events


def changes(events: list[Event]) -> list[tuple[str, str]]:
    return [(d["change"], d["task"]["id"]) for _, kind, d in events if kind == "task"]


def test_the_team_delivers_the_whole_project(tmp_path: Path) -> None:
    orchestrator, events = make(tmp_path)
    outcome = orchestrator.run()

    assert outcome.status == "done" and outcome.summary.startswith("Delivered for")
    tasks = {t.id: t for t in orchestrator.state.board.tasks()}
    assert [t.status for t in tasks.values()] == ["done"] * 5
    assert orchestrator.workspace.list_files() == [
        "copy.md", "design.md", "marketing/outreach_email.md", "marketing/social_posts.md",
        "project_brief.json", "research.md", "site/index.html", "site/style.css",
    ]
    # Shared memory: the brief collected decisions from three different agents.
    notes = orchestrator.state.brief.to_dict()["notes"]
    assert {notes[k]["by"] for k in notes} == {"CEO", "Researcher", "Designer"}
    assert [m.sender for m in orchestrator.state.messages.all()] == ["CEO"]
    assert orchestrator.activity() == {}   # every seat is free again


def test_work_respects_dependencies(tmp_path: Path) -> None:
    orchestrator, events = make(tmp_path)
    orchestrator.run()
    order = changes(events)
    # Nothing starts before the task it depends on has been approved by QA.
    assert order.index(("approved", "T1")) < order.index(("started", "T2"))
    assert order.index(("approved", "T1")) < order.index(("started", "T3"))
    assert order.index(("approved", "T2")) < order.index(("started", "T4"))
    assert order.index(("approved", "T3")) < order.index(("started", "T4"))
    assert order.index(("approved", "T3")) < order.index(("started", "T5"))


def test_qa_sends_work_back_and_the_owner_fixes_it(tmp_path: Path) -> None:
    client = RecordingClient()
    orchestrator, events = make(tmp_path, client)
    orchestrator.run()

    website = orchestrator.state.board.get("T4")
    assert website.status == "done" and website.revisions == 1 and website.attempts == 2
    assert "lang attribute" in website.feedback[0]
    assert [c for c in changes(events) if c[1] == "T4"] == [
        ("created", "T4"), ("started", "T4"), ("submitted", "T4"), ("revision", "T4"),
        ("started", "T4"), ("submitted", "T4"), ("approved", "T4"),
    ]
    # The second time, the Developer was shown QA's feedback, and the fix is on disk.
    first, second = client.first_prompts("Developer")
    assert "QA asked for changes" not in first
    assert "QA asked for changes (revision 1 of 2)" in second and "lang attribute" in second
    assert '<html lang="en">' in orchestrator.workspace.read_text("site/index.html")


def test_revisions_stop_at_the_limit(tmp_path: Path) -> None:
    class NeverSatisfied(RecordingClient):
        def qa_verdict(self, owner: str, revisions: int) -> tuple[str, str]:
            return "revise", "1. Make it better."

    client = NeverSatisfied()
    orchestrator, events = make(tmp_path, client, max_revisions=2)
    outcome = orchestrator.run()

    assert outcome.status == "done"
    for task in orchestrator.state.board.tasks():
        assert task.status == "done" and task.revisions == 2 and task.attempts == 3
    # On the last review QA was told it must approve, and its "revise" was refused.
    final_reviews = [p for p in client.first_prompts("QA") if "This is the final review" in p]
    assert len(final_reviews) == 5
    refused = [d for _, kind, d in events
               if kind == "tool_result" and d["tool"] == "review_task" and not d["ok"]]
    assert len(refused) == 5 and "limit" in refused[0]["output"]


def test_every_agent_sees_the_brief_board_and_messages(tmp_path: Path) -> None:
    client = RecordingClient()
    orchestrator, _ = make(tmp_path, client)
    orchestrator.post_founder_message("Keep everything in Italian.")
    orchestrator.run()

    prompt = client.first_prompts("Developer")[0]
    assert "# Your task: T4 — Build the landing page" in prompt
    assert f"Client goal: {GOAL}" in prompt                       # the brief
    assert "design_tokens (by Designer)" in prompt                # a colleague's decision
    assert "T2 [done] Design system for the landing page" in prompt   # the board
    assert "Founder to everyone: Keep everything in Italian." in prompt
    assert "- design.md" in prompt and "- copy.md" in prompt      # the files
    assert 'task_id "T4"' in prompt                               # how to hand in


def test_the_budget_stops_the_project(tmp_path: Path) -> None:
    client = RecordingClient()
    orchestrator, _ = make(tmp_path, client, max_cost_usd=0.12)
    outcome = orchestrator.run()

    assert outcome.status == "over_budget" and "$0.12" in outcome.summary
    assert not orchestrator.state.board.is_settled()
    # It stopped soon after crossing the limit instead of running all ~50 calls.
    assert orchestrator.costs.total_usd < 0.20 and len(client.requests) < 25
    assert not any(t.status == "in_progress" for t in orchestrator.state.board.tasks())


def test_the_stop_button_ends_the_project(tmp_path: Path) -> None:
    class StopDuringResearch(DemoClient):
        cancel: threading.Event

        def stream(self, **request: Any):  # type: ignore[override]
            if sent_to("Researcher", request):
                self.cancel.set()
            return super().stream(**request)

    client = StopDuringResearch(delay=0)
    orchestrator, _ = make(tmp_path, client)
    client.cancel = orchestrator.cancel
    outcome = orchestrator.run()

    assert outcome.status == "stopped"
    research = orchestrator.state.board.get("T1")
    assert research.status == "todo"   # put back in the queue, not lost or half-done
    assert orchestrator.activity() == {}


class FailingClient(DemoClient):
    """Raises `error` for one role, the first `times` requests."""

    def __init__(self, role: str, error: Exception, times: int = 10**6) -> None:
        super().__init__(delay=0)
        self.role, self.error, self.left = role, error, times

    def stream(self, **request: Any):  # type: ignore[override]
        if sent_to(self.role, request) and self.left > 0:
            self.left -= 1
            raise self.error
        return super().stream(**request)


_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def test_a_failed_task_blocks_only_what_depends_on_it(tmp_path: Path) -> None:
    orchestrator, _ = make(tmp_path, FailingClient("Designer", RuntimeError("boom")))
    outcome = orchestrator.run()

    status = {t.id: t.status for t in orchestrator.state.board.tasks()}
    assert status == {"T1": "done", "T2": "failed", "T3": "done", "T4": "failed", "T5": "done"}
    assert "boom" in orchestrator.state.board.get("T2").error
    assert "depends on T2" in orchestrator.state.board.get("T4").error
    assert outcome.status == "incomplete" and "T2, T4" in outcome.summary


def test_a_temporary_error_is_retried(tmp_path: Path) -> None:
    hiccup = anthropic.APIConnectionError(request=_REQUEST)
    orchestrator, events = make(tmp_path, FailingClient("Researcher", hiccup, times=1))
    outcome = orchestrator.run()

    assert outcome.status == "done"
    assert orchestrator.state.board.get("T1").attempts == 2
    assert ("released", "T1") in changes(events)


def test_a_temporary_error_that_keeps_happening_fails_the_task(tmp_path: Path) -> None:
    hiccup = anthropic.APIConnectionError(request=_REQUEST)
    orchestrator, _ = make(tmp_path, FailingClient("Researcher", hiccup), task_retries=1)
    outcome = orchestrator.run()

    research = orchestrator.state.board.get("T1")
    assert research.status == "failed" and research.attempts == 2
    assert "Could not reach the Claude API" in research.error
    assert outcome.status == "incomplete"


def test_a_wrong_api_key_stops_everything_at_once(tmp_path: Path) -> None:
    bad_key = anthropic.AuthenticationError(
        "invalid x-api-key", response=httpx2.Response(401, request=_REQUEST), body=None)
    orchestrator, _ = make(tmp_path, FailingClient("CEO", bad_key))
    with pytest.raises(anthropic.AuthenticationError):
        orchestrator.run()
    assert orchestrator.state.phase == "closed"


def test_a_broken_reviewer_does_not_block_delivery(tmp_path: Path) -> None:
    orchestrator, _ = make(tmp_path, FailingClient("QA", RuntimeError("qa crashed")))
    outcome = orchestrator.run()

    assert outcome.status == "done"
    assert "Not reviewed" in orchestrator.state.board.get("T1").review_note


def test_no_plan_means_no_project(tmp_path: Path) -> None:
    class LazyCeo(DemoClient):
        @staticmethod
        def _ceo_plan(goal: str):  # type: ignore[override]
            return [([{"type": "text", "text": "I'd rather not."}], 100, 10)]

    orchestrator, _ = make(tmp_path, LazyCeo(delay=0))
    with pytest.raises(OrchestratorError, match="did not create any tasks"):
        orchestrator.run()
