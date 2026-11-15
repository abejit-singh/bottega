"""The sandbox is a security boundary, so it gets the most adversarial tests."""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from bottega.tools import SandboxError, Workspace, new_project_id


def test_write_and_read_nested_file(workspace: Workspace) -> None:
    workspace.write_text("site/css/style.css", "body {}")
    assert workspace.read_text("site/css/style.css") == "body {}"
    assert workspace.list_files() == ["site/css/style.css"]


@pytest.mark.parametrize(
    "bad_path",
    ["../escape.txt", "site/../../escape.txt", "/etc/passwd", "", "   ", "a\x00b"],
)
def test_rejects_paths_outside_sandbox(workspace: Workspace, bad_path: str) -> None:
    with pytest.raises(SandboxError):
        workspace.write_text(bad_path, "nope")


def test_rejects_symlink_escape(workspace: Workspace, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, workspace.root / "link")
    with pytest.raises(SandboxError):
        workspace.write_text("link/evil.txt", "nope")
    assert not (outside / "evil.txt").exists()


def test_rejects_huge_files(workspace: Workspace) -> None:
    with pytest.raises(SandboxError, match="too large"):
        workspace.write_text("big.txt", "x" * 1_000_000)


def test_read_missing_file(workspace: Workspace) -> None:
    with pytest.raises(SandboxError, match="does not exist"):
        workspace.read_text("nope.md")


def test_project_id_is_readable_slug() -> None:
    pid = new_project_id("Build a landing page for a pizzeria in Mantova, Italy!",
                         now=datetime(2026, 11, 15, 21, 30, 0))
    assert pid.startswith("20261115-213000-build-a-landing-page")
    assert all(c.isalnum() or c == "-" for c in pid)
