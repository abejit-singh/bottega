"""FastAPI server for the Bottega command center.

Two ways the browser talks to us:
  * ordinary JSON requests  -> "give me the current state", "start this project"
  * Server-Sent Events (SSE) -> one long-lived response on which the server
    pushes each event the moment it happens.

Why SSE and not WebSockets? Updates only flow one way (server -> browser), and
SSE is plain HTTP: the browser's built-in `EventSource` reconnects on its own
and tells us the last event it saw. Less code, fewer moving parts.

Security notes:
  * There is no login. The CLI binds to 127.0.0.1 by default so only this
    computer can reach the dashboard (it can spend your API credits!).
  * File and note reads go through the same sandbox the agents use.
  * The frontend never inserts model-written text as HTML (see app.js).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bottega.company import Company, CompanyBusy
from bottega.sandbox import SandboxError
from bottega.web.hub import EventHub

STATIC_DIR = Path(__file__).parent / "static"
PING_SECONDS = 10.0


class ProjectRequest(BaseModel):
    """Body of POST /api/projects. Pydantic validates it before our code runs."""

    goal: str = Field(min_length=3, max_length=2000)
    role: str = "researcher"


def _sse(event: dict[str, Any]) -> str:
    """Format one event in the SSE wire format: `id:` and `data:` lines, blank line."""
    return f"id: {event['id']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def create_app(company: Company, hub: EventHub) -> FastAPI:
    """Build the app around an existing Company (so tests can pass a fake one)."""
    app = FastAPI(title="Bottega", docs_url="/api/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------ state

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        # Read the event counter BEFORE the snapshot. If something changes in
        # between, the browser will simply receive that event again and
        # refresh; reading in the other order could miss an update forever.
        last_event_id = hub.last_id
        return {**company.snapshot(), "last_event_id": last_event_id}

    # --------------------------------------------------------------- projects

    @app.post("/api/projects", status_code=201)
    def start_project(body: ProjectRequest) -> dict[str, Any]:
        try:
            project = company.start_project(body.goal, body.role)
        except CompanyBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return project.to_dict()

    @app.post("/api/projects/{project_id}/stop", status_code=202)
    def stop_project(project_id: str) -> dict[str, bool]:
        if company.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Unknown project.")
        return {"stopping": company.stop_project(project_id)}

    @app.get("/api/projects/{project_id}/file")
    def read_project_file(project_id: str, path: str) -> dict[str, str]:
        project = company.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Unknown project.")
        try:
            return {"path": path, "content": project.workspace.read_text(path)}
        except SandboxError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ----------------------------------------------------------------- memory

    @app.get("/api/memory")
    def list_memory() -> dict[str, Any]:
        return {"notes": [asdict(note) for note in company.memory.notes()]}

    @app.get("/api/memory/note")
    def read_memory_note(path: str) -> dict[str, str]:
        if not path.endswith(".md"):
            raise HTTPException(status_code=404, detail="Not a note.")
        try:
            return {"path": path, "content": company.memory.read(path)}
        except SandboxError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ----------------------------------------------------------------- events

    @app.get("/api/events/history")
    def event_history(after: int = 0) -> dict[str, Any]:
        """Plain-JSON fallback for networks where SSE is buffered or blocked."""
        return {"events": hub.history(after)}

    @app.get("/api/events")
    async def events(request: Request, after: int = 0) -> StreamingResponse:
        # On an automatic reconnect the browser sends the last id it received.
        header = request.headers.get("last-event-id", "")
        after_id = int(header) if header.isdigit() else after
        queue, backlog = hub.subscribe(after_id)

        async def stream() -> AsyncIterator[str]:
            try:
                yield "event: ping\ndata: {}\n\n"  # tells the page the link is up
                for event in backlog:
                    yield _sse(event)
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=PING_SECONDS)
                        yield _sse(event)
                    except asyncio.TimeoutError:
                        yield "event: ping\ndata: {}\n\n"  # heartbeat on a quiet line
            finally:
                # Runs when the browser disconnects (the generator is cancelled).
                hub.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
