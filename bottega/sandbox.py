"""The sandbox: a directory that agents are allowed to read and write.

Security decision: an LLM decides which paths to write to, and LLM output must be
treated as untrusted input. Without a sandbox, a confused (or prompt-injected)
agent could write `../../.bashrc` or read `/etc/passwd`. So every path an agent
gives us goes through `Workspace.resolve()`, which guarantees the final path is
inside `output/<project_id>/` — even with `..` segments or symlinks.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path, PurePosixPath

MAX_FILE_BYTES = 300_000  # generous for HTML/CSS/markdown, small enough to stop runaway output


class SandboxError(Exception):
    """Raised when a path or file operation would break the sandbox rules."""


def new_project_id(goal: str, now: datetime | None = None) -> str:
    """Readable, unique-enough id: `20261115-213000-build-a-landing-page`."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")[:40].rstrip("-")
    return f"{stamp}-{slug or 'project'}"


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative_path: str) -> Path:
        """Turn an agent-supplied relative path into a safe absolute path."""
        if not relative_path or not relative_path.strip():
            raise SandboxError("Path must not be empty.")
        if "\x00" in relative_path:
            raise SandboxError("Path contains a null byte.")
        if PurePosixPath(relative_path).is_absolute() or Path(relative_path).is_absolute():
            raise SandboxError(f"Use a path relative to the project folder, not {relative_path!r}.")

        # .resolve() collapses `..` and follows symlinks, so the check below
        # catches both `../escape.txt` and a symlink pointing outside the root.
        candidate = (self.root / relative_path).resolve()
        if not candidate.is_relative_to(self.root):
            raise SandboxError(f"Path {relative_path!r} escapes the project folder.")
        return candidate

    def write_text(self, relative_path: str, content: str) -> Path:
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise SandboxError(f"File too large ({len(data)} bytes, limit {MAX_FILE_BYTES}).")
        path = self.resolve(relative_path)
        if path == self.root or path.is_dir():
            raise SandboxError(f"{relative_path!r} is a directory.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def read_text(self, relative_path: str) -> str:
        path = self.resolve(relative_path)
        if not path.is_file():
            raise SandboxError(f"File {relative_path!r} does not exist.")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise SandboxError(f"File {relative_path!r} is too large to read.")
        return path.read_text(encoding="utf-8", errors="replace")

    def list_files(self) -> list[str]:
        """All files in the workspace as sorted POSIX-style relative paths."""
        return sorted(
            p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()
        )

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()
