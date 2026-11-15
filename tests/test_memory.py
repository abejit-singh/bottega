"""Tests for the company memory vault and its tools."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from bottega.agents import build_agent
from bottega.memory import MemoryVault
from bottega.sandbox import SandboxError
from tests.conftest import FakeClient, make_message, text, tool_use


@pytest.fixture
def vault(tmp_path: Path) -> MemoryVault:
    return MemoryVault(tmp_path / "memory")


def test_save_writes_obsidian_note(vault: MemoryVault) -> None:
    rel = vault.save(folder="lessons", title="Mantova food scene", content="Tortelli di zucca.",
                     tags=["Mantova", "food scene"], author="Researcher", today=date(2026, 11, 15))
    assert rel == "lessons/Mantova food scene.md"
    note = vault.read("Mantova food scene")  # lookup by title, like an Obsidian [[link]]
    assert note.startswith("---\nauthor: Researcher\nupdated: 2026-11-15\ntags: [food-scene, mantova]\n---")
    assert "# Mantova food scene" in note and "Tortelli di zucca." in note


def test_title_is_sanitized_and_folder_restricted(vault: MemoryVault) -> None:
    rel = vault.save(folder="clients", title="Pizza/Napoli: #1?", content="x", tags=[], author="A")
    assert rel == "clients/PizzaNapoli 1.md"
    with pytest.raises(SandboxError, match="Folder"):
        vault.save(folder="../secrets", title="x", content="x", tags=[], author="A")


def test_search_ranks_title_over_body(vault: MemoryVault) -> None:
    vault.save(folder="lessons", title="Pizza pricing", content="Margherita prices.", tags=[], author="A")
    vault.save(folder="lessons", title="Coffee", content="Some cafés also sell pizza.", tags=[], author="A")
    hits = vault.search("pizza")
    assert [h.title for h in hits] == ["Pizza pricing", "Coffee"]
    assert "pizza" in hits[1].snippet.lower()


def test_search_uses_tags_and_ignores_short_words(vault: MemoryVault) -> None:
    vault.save(folder="playbooks", title="Checklist", content="...", tags=["landing-page"], author="A")
    assert vault.search("landing")[0].title == "Checklist"
    assert vault.search("a an") == []


def test_obsidian_config_is_ignored(vault: MemoryVault) -> None:
    vault.workspace.write_text(".obsidian/notes.md", "app config")
    assert vault.list_notes() == []


def test_agent_consults_and_saves_memory(settings, workspace, costs, vault) -> None:
    vault.save(folder="playbooks", title="Landing page checklist", content="Hero first.",
               tags=["landing-page"], author="Founder")
    client = FakeClient([
        make_message([tool_use("search_memory", {"query": "landing page pizzeria"}, "t1")],
                     stop_reason="tool_use"),
        make_message([tool_use("save_memory", {"folder": "clients", "title": "Pizzeria Mantova",
                                               "content": "See [[Landing page checklist]].",
                                               "tags": ["pizzeria"]}, "t2")],
                     stop_reason="tool_use"),
        make_message([text("done")]),
    ])
    agent = build_agent("researcher", client=client, settings=settings, workspace=workspace,
                        costs=costs, memory=vault)
    agent.run("task")

    search_result = client.requests[1]["messages"][-1]["content"][0]["content"]
    assert "Landing page checklist" in search_result
    note = vault.read("Pizzeria Mantova")
    assert "author: Researcher" in note and "[[Landing page checklist]]" in note


def test_memory_tools_fail_gracefully_without_vault(settings, workspace, costs) -> None:
    client = FakeClient([
        make_message([tool_use("search_memory", {"query": "x y z"})], stop_reason="tool_use"),
        make_message([text("ok")]),
    ])
    build_agent("researcher", client=client, settings=settings, workspace=workspace,
                costs=costs).run("task")
    result = client.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "not available" in result["content"]


def test_bad_tags_rejected(settings, workspace, costs, vault) -> None:
    client = FakeClient([
        make_message([tool_use("save_memory", {"folder": "lessons", "title": "t", "content": "c",
                                               "tags": [1, 2]})], stop_reason="tool_use"),
        make_message([text("ok")]),
    ])
    build_agent("researcher", client=client, settings=settings, workspace=workspace,
                costs=costs, memory=vault).run("task")
    assert client.requests[1]["messages"][-1]["content"][0]["is_error"]
    assert vault.list_notes() == []
