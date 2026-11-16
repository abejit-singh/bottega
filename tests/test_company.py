"""Tests for the Company: project lifecycle, snapshots and history on disk."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from bottega.company import META_FILE, Company, CompanyBusy
from bottega.config import Settings
from bottega.demo import DemoClient, demo_settings

GOAL = "A landing page for a pizzeria in Mantova, Italy"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(output_dir=tmp_path / "output", memory_dir=tmp_path / "memory")


def make(settings: Settings, events: list[dict[str, Any]] | None = None, client: Any = None) -> Company:
    client = client or DemoClient(delay=0)
    return Company(settings, publish=(events if events is not None else []).append,
                   client_factory=lambda: client, demo=True)


class PausedAt(DemoClient):
    """A demo client that waits at one role's first request until released."""

    def __init__(self, role: str) -> None:
        super().__init__(delay=0)
        self.role = role
        self.reached = threading.Event()
        self.release = threading.Event()

    def stream(self, **request: Any):  # type: ignore[override]
        if re.search(rf"You are (?:the )?{self.role} ", request["system"][0]["text"]):
            self.reached.set()
            self.release.wait(timeout=10)
        return super().stream(**request)


def test_a_project_runs_to_completion(settings: Settings) -> None:
    events: list[dict[str, Any]] = []
    company = make(settings, events)
    project = company.start_project(GOAL)
    company.wait(project.id, timeout=30)

    assert project.status == "done" and project.error is None
    assert project.summary.startswith("Delivered for")
    assert project.new_notes == ["lessons/Demo run notes.md"]
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "project_start" and kinds[-1] == "project_done"
    assert {"task", "message", "brief", "tool_call", "usage"} <= set(kinds)
    assert all(e["project_id"] == project.id for e in events)

    detail = project.detail()
    assert [t["status"] for t in detail["tasks"]] == ["done"] * 5
    assert "site/index.html" in detail["files"] and detail["messages"][0]["sender"] == "CEO"
    assert detail["brief"]["goal"] == GOAL and detail["tasks_done"] == detail["tasks_total"] == 5


def test_snapshot_shows_who_is_working_on_what(settings: Settings) -> None:
    client = PausedAt("Researcher")
    company = make(settings, client=client)
    before = company.snapshot()
    assert [a["title"] for a in before["agents"]] == ["CEO", "Researcher", "Designer", "Developer", "Marketer", "QA"]
    assert {a["status"] for a in before["agents"]} == {"idle"} and before["busy"] is False

    project = company.start_project(GOAL)
    assert client.reached.wait(timeout=10)
    during = company.snapshot()
    researcher = next(a for a in during["agents"] if a["name"] == "researcher")
    assert during["busy"] is True
    assert (researcher["status"], researcher["doing"], researcher["task_id"]) == ("working", "working", "T1")
    assert researcher["task_title"] == "Research the business and its audience"
    assert during["projects"][0]["phase"] == "working"

    client.release.set()
    company.wait(project.id, timeout=30)
    after = company.snapshot()
    assert {a["status"] for a in after["agents"]} == {"idle"}
    assert all(a["calls"] > 0 and a["cost_usd"] > 0 for a in after["agents"])
    assert after["totals"]["cost_usd"] == pytest.approx(sum(a["cost_usd"] for a in after["agents"]))
    assert after["projects"][0]["tasks_done"] == 5


def test_only_one_project_at_a_time(settings: Settings) -> None:
    client = PausedAt("CEO")
    company = make(settings, client=client)
    first = company.start_project("First goal")
    with pytest.raises(CompanyBusy):
        company.start_project("Second goal")
    client.release.set()
    company.wait(first.id, timeout=30)
    assert first.status == "done" and company.snapshot()["busy"] is False


def test_stop_and_founder_messages(settings: Settings) -> None:
    client = PausedAt("Researcher")
    events: list[dict[str, Any]] = []
    company = make(settings, events, client)
    project = company.start_project(GOAL)
    assert client.reached.wait(timeout=10)

    company.post_message(project.id, "Please keep it short.")
    assert project.state.messages.all()[-1].sender == "Founder"
    assert company.stop_project(project.id) is True
    client.release.set()
    company.wait(project.id, timeout=30)

    assert project.status == "stopped"
    assert company.stop_project(project.id) is False      # nothing left to stop
    with pytest.raises(ValueError, match="while a project is running"):
        company.post_message(project.id, "Too late")
    assert {"stop_requested", "message"} <= {e["kind"] for e in events}


def test_failures_become_readable_errors(settings: Settings) -> None:
    class WrongKey(DemoClient):
        def stream(self, **request: Any):  # type: ignore[override]
            request_ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.AuthenticationError(
                "invalid x-api-key", response=httpx2.Response(401, request=request_), body=None)

    events: list[dict[str, Any]] = []
    company = make(settings, events, WrongKey(delay=0))
    project = company.start_project(GOAL)
    company.wait(project.id, timeout=30)

    assert project.status == "failed"
    assert project.error == "Authentication failed: set ANTHROPIC_API_KEY in your .env file."
    assert events[-1]["kind"] == "project_done" and events[-1]["data"]["status"] == "failed"


def test_history_is_reloaded_after_a_restart(settings: Settings) -> None:
    company = make(settings)
    project = company.start_project(GOAL)
    company.wait(project.id, timeout=30)

    restarted = make(settings)                       # a new Company on the same folders
    loaded = restarted.get_project(project.id)
    assert loaded is not None and loaded.status == "done" and loaded.summary == project.summary
    assert [t["status"] for t in loaded.detail()["tasks"]] == ["done"] * 5
    assert loaded.costs.total_usd == pytest.approx(project.costs.total_usd)
    assert restarted.snapshot()["totals"]["cost_usd"] == pytest.approx(project.costs.total_usd)
    assert restarted.snapshot()["busy"] is False


def test_a_project_cut_off_by_a_crash_is_shown_as_incomplete(settings: Settings) -> None:
    company = make(settings)
    project = company.start_project(GOAL)
    company.wait(project.id, timeout=30)
    meta_path = project.workspace.root / META_FILE
    meta = json.loads(meta_path.read_text())
    meta_path.write_text(json.dumps({**meta, "status": "running", "finished_at": None}))

    loaded = make(settings).get_project(project.id)
    assert loaded.status == "incomplete" and "Interrupted" in loaded.error


def test_folders_that_are_not_projects_are_ignored(settings: Settings) -> None:
    (settings.output_dir / "holiday-photos").mkdir(parents=True)
    (settings.output_dir / "notes.txt").write_text("hello")
    assert make(settings).snapshot()["projects"] == []


def test_rejects_an_empty_goal(settings: Settings) -> None:
    with pytest.raises(ValueError, match="empty"):
        make(settings).start_project("   ")


def test_demo_settings_never_touch_real_folders(tmp_path: Path) -> None:
    real = Settings(output_dir=tmp_path / "output", memory_dir=tmp_path / "memory")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "Handbook.md").write_text("# Handbook")
    demo = demo_settings(real)
    assert demo.output_dir != real.output_dir and demo.memory_dir != real.memory_dir
    assert (demo.memory_dir / "Handbook.md").read_text() == "# Handbook"
