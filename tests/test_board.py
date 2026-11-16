"""Tests for the task board, the message log and the project brief."""

from __future__ import annotations

from pathlib import Path

import pytest

from bottega.project import BoardError, MessageLog, ProjectBrief, ProjectState, Task, TaskBoard
from bottega.sandbox import SandboxError, Workspace


@pytest.fixture
def board() -> TaskBoard:
    board = TaskBoard(max_revisions=2)
    board.add("Research", "Write research.md", "researcher")            # T1
    board.add("Design", "Write design.md", "designer", ["T1"])          # T2
    board.add("Copy", "Write copy.md", "marketer", ["T1"])              # T3
    board.add("Website", "Build the site", "developer", ["T2", "T3"])   # T4
    return board


def _finish(board: TaskBoard, task_id: str) -> None:
    board.start(task_id)
    board.submit(task_id, "done")
    board.approve(task_id)


def test_only_tasks_with_finished_dependencies_are_ready(board: TaskBoard) -> None:
    assert [t.id for t in board.ready()] == ["T1"]
    _finish(board, "T1")
    assert [t.id for t in board.ready()] == ["T2", "T3"]   # these two can run in parallel
    _finish(board, "T2")
    assert [t.id for t in board.ready()] == ["T3"]         # T4 still waits for T3
    _finish(board, "T3")
    assert [t.id for t in board.ready()] == ["T4"]


def test_a_task_in_review_does_not_unblock_its_dependents(board: TaskBoard) -> None:
    board.start("T1")
    board.submit("T1", "done")
    assert board.ready() == []          # T1 is waiting for QA, not done
    assert [t.id for t in board.awaiting_review()] == ["T1"]


def test_one_task_per_owner_and_busy_owners_are_skipped(board: TaskBoard) -> None:
    board.add("More copy", "Email", "marketer", ["T1"])   # T5: same owner as T3
    _finish(board, "T1")
    assert [t.id for t in board.ready()] == ["T2", "T3"]                  # not T5: one seat per role
    assert [t.id for t in board.ready(busy_owners={"designer"})] == ["T3"]


def test_dependencies_must_already_exist() -> None:
    board = TaskBoard()
    with pytest.raises(BoardError, match="Unknown dependency T9"):
        board.add("Site", "Build it", "developer", ["T9"])
    assert board.tasks() == []


def test_revision_loop_is_limited(board: TaskBoard) -> None:
    for round_number in (1, 2):
        board.start("T1")
        board.submit("T1", "attempt")
        task = board.request_revision("T1", f"fix {round_number}")
        assert task.status == "todo" and task.revisions == round_number
    board.start("T1")
    board.submit("T1", "attempt 3")
    assert not board.can_revise("T1")
    with pytest.raises(BoardError, match="limit"):
        board.request_revision("T1", "again")
    done = board.approve("T1", "accepted with remarks")
    assert done.status == "done" and done.feedback == ["fix 1", "fix 2"] and done.attempts == 3


def test_illegal_moves_are_refused(board: TaskBoard) -> None:
    with pytest.raises(BoardError, match="cannot be submitted"):
        board.submit("T1", "never started")
    with pytest.raises(BoardError, match="not waiting for review"):
        board.request_revision("T1", "nope")
    with pytest.raises(BoardError, match="no task"):
        board.start("T99")


def test_a_failure_blocks_everything_that_depends_on_it(board: TaskBoard) -> None:
    board.fail("T1", "boom")
    blocked = board.fail_blocked()
    assert [t.id for t in blocked] == ["T2", "T3", "T4"]   # T4 is blocked through T2/T3
    assert "depends on T1" in board.get("T2").error
    assert board.is_settled()


def test_readers_get_copies_not_live_tasks(board: TaskBoard) -> None:
    copy = board.get("T1")
    copy.status = "done"
    copy.depends_on.append("T2")
    assert board.get("T1").status == "todo" and board.get("T1").depends_on == []


def test_listener_hears_every_change() -> None:
    heard: list[tuple[str, str]] = []
    board = TaskBoard(on_change=lambda change, task: heard.append((change, task.status)))
    board.add("Research", "x", "researcher")
    board.start("T1")
    board.release("T1")
    assert heard == [("created", "todo"), ("started", "in_progress"), ("released", "todo")]


def test_board_is_full_at_ten_tasks() -> None:
    board = TaskBoard()
    for i in range(10):
        board.add(f"Task {i}", "x", "researcher")
    with pytest.raises(BoardError, match="full"):
        board.add("One too many", "x", "researcher")


def test_render_shows_status_dependencies_and_results(board: TaskBoard) -> None:
    _finish(board, "T1")
    text = board.render()
    assert "T1 [done] Research — owner: researcher" in text
    assert "T4 [todo] Website — owner: developer (after T2, T3)" in text
    assert "delivered: done" in text


def test_messages_reach_the_right_readers() -> None:
    log = MessageLog()
    log.post("CEO", "all", "Kickoff")
    log.post("Designer", "Developer", "Use the tokens in the brief")
    log.post("Developer", "all", "Site is up")
    assert [m.text for m in log.for_recipient("Developer")] == ["Kickoff", "Use the tokens in the brief"]
    assert [m.text for m in log.for_recipient("Marketer")] == ["Kickoff", "Site is up"]
    assert "Designer to Developer" in log.render("Developer")
    with pytest.raises(ValueError):
        log.post("CEO", "all", "   ")


def test_brief_is_saved_and_validated(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path, reserved=("project_brief.json",))
    brief = ProjectBrief(workspace, "p1", "A landing page")
    brief.set_note("tone_of_voice", "Warm and direct", "Researcher")
    assert '"tone_of_voice"' in workspace.read_text("project_brief.json")
    assert "tone_of_voice (by Researcher):\nWarm and direct" in brief.render()
    with pytest.raises(ValueError, match="snake_case"):
        brief.set_note("Tone Of Voice!", "x", "Researcher")
    # Agents cannot overwrite the brief or the bookkeeping folder with write_file.
    with pytest.raises(SandboxError, match="dedicated tool"):
        workspace.write_text("project_brief.json", "{}")
    with pytest.raises(SandboxError, match="bookkeeping"):
        workspace.write_text(".bottega/state.json", "{}")


def test_state_survives_a_save_and_load(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path, reserved=("project_brief.json",))
    state = ProjectState(TaskBoard(), MessageLog(), ProjectBrief(workspace, "p1", "Goal"))
    state.brief.save()
    state.board.add("Research", "x", "researcher")
    state.messages.post("CEO", "all", "Hello")
    state.final_summary = "All good"
    state.save(workspace)

    loaded = ProjectState.load(workspace)
    assert [t.title for t in loaded.board.tasks()] == ["Research"]
    assert isinstance(loaded.board.get("T1"), Task)
    assert loaded.messages.all()[0].text == "Hello"
    assert loaded.final_summary == "All good" and loaded.phase == "closed"
    assert ".bottega/state.json" not in workspace.list_files()   # hidden from agents
