"""The orchestrator: runs one project from the client's goal to the final files.

Agents never call each other. The orchestrator is the only thing that decides
who works when, and it decides by looking at the task board:

    1. PLAN      the CEO breaks the goal into tasks on the board
    2. WORK      each task goes to its owner as soon as its dependencies are
                 done; several agents work at the same time; every finished
                 task goes to QA, who approves it or sends it back (at most
                 `max_revisions` times)
    3. WRAP UP   the CEO checks the result and closes the project, or (once)
                 adds follow-up tasks, which sends us back to step 2

Design decisions:
  * The orchestrator is plain, predictable code. The *judgement* (what to
    build, is it good enough) belongs to the models; the *control flow* (order,
    limits, retries, budget) does not. That is what keeps cost and behavior
    bounded no matter what a model answers.
  * Parallel work uses a thread pool: an agent spends almost all its time
    waiting for the API, which is exactly what threads are good at.
  * One seat per role: an agent does one thing at a time, so two tasks for the
    Marketer run one after the other even if both are ready.
  * A failed task does not sink the project. Tasks that depended on it are
    marked failed too, everything else carries on, and the CEO reports it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Literal

import anthropic

from bottega.agents.base import AgentError, AgentResult
from bottega.agents.roles import build_agent
from bottega.config import Settings
from bottega.costs import CostTracker
from bottega.errors import explain, is_fatal, is_transient
from bottega.events import Event
from bottega.memory import MemoryVault
from bottega.project import MAX_TASKS, Message, ProjectBrief, ProjectState, Task, TaskBoard
from bottega.project.messages import MessageLog
from bottega.sandbox import Workspace
from bottega.team import title_of

logger = logging.getLogger(__name__)

OutcomeStatus = Literal["done", "incomplete", "stopped", "over_budget"]

# emit(agent display name, event kind, data)
Emit = Callable[[str, str, dict[str, Any]], None]

# Who gets the credit in the live feed for each kind of change on the board.
_ACTOR = {"created": "CEO", "approved": "QA", "revision": "QA", "failed": "Bottega"}


class OrchestratorError(Exception):
    """The project cannot continue (for example, the CEO produced no plan)."""


@dataclass(frozen=True)
class Outcome:
    status: OutcomeStatus
    summary: str


class Orchestrator:
    def __init__(
        self,
        *,
        project_id: str,
        goal: str,
        workspace: Workspace,
        settings: Settings,
        client: Any,
        costs: CostTracker,
        memory: MemoryVault | None = None,
        cancel: threading.Event | None = None,
        emit: Emit = lambda agent, kind, data: None,
    ) -> None:
        self.goal = goal
        self.workspace = workspace
        self.settings = settings
        self.client = client
        self.costs = costs
        self.memory = memory
        self.cancel = cancel or threading.Event()
        self._emit = emit

        self.state = ProjectState(
            board=TaskBoard(settings.max_revisions, on_change=self._on_task_change),
            messages=MessageLog(on_post=self._on_message),
            brief=ProjectBrief(workspace, project_id, goal, on_change=self._on_brief_change),
        )
        self._lock = threading.Lock()
        self._active: dict[str, dict[str, Any]] = {}  # role -> what that agent is doing now
        self._reviewing: set[str] = set()  # task ids QA is looking at right now
        self._review_attempts: dict[str, int] = {}
        self._fatal: BaseException | None = None
        self._closing_words = ""  # the CEO's last reply, used if it never calls finish_project

    # ------------------------------------------------------------------ public

    def run(self) -> Outcome:
        """Run the whole project. Raises only for errors that make it pointless
        to continue (bad API key, no plan); everything else becomes an Outcome."""
        self.state.brief.save()
        try:
            self._plan()
            while not self._halted():
                self._work()
                if self._halted():
                    break
                self._wrap_up()
                if self.state.finished or self.state.board.is_settled():
                    break  # closed by the CEO, or no follow-up tasks were added
            return self._outcome()
        finally:
            self.state.phase = "closed"
            self.state.save(self.workspace)

    def activity(self) -> dict[str, dict[str, Any]]:
        """What each busy agent is doing right now (for the dashboard)."""
        with self._lock:
            return {role: dict(info) for role, info in self._active.items()}

    def post_founder_message(self, text: str) -> Message:
        """A message from the human owner. Agents read it when they start their next task."""
        return self.state.messages.post("Founder", "all", text)

    # ------------------------------------------------------------- the 3 steps

    def _plan(self) -> None:
        self.state.phase = "planning"
        self._mark_busy("ceo", doing="planning")
        self._run_agent("ceo", self._planning_prompt())
        if self._halted():
            return
        if not self.state.board.tasks():
            raise OrchestratorError("The CEO did not create any tasks, so there is nothing to work on.")
        self.state.phase = "working"

    def _work(self) -> None:
        """Keep handing out work until the board is settled or we must stop."""
        with ThreadPoolExecutor(self.settings.max_parallel, thread_name_prefix="agent") as pool:
            running: dict[Future[None], str] = {}
            while True:
                if not self._halted():
                    self.state.board.fail_blocked()
                    free_slots = self.settings.max_parallel - len(running)
                    for kind, task in self._next_jobs()[:max(free_slots, 0)]:
                        # Claim the task and the seat here, on the coordinating
                        # thread, so the same job can't be picked twice.
                        if kind == "work":
                            self.state.board.start(task.id)
                            self._mark_busy(task.owner, doing="working", task=task)
                            running[pool.submit(self._do_work, task.id)] = task.id
                        else:
                            self._reviewing.add(task.id)
                            self._mark_busy("qa", doing="reviewing", task=task)
                            running[pool.submit(self._do_review, task.id)] = task.id
                if not running:
                    break
                finished, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in finished:
                    running.pop(future)
                    error = future.exception()
                    if error is not None and self._fatal is None:
                        self._fatal = error
                        self.cancel.set()  # no point letting the others keep spending
                self.state.save(self.workspace)
        if self._fatal is not None:
            raise self._fatal

    def _wrap_up(self) -> None:
        self.state.phase = "wrap_up"
        tasks_before = len(self.state.board.tasks())
        self._mark_busy("ceo", doing="final review")
        self._closing_words = self._run_agent("ceo", self._wrap_up_prompt()).final_text
        if len(self.state.board.tasks()) > tasks_before:
            self.state.follow_ups_left -= 1
            self.state.phase = "working"

    # -------------------------------------------------------------------- jobs

    def _next_jobs(self) -> list[tuple[str, Task]]:
        """Work that can start right now: at most one review, plus ready tasks."""
        busy = set(self.activity())
        jobs: list[tuple[str, Task]] = []
        if "qa" not in busy:
            waiting = [t for t in self.state.board.awaiting_review() if t.id not in self._reviewing]
            if waiting:
                jobs.append(("review", waiting[0]))
        jobs.extend(("work", task) for task in self.state.board.ready(busy))
        return jobs

    def _do_work(self, task_id: str) -> None:
        """Runs in a worker thread: the owner works on one task."""
        board = self.state.board
        task = board.get(task_id)
        try:
            result = self._run_agent(task.owner, self._task_prompt(task), task_id)
        except Exception as exc:
            if is_fatal(exc):
                raise
            if is_transient(exc) and task.attempts <= self.settings.task_retries:
                logger.warning("%s failed with a temporary error, retrying: %s", task_id, exc)
                board.release(task_id)
            else:
                if not isinstance(exc, (AgentError, anthropic.AnthropicError)):
                    logger.exception("Task %s crashed", task_id)  # a bug in our own code
                board.fail(task_id, explain(exc))
            return

        if board.get(task_id).status != "in_progress":
            return  # the agent handed it in (or marked it blocked) with update_task
        if result.stop_reason in ("cancelled", "budget_exceeded"):
            board.release(task_id)
        elif result.completed:
            # The agent finished but forgot to call update_task: hand it in for them.
            board.submit(task_id, result.final_text or "(no summary provided)")
        else:
            board.fail(task_id, f"The {title_of(task.owner)} could not finish: {result.final_text}")

    def _do_review(self, task_id: str) -> None:
        """Runs in a worker thread: QA reviews one finished task."""
        board = self.state.board
        task = board.get(task_id)
        attempt = self._review_attempts[task_id] = self._review_attempts.get(task_id, 0) + 1
        try:
            result = self._run_agent("qa", self._review_prompt(task), task_id)
        except Exception as exc:
            if is_fatal(exc):
                raise
            if is_transient(exc) and attempt <= self.settings.task_retries:
                return  # still "review": it will be picked up again
            # A broken reviewer must not block delivery. Accept the work and say so.
            board.approve(task_id, f"Not reviewed: QA failed ({explain(exc)}).")
            return
        finally:
            self._reviewing.discard(task_id)

        if board.get(task_id).status == "review" and result.stop_reason not in (
            "cancelled", "budget_exceeded"
        ):
            board.approve(task_id, "QA gave no verdict; accepted as delivered.")

    def _run_agent(self, role: str, prompt: str, task_id: str | None = None) -> AgentResult:
        """Build the agent for `role`, run it, and always free its seat afterwards."""
        try:
            agent = build_agent(
                role,
                client=self.client,
                settings=self.settings,
                workspace=self.workspace,
                costs=self.costs,
                memory=self.memory,
                project=self.state,
                cancel=self.cancel,
                on_event=self._on_agent_event,
            )
            return agent.run(prompt, task_id)
        finally:
            with self._lock:
                self._active.pop(role, None)

    def _mark_busy(self, role: str, *, doing: str, task: Task | None = None) -> None:
        with self._lock:
            self._active[role] = {
                "doing": doing,
                "task_id": task.id if task else None,
                "task_title": task.title if task else None,
            }

    # ---------------------------------------------------------------- outcome

    def _halted(self) -> bool:
        return self.cancel.is_set() or self._over_budget()

    def _over_budget(self) -> bool:
        return self.costs.total_usd >= self.settings.max_cost_usd

    def _outcome(self) -> Outcome:
        tasks = self.state.board.tasks()
        all_done = bool(tasks) and all(t.status == "done" for t in tasks)
        if self.state.finished and all_done:
            return Outcome("done", self.state.final_summary)
        if self.cancel.is_set():
            return Outcome("stopped", "Stopped by the founder before the work was finished.")
        if all_done:
            # Every task passed QA; the CEO just never formally closed the project.
            return Outcome("done", self.state.final_summary or self._closing_words or "All tasks are done.")
        if self._over_budget():
            limit = self.settings.max_cost_usd
            return Outcome("over_budget", f"Stopped: the ${limit:.2f} budget for this project is used up.")
        failed = [t.id for t in tasks if t.status == "failed"]
        note = f"Tasks that failed: {', '.join(failed)}." if failed else "Some tasks were not finished."
        summary = self.state.final_summary or self._closing_words
        return Outcome("incomplete", f"{summary}\n\n{note}".strip())

    # ---------------------------------------------------------------- prompts
    # What each agent is told. Shared context (brief, board, messages, files)
    # is added to every prompt, so no agent has to ask a colleague for it.

    def _context(self, reader: str) -> str:
        files = self.workspace.list_files()
        return "\n\n".join([
            "## Project brief\n" + self.state.brief.render(),
            "## Task board\n" + self.state.board.render(),
            "## Messages for you\n" + self.state.messages.render(reader),
            "## Files in the project folder\n" + ("\n".join(f"- {f}" for f in files) or "(none yet)"),
        ])

    def _planning_prompt(self) -> str:
        return "\n\n".join([
            "Plan the project.",
            f"## Client goal\n{self.goal}",
            "## Limits\n"
            f"- At most {MAX_TASKS} tasks; four to six is usual.\n"
            f"- QA reviews every task and can send it back up to {self.settings.max_revisions} times.\n"
            f"- The budget for the whole project is ${self.settings.max_cost_usd:.2f}.",
            "## Messages for you\n" + self.state.messages.render("CEO"),
        ])

    def _task_prompt(self, task: Task) -> str:
        parts = [f"# Your task: {task.id} — {task.title}", task.description]
        if task.feedback:
            parts.append(
                f"## QA asked for changes (revision {task.revisions} of {self.settings.max_revisions})\n"
                f"{task.feedback[-1]}\n\n"
                "Fix every point above and keep what already works. Rewrite complete files."
            )
        parts.append(
            "## How to hand in\n"
            "Write your deliverables with write_file. Then call update_task with "
            f'task_id "{task.id}", status "done" and an output summary that names the files you '
            "wrote. After that, reply with one sentence and stop."
        )
        parts.append(self._context(title_of(task.owner)))
        return "\n\n".join(parts)

    def _review_prompt(self, task: Task) -> str:
        final = not self.state.board.can_revise(task.id)
        parts = [
            f"# Review task {task.id} — {task.title}",
            f"Owner: {title_of(task.owner)}\n"
            f"Revisions so far: {task.revisions} of {self.settings.max_revisions}",
            f"## What was asked\n{task.description}",
            f"## What the owner says was delivered\n{task.output}",
        ]
        if task.feedback:
            parts.append("## Your earlier feedback on this task\n" + "\n\n".join(task.feedback))
        if final:
            parts.append(
                "## This is the final review\n"
                "The revision limit is reached. You must approve; list any remaining issues in "
                "the feedback so the client knows about them."
            )
        parts.append(
            "## How to give your verdict\n"
            f'Read the delivered files, then call review_task with task_id "{task.id}".'
        )
        parts.append(self._context("QA"))
        return "\n\n".join(parts)

    def _wrap_up_prompt(self) -> str:
        if self.state.follow_ups_left > 0:
            options = ("If the goal is met, call finish_project. If something essential is missing "
                       "you may create follow-up tasks instead (this is the only time you can).")
        else:
            options = "No more tasks can be added. Call finish_project and be honest about what is missing."
        return "\n\n".join([
            "Review the finished work.",
            f"## Client goal\n{self.goal}",
            f"## What you can do\n{options}",
            self._context("CEO"),
        ])

    # ----------------------------------------------------------------- events

    def _on_agent_event(self, event: Event) -> None:
        self._emit(event.agent, event.kind, event.data)

    def _on_task_change(self, change: str, task: Task) -> None:
        actor = _ACTOR.get(change) or title_of(task.owner)
        self._emit(actor, "task", {"change": change, "task": task.to_dict()})

    def _on_message(self, message: Message) -> None:
        self._emit(message.sender, "message", {"to": message.to, "text": message.text})

    def _on_brief_change(self, key: str, author: str) -> None:
        self._emit(author, "brief", {"key": key})
