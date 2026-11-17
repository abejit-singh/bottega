"""FastAPI server for the Bottega dashboard.

Two ways the browser talks to us:
  * ordinary JSON requests  -> "give me the current state", "start this project"
  * Server-Sent Events (SSE) -> one long-lived response on which the server
    pushes each event the moment it happens.

Why SSE and not WebSockets? Updates only flow one way (server -> browser), and
SSE is plain HTTP: the browser's built-in `EventSource` reconnects on its own
and tells us the last event it saw. Less code, fewer moving parts.

Security notes (the dashboard has no login and can spend your API credits):
  * The CLI binds to 127.0.0.1 by default, so only this computer can reach it.
  * Requests that change something must come from the dashboard page itself.
    Another website open in your browser cannot start projects (see
    `same_site_only`).
  * Pages written by agents are untrusted. The preview serves them with a
    `sandbox` policy, so their scripts cannot talk to this server as you.
  * File and note reads go through the same sandbox the agents use.
  * The frontend never inserts model-written text as HTML (see app.js).
"""

from __future__ import annotations

import asyncio
import io
import json
import mimetypes
import re
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bottega.company import Company, CompanyBusy, Project
from bottega.sandbox import INTERNAL_DIR, SandboxError
from bottega.web.hub import EventHub

STATIC_DIR = Path(__file__).parent / "static"
PING_SECONDS = 10.0


class ProjectRequest(BaseModel):
    """Body of POST /api/projects. Pydantic validates it before our code runs."""

    goal: str = Field(min_length=3, max_length=2000)


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1500)


def _sse(event: dict[str, Any]) -> str:
    """Format one event in the SSE wire format: `id:` and `data:` lines, blank line."""
    return f"id: {event['id']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def create_app(company: Company, hub: EventHub) -> FastAPI:
    """Build the app around an existing Company (so tests can pass a fake one)."""
    app = FastAPI(title="Bottega", docs_url="/api/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def same_site_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Refuse state-changing requests that don't come from our own page.

        Browsers label every request with where it came from (`Sec-Fetch-Site`,
        or `Origin` in older ones), and page scripts cannot fake those headers.
        Without this check, any website you visit could quietly POST to
        localhost and start projects with your API key. Tools like curl send
        neither header and are allowed: they are not a browser being tricked.
        """
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            site = request.headers.get("sec-fetch-site")
            origin = request.headers.get("origin")
            if site is not None:
                allowed = site in ("same-origin", "none")
            else:
                allowed = origin is None or urlparse(origin).netloc == request.headers.get("host")
            if not allowed:
                return JSONResponse({"detail": "Cross-site requests are not allowed."}, 403)
        return await call_next(request)

    def project_or_404(project_id: str) -> Project:
        project = company.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Unknown project.")
        return project

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
            project = company.start_project(body.goal)
        except CompanyBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return project.to_dict()

    @app.get("/api/projects/{project_id}")
    def project_detail(project_id: str) -> dict[str, Any]:
        """Board, messages, brief and files of one project."""
        return project_or_404(project_id).detail()

    @app.post("/api/projects/{project_id}/stop", status_code=202)
    def stop_project(project_id: str) -> dict[str, bool]:
        project_or_404(project_id)
        return {"stopping": company.stop_project(project_id)}

    @app.post("/api/projects/{project_id}/messages", status_code=201)
    def post_message(project_id: str, body: MessageRequest) -> dict[str, bool]:
        project_or_404(project_id)
        try:
            company.post_message(project_id, body.text)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"posted": True}

    @app.get("/api/projects/{project_id}/file")
    def read_project_file(project_id: str, path: str) -> dict[str, str]:
        """A deliverable as text, for the code viewer."""
        project = project_or_404(project_id)
        if path not in project.workspace.list_files():   # also hides the bookkeeping folder
            raise HTTPException(status_code=404, detail="File does not exist.")
        try:
            return {"path": path, "content": project.workspace.read_text(path)}
        except SandboxError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/projects/{project_id}/download")
    def download_project(project_id: str) -> Response:
        """Every deliverable in one zip file (Bottega's bookkeeping is left out)."""
        project = project_or_404(project_id)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in project.workspace.list_files():
                archive.write(project.workspace.resolve(path), f"{project.id}/{path}")
        name = re.sub(r"[^A-Za-z0-9._-]", "-", project.id)
        return Response(buffer.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})

    @app.get("/api/projects/{project_id}/preview/{path:path}")
    def preview_project_file(project_id: str, path: str) -> FileResponse:
        """A deliverable served raw, so a generated web page can be shown in a frame."""
        project = project_or_404(project_id)
        try:
            target = project.workspace.resolve(path)
        except SandboxError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not target.is_file() or INTERNAL_DIR in target.relative_to(project.workspace.root).parts:
            raise HTTPException(status_code=404, detail="File does not exist.")
        return FileResponse(
            target,
            media_type=mimetypes.guess_type(target.name)[0] or "text/plain",
            headers={
                # The page was written by a model: treat it like a stranger's
                # website. `sandbox` gives it a throwaway origin, so even its
                # own scripts cannot call this API with the user's authority.
                "Content-Security-Policy": "sandbox allow-scripts",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

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
                    except TimeoutError:
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
