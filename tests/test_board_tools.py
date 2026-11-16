"""Tests for the teamwork tools and for who is allowed to use what."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bottega.agents import ROLES, build_agent
from bottega.project import BRIEF_FILE, MessageLog, ProjectBrief, ProjectState, TaskBoard
from bottega.sandbox import Workspace
from bottega.tools import BOARD_TOOL_NAMES, TOOL_REGISTRY, ToolContext, ToolError


@pytest.fixture
def state(tmp_path: Path) -> ProjectState:
    workspace = Workspace(tmp_path, reserved=(BRIEF_FILE,))
    return ProjectState(TaskBoard(max_revisions=1), MessageLog(), ProjectBrief(workspace, "p1", "Goal"))


def call(tool: str, state: ProjectState, tmp_path: Path, agent: str = "CEO",
         working_on: str | None = None, /, **args: Any) -> str:
    """Call a tool as `agent`, whose current task is `working_on`."""
    ctx = ToolContext(agent_name=agent, workspace=Workspace(tmp_path, reserved=(BRIEF_FILE,)),
                      role=agent.lower(), project=state, task_id=working_on)
    spec = TOOL_REGISTRY[tool]
    return spec.handler(ctx, spec.validate(args))


def test_ceo_creates_tasks_with_dependencies(state: ProjectState, tmp_path: Path) -> None:
    call("create_task", state, tmp_path, title="Research", description="Write research.md", owner="researcher")
    result = call("create_task", state, tmp_path, title="Design", description="Write design.md",
                  owner="designer", depends_on=["T1"])
    assert result == "Created T2: Design (owner: designer, after T1)."
    assert state.board.get("T2").depends_on == ["T1"]


@pytest.mark.parametrize(("args", "message"), [
    ({"owner": "qa"}, "never create tasks for QA"),
    ({"owner": "ceo"}, "Owner must be one of"),
    ({"depends_on": ["T7"]}, "Unknown dependency T7"),
    ({"title": "   "}, "needs both a title"),
])
def test_bad_tasks_are_refused_with_a_useful_message(
    state: ProjectState, tmp_path: Path, args: dict[str, Any], message: str
) -> None:
    task = {"title": "Site", "description": "Build it", "owner": "developer", **args}
    with pytest.raises(ToolError, match=message):
        call("create_task", state, tmp_path, **task)
    assert state.board.tasks() == []


def test_tasks_can_only_be_added_while_planning_or_once_at_the_end(
    state: ProjectState, tmp_path: Path
) -> None:
    task = {"title": "Site", "description": "Build it", "owner": "developer"}
    state.phase = "working"
    with pytest.raises(ToolError, match="can't be added now"):
        call("create_task", state, tmp_path, **task)
    state.phase = "wrap_up"
    call("create_task", state, tmp_path, **task)        # one follow-up round is allowed
    state.follow_ups_left = 0
    with pytest.raises(ToolError, match="can't be added now"):
        call("create_task", state, tmp_path, **task)


def test_a_worker_can_only_hand_in_its_own_task(state: ProjectState, tmp_path: Path) -> None:
    state.board.add("Research", "x", "researcher")
    state.board.add("Copy", "x", "marketer")
    state.board.start("T1")
    state.board.start("T2")
    with pytest.raises(ToolError, match=r"only update the task you are working on \(T1\)"):
        call("update_task", state, tmp_path, "Researcher", "T1", task_id="T2", status="done", output="x")
    call("update_task", state, tmp_path, "Researcher", "T1", task_id="T1", status="done", output="Wrote research.md")
    assert state.board.get("T1").status == "review"      # not "done": QA decides that
    call("update_task", state, tmp_path, "Marketer", "T2", task_id="T2", status="blocked", output="No research")
    assert state.board.get("T2").status == "failed" and state.board.get("T2").error == "No research"


def test_qa_verdicts(state: ProjectState, tmp_path: Path) -> None:
    state.board.add("Research", "x", "researcher")
    state.board.start("T1")
    state.board.submit("T1", "done")
    with pytest.raises(ToolError, match="only give a verdict on that task"):
        call("review_task", state, tmp_path, "QA", "T2", task_id="T1", verdict="approve")
    with pytest.raises(ToolError, match="specific feedback"):
        call("review_task", state, tmp_path, "QA", "T1", task_id="T1", verdict="revise")
    call("review_task", state, tmp_path, "QA", "T1", task_id="T1", verdict="revise", feedback="1. Add sources.")
    assert state.board.get("T1").status == "todo" and state.board.get("T1").revisions == 1

    state.board.start("T1")
    state.board.submit("T1", "done again")
    with pytest.raises(ToolError, match="limit"):          # max_revisions is 1 in this fixture
        call("review_task", state, tmp_path, "QA", "T1", task_id="T1", verdict="revise", feedback="More.")
    call("review_task", state, tmp_path, "QA", "T1", task_id="T1", verdict="approve", feedback="Fine.")
    assert state.board.get("T1").status == "done" and state.board.get("T1").review_note == "Fine."


def test_the_project_can_only_be_closed_in_the_final_review(state: ProjectState, tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="not finished yet"):
        call("finish_project", state, tmp_path, summary="Done!")
    state.phase = "wrap_up"
    call("finish_project", state, tmp_path, summary="Delivered a landing page.")
    assert state.finished and state.final_summary == "Delivered a landing page."


def test_messages_and_brief(state: ProjectState, tmp_path: Path) -> None:
    call("post_message", state, tmp_path, "Designer", to="developer", text="Tokens are in the brief.")
    message = state.messages.all()[0]
    assert (message.sender, message.to) == ("Designer", "Developer")
    with pytest.raises(ToolError, match="must be 'all' or one of"):
        call("post_message", state, tmp_path, "Designer", to="the intern", text="Hi")

    call("update_brief", state, tmp_path, "Designer", key="design_tokens", text="accent #c8452c")
    assert state.brief.to_dict()["notes"]["design_tokens"] == {"text": "accent #c8452c", "by": "Designer"}
    with pytest.raises(ToolError, match="snake_case"):
        call("update_brief", state, tmp_path, "Designer", key="Design Tokens", text="x")
    # The brief can't be overwritten through the generic file tool.
    with pytest.raises(ToolError, match="dedicated tool"):
        call("write_file", state, tmp_path, "Developer", path="project_brief.json", content="{}")


def test_board_tools_need_a_board(tmp_path: Path) -> None:
    ctx = ToolContext(agent_name="Researcher", workspace=Workspace(tmp_path))
    with pytest.raises(ToolError, match="no task board"):
        TOOL_REGISTRY["post_message"].handler(ctx, {"to": "all", "text": "Hi"})


def test_least_privilege_per_role() -> None:
    tools = {role: set(spec.tool_names) for role, spec in ROLES.items()}
    assert [role for role in tools if "create_task" in tools[role]] == ["ceo"]
    assert [role for role in tools if "finish_project" in tools[role]] == ["ceo"]
    assert [role for role in tools if "review_task" in tools[role]] == ["qa"]
    assert "write_file" not in tools["qa"] and "write_file" not in tools["ceo"]
    assert all("update_task" in tools[r] for r in ("researcher", "designer", "developer", "marketer"))
    assert all("read_file" in t for t in tools.values())


def test_a_solo_agent_gets_no_board_tools(settings, workspace, costs) -> None:
    agent = build_agent("developer", client=object(), settings=settings, workspace=workspace, costs=costs)
    assert set(agent.tools) == set(ROLES["developer"].tool_names) - BOARD_TOOL_NAMES
    assert "write_file" in agent.tools and "update_task" not in agent.tools
