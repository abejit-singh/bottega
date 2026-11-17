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

GOAL = {"goal": "A landing page for a pizzeria in Mantova, Italy"}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(output_dir=tmp_path / "output", memory_dir=tmp_path / "memory")
    hub = EventHub()
    company = Company(settings, publish=hub.publish,
                      client_factory=lambda: DemoClient(delay=0), demo=True)
    company.memory.save(folder="playbooks", title="Landing page checklist",
                        content="Hero first.", tags=["playbook"], author="Founder")
    return TestClient(create_app(company, hub))


def run_project(client: TestClient) -> str:
    """Start the demo project and wait for the team to finish it."""
    created = client.post("/api/projects", json=GOAL)
    assert created.status_code == 201
    for _ in range(500):
        if not client.get("/api/state").json()["busy"]:
            return created.json()["id"]
        time.sleep(0.02)
    raise AssertionError("project did not finish")


def test_dashboard_page_and_assets_are_served(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200 and "Bottega" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_initial_state(client: TestClient) -> None:
    state = client.get("/api/state").json()
    assert state["demo"] is True and state["busy"] is False and state["projects"] == []
    assert [a["name"] for a in state["agents"]] == ["ceo", "researcher", "designer", "developer", "marketer", "qa"]
    assert state["settings"]["max_cost_usd"] == 1.0 and state["settings"]["max_revisions"] == 2
    assert state["last_event_id"] == 0


def test_full_project_flow(client: TestClient) -> None:
    project_id = run_project(client)

    state = client.get("/api/state").json()
    assert state["projects"][0]["status"] == "done" and state["projects"][0]["tasks_done"] == 5
    assert state["totals"]["calls"] == 50

    detail = client.get(f"/api/projects/{project_id}").json()
    assert [t["id"] for t in detail["tasks"]] == ["T1", "T2", "T3", "T4", "T5"]
    assert detail["tasks"][3]["revisions"] == 1 and "lang attribute" in detail["tasks"][3]["feedback"][0]
    assert detail["messages"][0]["sender"] == "CEO" and "design_tokens" in detail["brief"]["notes"]
    assert "site/index.html" in detail["files"] and detail["summary"].startswith("Delivered for")

    file = client.get(f"/api/projects/{project_id}/file", params={"path": "research.md"})
    assert file.status_code == 200 and "pizzeria" in file.json()["content"]

    events = client.get("/api/events/history").json()["events"]
    assert events[0]["kind"] == "project_start" and events[-1]["kind"] == "project_done"
    assert client.get("/api/events/history", params={"after": events[-1]["id"]}).json() == {"events": []}


def test_generated_pages_are_previewed_in_a_sandbox(client: TestClient) -> None:
    project_id = run_project(client)
    page = client.get(f"/api/projects/{project_id}/preview/site/index.html")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert '<link rel="stylesheet" href="style.css">' in page.text
    # The page is model-written: it must not run with the dashboard's authority.
    assert page.headers["content-security-policy"] == "sandbox allow-scripts"
    css = client.get(f"/api/projects/{project_id}/preview/site/style.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")


def test_memory_endpoints(client: TestClient) -> None:
    notes = client.get("/api/memory").json()["notes"]
    assert [n["title"] for n in notes] == ["Landing page checklist"]
    assert notes[0]["folder"] == "playbooks" and notes[0]["author"] == "Founder"
    note = client.get("/api/memory/note", params={"path": notes[0]["path"]})
    assert "Hero first." in note.json()["content"]


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", "missing.md", ".bottega/state.json"])
def test_file_endpoints_cannot_escape_the_sandbox(client: TestClient, path: str) -> None:
    project_id = run_project(client)
    assert client.get(f"/api/projects/{project_id}/file", params={"path": path}).status_code == 404
    assert client.get(f"/api/projects/{project_id}/preview/{path}").status_code == 404
    assert client.get("/api/memory/note", params={"path": path}).status_code == 404


def test_input_validation_and_errors(client: TestClient) -> None:
    assert client.post("/api/projects", json={"goal": ""}).status_code == 422
    assert client.post("/api/projects/nope/stop").status_code == 404
    assert client.get("/api/projects/nope").status_code == 404
    assert client.get("/api/projects/nope/file", params={"path": "a.md"}).status_code == 404
    project_id = run_project(client)
    late = client.post(f"/api/projects/{project_id}/messages", json={"text": "Hello team"})
    assert late.status_code == 409 and "while a project is running" in late.json()["detail"]


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site"},                 # another website, or a sandboxed preview
    {"Origin": "https://evil.example"},               # older browsers: no Sec-Fetch-Site
])
def test_other_websites_cannot_start_projects(client: TestClient, headers: dict[str, str]) -> None:
    refused = client.post("/api/projects", json=GOAL, headers=headers)
    assert refused.status_code == 403
    assert client.get("/api/state").json()["projects"] == []
    # Reading is still allowed, and the dashboard's own requests pass.
    assert client.get("/api/state", headers=headers).status_code == 200
    assert client.post("/api/projects", json=GOAL, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 201


def test_download_all_files_as_a_zip(client: TestClient) -> None:
    import io
    import zipfile

    project_id = run_project(client)
    response = client.get(f"/api/projects/{project_id}/download")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    assert f'filename="{project_id}.zip"' in response.headers["content-disposition"]
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert f"{project_id}/site/index.html" in names and f"{project_id}/research.md" in names
    assert not any(".bottega" in name for name in names)   # bookkeeping stays private
