"""Tools for teamwork: the task board, messages and the shared brief.

Least privilege in practice: each role only gets the tools its job needs (see
agents/roles.py), and each tool double-checks what it is asked to do. A worker
can only hand in its own current task; QA can only judge the task it was asked
to review; only the CEO can add tasks or close the project.
"""

from __future__ import annotations

from typing import Any

from bottega.project import EVERYONE, BoardError, ProjectState
from bottega.team import SEATS, WORKER_ROLES, title_of
from bottega.tools.base import Tool, ToolContext, ToolError


def _state(ctx: ToolContext) -> ProjectState:
    if ctx.project is None:
        raise ToolError("There is no task board in this run.")
    return ctx.project


def _create_task(ctx: ToolContext, args: dict[str, Any]) -> str:
    state = _state(ctx)
    may_add = state.phase == "planning" or (state.phase == "wrap_up" and state.follow_ups_left > 0)
    if not may_add:
        raise ToolError("Tasks can't be added now. If the goal is met, call finish_project.")
    owner = args["owner"]
    if owner not in WORKER_ROLES:
        raise ToolError(
            f"Owner must be one of: {', '.join(WORKER_ROLES)}. QA reviews every task "
            "automatically, so never create tasks for QA or for yourself."
        )
    title, description = args["title"].strip(), args["description"].strip()
    if not title or not description:
        raise ToolError("A task needs both a title and a description.")
    depends_on = args.get("depends_on", [])
    if not all(isinstance(d, str) for d in depends_on):
        raise ToolError("depends_on must be a list of task ids, e.g. [\"T1\"].")
    try:
        task = state.board.add(title[:120], description[:3000], owner, depends_on)
    except BoardError as exc:
        raise ToolError(str(exc)) from exc
    after = f", after {', '.join(task.depends_on)}" if task.depends_on else ""
    return f"Created {task.id}: {task.title} (owner: {owner}{after})."


def _update_task(ctx: ToolContext, args: dict[str, Any]) -> str:
    state = _state(ctx)
    task_id = args["task_id"]
    if task_id != ctx.task_id:
        raise ToolError(f"You can only update the task you are working on ({ctx.task_id}).")
    output = args["output"].strip()
    if not output:
        raise ToolError("Describe what you delivered (or why you are blocked) in `output`.")
    try:
        if args["status"] == "done":
            state.board.submit(task_id, output[:2000])
            return f"{task_id} handed in. QA will review it. Reply with one sentence and stop."
        state.board.fail(task_id, output[:2000])
        return f"{task_id} marked as blocked. Reply with one sentence and stop."
    except BoardError as exc:
        raise ToolError(str(exc)) from exc


def _post_message(ctx: ToolContext, args: dict[str, Any]) -> str:
    state = _state(ctx)
    to = args["to"]
    if to != EVERYONE and to not in SEATS:
        raise ToolError(f"`to` must be 'all' or one of: {', '.join(SEATS)}.")
    recipient = EVERYONE if to == EVERYONE else title_of(to)
    try:
        state.messages.post(ctx.agent_name, recipient, args["text"])
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return f"Message posted to {'everyone' if to == EVERYONE else recipient}."


def _update_brief(ctx: ToolContext, args: dict[str, Any]) -> str:
    state = _state(ctx)
    try:
        state.brief.set_note(args["key"], args["text"], ctx.agent_name)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return f"Brief updated: {args['key']}."


def _review_task(ctx: ToolContext, args: dict[str, Any]) -> str:
    state = _state(ctx)
    task_id = args["task_id"]
    if task_id != ctx.task_id:
        raise ToolError(f"You are reviewing {ctx.task_id}; you can only give a verdict on that task.")
    feedback = args.get("feedback", "").strip()
    try:
        if args["verdict"] == "approve":
            state.board.approve(task_id, feedback[:2000])
            return f"{task_id} approved. Reply with one sentence and stop."
        if not feedback:
            raise ToolError("A revision needs specific feedback: list exactly what must change.")
        state.board.request_revision(task_id, feedback[:2000])
        return f"{task_id} sent back to its owner. Reply with one sentence and stop."
    except BoardError as exc:
        raise ToolError(str(exc)) from exc


def _finish_project(ctx: ToolContext, args: dict[str, Any]) -> str:
    state = _state(ctx)
    if state.phase != "wrap_up":
        raise ToolError("The work is not finished yet. finish_project is only for the final review.")
    summary = args["summary"].strip()
    if not summary:
        raise ToolError("Write a summary for the client.")
    state.finished = True
    state.final_summary = summary[:4000]
    return "Project closed. Reply with one closing sentence and stop."


_ROLE_NAMES = list(SEATS)

CREATE_TASK = Tool(
    name="create_task",
    description=(
        "Add a task to the board and assign it to one team member. The description must be "
        "precise enough to work from without asking questions: what to produce, which files to "
        "write, what good looks like. Use depends_on for tasks that need another task's result; "
        "tasks without a dependency between them run at the same time. A task can only depend "
        "on tasks created before it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short, e.g. 'Design system for the landing page'."},
            "description": {"type": "string", "description": "What to produce, file names, quality bar."},
            "owner": {"type": "string", "enum": list(WORKER_ROLES)},
            "depends_on": {"type": "array", "items": {"type": "string"},
                           "description": "Ids of tasks that must be done first, e.g. [\"T1\"]."},
        },
        "required": ["title", "description", "owner"],
        "additionalProperties": False,
    },
    handler=_create_task,
)

UPDATE_TASK = Tool(
    name="update_task",
    description=(
        "Hand in the task you are working on. Use status 'done' when your deliverables are "
        "written, with an output summary that names the files. Use 'blocked' only if you truly "
        "cannot do the task, and say why."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Your current task, e.g. 'T3'."},
            "status": {"type": "string", "enum": ["done", "blocked"]},
            "output": {"type": "string", "description": "2-4 sentences: what you delivered and in which files."},
        },
        "required": ["task_id", "status", "output"],
        "additionalProperties": False,
    },
    handler=_update_task,
)

POST_MESSAGE = Tool(
    name="post_message",
    description=(
        "Post a short message on the team's message log. Others read it when they start their "
        "next task. Use it for things a colleague needs to know and that are not in your "
        "deliverable (a decision, a warning, a question). Don't narrate your work."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "to": {"type": "string", "enum": [EVERYONE, *_ROLE_NAMES]},
            "text": {"type": "string", "description": "One to three sentences."},
        },
        "required": ["to", "text"],
        "additionalProperties": False,
    },
    handler=_post_message,
)

UPDATE_BRIEF = Tool(
    name="update_brief",
    description=(
        "Save a decision or key fact to the project brief (project_brief.json), which every team "
        "member sees at the top of every task. Use it for things everybody must apply the same "
        "way, e.g. key 'audience', 'tone_of_voice', 'design_tokens'. Setting an existing key "
        "replaces it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Short snake_case name, e.g. 'tone_of_voice'."},
            "text": {"type": "string", "description": "The note: concise and concrete."},
        },
        "required": ["key", "text"],
        "additionalProperties": False,
    },
    handler=_update_brief,
)

REVIEW_TASK = Tool(
    name="review_task",
    description=(
        "Give your verdict on the task you are reviewing. 'approve' accepts the work (put minor "
        "remarks in feedback). 'revise' sends it back to its owner: feedback must then be a "
        "numbered list of exact changes (which file, what, where)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "verdict": {"type": "string", "enum": ["approve", "revise"]},
            "feedback": {"type": "string", "description": "Required for 'revise'; optional remarks for 'approve'."},
        },
        "required": ["task_id", "verdict"],
        "additionalProperties": False,
    },
    handler=_review_task,
)

FINISH_PROJECT = Tool(
    name="finish_project",
    description=(
        "Close the project during the final review, when the client's goal is met. The summary "
        "is what the client reads: what was delivered, where the files are, and what still "
        "needs their attention (placeholders, assumptions)."
    ),
    input_schema={
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    },
    handler=_finish_project,
)

BOARD_TOOLS = [CREATE_TASK, UPDATE_TASK, POST_MESSAGE, UPDATE_BRIEF, REVIEW_TASK, FINISH_PROJECT]
