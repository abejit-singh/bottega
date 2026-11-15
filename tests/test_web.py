"""Tests for the event hub and the HTTP API (no real server, no real API)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bottega.company import Company
from bottega.config import Settings
from bottega.demo import DemoClient
from bottega.web import EventHub, create_app

# ------------------------------------------------------------------ EventHub


def test_hub_numbers_events_and_replays_history() -> None:
    hub = EventHub()
    hub.publish({"kind": "a"})
    hub.publish({"kind": "b"})
    hub.publish({"kind": "c"})
    assert hub.last_id == 3
    assert [e["kind"] for e in hub.history(after_id=1)] == ["b", "c"]


def test_hub_history_is_bounded() -> None:
    hub = EventHub(history_size=2)
    for kind in "abc":
        hub.publish({"kind": kind})
    assert [e["id"] for e in hub.history()] == [2, 3]


def test_hub_delivers_backlog_then_live_events_across_threads() -> None:
    async def scenario() -> list[str]:
        hub = EventHub()
        hub.publish({"kind": "old"})
        queue, backlog = hub.subscribe(after_id=0)
        # Publish from another thread, like an agent would.
        await asyncio.to_thread(hub.publish, {"kind": "live"})
        live = await asyncio.wait_for(queue.get(), timeout=2)
        hub.unsubscribe(queue)
        hub.publish({"kind": "after-unsubscribe"})
        assert queue.empty()
        return [backlog[0]["kind"], live["kind"]]

    assert asyncio.run(scenario()) == ["old", "live"]


# ------------------------------------------------------------------ HTTP API


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(output_dir=tmp_path / "output", memory_dir=tmp_path / "memory")
    hub = EventHub()
    company = Company(settings, publish=hub.publish,
                      client_factory=lambda: DemoClient(delay=0), demo=True)
    company.memory.save(folder="playbooks", title="Landing page checklist",
                        content="Hero first.", tags=["playbook"], author="Founder")
    return TestClient(create_app(company, hub))


def _wait_until_idle(client: TestClient) -> dict:
    for _ in range(200):
        state = client.get("/api/state").json()
        if not state["busy"]:
            return state
        time.sleep(0.02)
    raise AssertionError("project did not finish")


def test_dashboard_page_and_assets_are_served(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200 and "Bottega" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_initial_state(client: TestClient) -> None:
    state = client.get("/api/state").json()
    assert state["demo"] is True and state["busy"] is False
    assert len(state["agents"]) == 6
    assert state["settings"]["max_cost_usd"] == 1.0
    assert state["last_event_id"] == 0


def test_full_project_flow(client: TestClient) -> None:
    created = client.post("/api/projects", json={"goal": "Landing page for a pizzeria"})
    assert created.status_code == 201
    project_id = created.json()["id"]

    state = _wait_until_idle(client)
    project = state["projects"][0]
    assert project["status"] == "done" and project["files"] == ["research.md"]
    assert state["totals"]["calls"] == 5

    file = client.get(f"/api/projects/{project_id}/file", params={"path": "research.md"})
    assert file.status_code == 200 and "pizzeria" in file.json()["content"]

    events = client.get("/api/events/history").json()["events"]
    assert events[0]["kind"] == "project_start" and events[-1]["kind"] == "project_done"
    assert state["last_event_id"] <= events[-1]["id"]
    # "after" returns only newer events
    assert client.get("/api/events/history", params={"after": events[-1]["id"]}).json() == {"events": []}


def test_memory_endpoints(client: TestClient) -> None:
    notes = client.get("/api/memory").json()["notes"]
    assert [n["title"] for n in notes] == ["Landing page checklist"]
    assert notes[0]["folder"] == "playbooks" and notes[0]["author"] == "Founder"
    note = client.get("/api/memory/note", params={"path": notes[0]["path"]})
    assert "Hero first." in note.json()["content"]


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", "missing.md"])
def test_file_endpoints_cannot_escape_the_sandbox(client: TestClient, path: str) -> None:
    project_id = client.post("/api/projects", json={"goal": "Sandbox check"}).json()["id"]
    _wait_until_idle(client)
    assert client.get(f"/api/projects/{project_id}/file", params={"path": path}).status_code == 404
    assert client.get("/api/memory/note", params={"path": path}).status_code == 404
    assert client.get("/api/memory/note", params={"path": "../secret.md"}).status_code == 404


def test_input_validation_and_errors(client: TestClient) -> None:
    assert client.post("/api/projects", json={"goal": ""}).status_code == 422
    assert client.post("/api/projects", json={"goal": "ok goal", "role": "astronaut"}).status_code == 400
    assert client.post("/api/projects/nope/stop").status_code == 404
    assert client.get("/api/projects/nope/file", params={"path": "a.md"}).status_code == 404
