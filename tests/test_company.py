"""Tests for the Company (project lifecycle), using the scripted DemoClient."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from bottega.company import Company, CompanyBusy
from bottega.config import Settings
from bottega.demo import DemoClient, demo_settings
from tests.conftest import FakeClient, make_message, text


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(output_dir=tmp_path / "output", memory_dir=tmp_path / "memory")


def _company(settings: Settings, events: list[dict[str, Any]], client: Any = None) -> Company:
    factory = (lambda: client) if client is not None else (lambda: DemoClient(delay=0))
    return Company(settings, publish=events.append, client_factory=factory, demo=True)


def test_project_runs_to_completion(settings: Settings) -> None:
    events: list[dict[str, Any]] = []
    company = _company(settings, events)
    project = company.start_project("Build a landing page for a pizzeria")
    company.wait(project.id, timeout=10)

    assert project.status == "done"
    assert project.workspace.list_files() == ["research.md"]
    assert project.new_notes == ["lessons/Demo run notes.md"]
    assert "pizzeria" in project.workspace.read_text("research.md")

    kinds = [e["kind"] for e in events]
    assert kinds[0] == "project_start" and kinds[-1] == "project_done"
    assert "tool_call" in kinds and "usage" in kinds
    assert all(e["project_id"] == project.id for e in events)


def test_snapshot_reports_team_spend_and_projects(settings: Settings) -> None:
    company = _company(settings, [])
    before = company.snapshot()
    assert [a["title"] for a in before["agents"]] == [
        "CEO", "Researcher", "Designer", "Developer", "Marketer", "QA"
    ]
    assert {a["title"]: a["status"] for a in before["agents"]}["CEO"] == "offline"
    assert before["busy"] is False and before["projects"] == []

    project = company.start_project("A goal for the team")
    company.wait(project.id, timeout=10)
    after = company.snapshot()

    researcher = next(a for a in after["agents"] if a["name"] == "researcher")
    assert researcher["status"] == "idle" and researcher["calls"] == 5
    assert after["totals"]["cost_usd"] == pytest.approx(researcher["cost_usd"])
    assert after["totals"]["cost_usd"] > 0
    assert after["projects"][0]["id"] == project.id
    assert after["projects"][0]["files"] == ["research.md"]


def test_only_one_project_at_a_time(settings: Settings) -> None:
    release = threading.Event()

    class BlockingClient(FakeClient):
        def stream(self, **request: Any):  # type: ignore[override]
            release.wait(timeout=10)  # hold the "model call" until the test says go
            return super().stream(**request)

    company = _company(settings, [], client=BlockingClient([make_message([text("done")])]))
    first = company.start_project("First goal")
    assert company.snapshot()["busy"] is True
    with pytest.raises(CompanyBusy):
        company.start_project("Second goal")
    release.set()
    company.wait(first.id, timeout=10)
    assert first.status == "done" and company.snapshot()["busy"] is False


def test_stop_cancels_a_running_project(settings: Settings) -> None:
    release = threading.Event()

    class BlockingClient(FakeClient):
        def stream(self, **request: Any):  # type: ignore[override]
            release.wait(timeout=10)
            return super().stream(**request)

    # The script would loop on tools forever; the stop flag must end it.
    from tests.conftest import tool_use
    loop = [make_message([tool_use("list_files", {}, f"t{i}")], stop_reason="tool_use")
            for i in range(5)]
    events: list[dict[str, Any]] = []
    company = _company(settings, events, client=BlockingClient(loop))
    project = company.start_project("Never-ending goal")
    assert company.stop_project(project.id) is True
    release.set()
    company.wait(project.id, timeout=10)

    assert project.status == "stopped"
    assert company.stop_project(project.id) is False  # nothing left to stop
    assert "stop_requested" in [e["kind"] for e in events]


def test_failures_become_readable_errors(settings: Settings) -> None:
    class BrokenClient:
        beta = messages = property(lambda self: self)

        def stream(self, **request: Any):
            raise RuntimeError("boom")

    events: list[dict[str, Any]] = []
    company = _company(settings, events, client=BrokenClient())
    project = company.start_project("A goal that fails")
    company.wait(project.id, timeout=10)

    assert project.status == "failed"
    assert "boom" in (project.error or "")
    assert events[-1]["kind"] == "project_done" and events[-1]["data"]["status"] == "failed"


def test_rejects_bad_input(settings: Settings) -> None:
    company = _company(settings, [])
    with pytest.raises(ValueError, match="empty"):
        company.start_project("   ")
    with pytest.raises(ValueError, match="Unknown role"):
        company.start_project("goal", role="astronaut")


def test_demo_settings_never_touch_real_folders(tmp_path: Path) -> None:
    real = Settings(output_dir=tmp_path / "output", memory_dir=tmp_path / "memory")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "Handbook.md").write_text("# Handbook")
    demo = demo_settings(real)
    assert demo.output_dir != real.output_dir and demo.memory_dir != real.memory_dir
    assert (demo.memory_dir / "Handbook.md").read_text() == "# Handbook"
