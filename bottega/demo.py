"""Demo mode: a scripted stand-in for the Claude API.

`--demo` swaps the real Anthropic client for this `DemoClient`. It answers
every request from a fixed script, but everything around it is real: the
orchestrator, the task board, the tools, the QA loop, the dashboard. So you can
explore and develop the whole system:
  * without an API key,
  * without spending money,
  * with the same result every time (which also makes it a great test double).

The script shows the full life of a project: the CEO plans five tasks, the
team works on them (two at a time where the plan allows), QA reviews each one
and sends the website back once before approving it.

Nothing here is written by Claude: the deliverables are placeholders and the
costs are simulated. Demo files go to a temporary folder, so your real
`output/` and `memory/` are never touched.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from anthropic.types.beta import BetaMessage

from bottega.config import Settings

Block = dict[str, Any]
# (content blocks, input tokens, output tokens); a step without tool calls ends the turn.
Step = tuple[list[Block], int, int]


def demo_settings(settings: Settings) -> Settings:
    """Point output and memory at a throwaway folder (memory starts as a copy)."""
    root = Path(tempfile.mkdtemp(prefix="bottega-demo-"))
    if settings.memory_dir.is_dir():
        shutil.copytree(settings.memory_dir, root / "memory",
                        ignore=shutil.ignore_patterns(".obsidian"))
    return replace(settings, output_dir=root / "output", memory_dir=root / "memory")


def _text(value: str) -> Block:
    return {"type": "text", "text": value}


def _tool(name: str, **tool_input: Any) -> Block:
    return {"type": "tool_use", "name": name, "input": tool_input}


def _find(pattern: str, text: str, default: str = "") -> str:
    match = re.search(pattern, text)
    return match.group(1).strip() if match else default


# --------------------------------------------------------------- deliverables

_NOTICE = ("> **Demo mode.** This file was produced by a script, not by Claude.\n"
           "> Start Bottega without `--demo` (and with an API key) to get real work.\n")


def _research(goal: str) -> str:
    return f"""# Research (demo placeholder)

{_NOTICE}
**Goal:** {goal}

## Business summary
- (In a real run, the Researcher describes the business and its local context here.)

## Target audience
- (2-3 personas with their goals and objections.)

## Competitors
- (Typical competitor archetypes for this kind of business.)

## Positioning
- (One-sentence positioning statement and 3 key messages.)

## Recommendations for the team
- Designer: warm, welcoming, hand-made feeling
- Developer: see [[Landing page checklist]] for the must-have sections
- Marketer: friendly and concrete; write in the audience's language
"""


_DESIGN = f"""# Design system (demo placeholder)

{_NOTICE}
## Concept
Warm and welcoming, like a place you already know. Generous spacing, one strong accent.

## Color palette
| Role | Hex |
|---|---|
| Background | `#fbf6ee` |
| Surface | `#ffffff` |
| Text | `#2a211b` |
| Muted text | `#6f6258` |
| Primary accent | `#c8452c` |
| Secondary accent | `#3f6b4f` |

## Typography
- Headings: Georgia, 'Times New Roman', serif — h1 44px/1.1, h2 28px/1.2
- Body: system-ui, sans-serif — 17px/1.6

## Page structure
1. Hero: headline, one-line promise, primary button
2. Three reasons to choose us
3. Offer (menu or services)
4. Location, hours and contact
5. Footer
"""

_COPY = f"""# Website copy (demo placeholder)

{_NOTICE}
## Hero
- Headline: [PLACEHOLDER: headline written by the Marketer]
- Subheadline: [PLACEHOLDER: one-line promise]
- Button: Book a table

## Three reasons
1. [PLACEHOLDER: reason one]
2. [PLACEHOLDER: reason two]
3. [PLACEHOLDER: reason three]

## Contact
- [PLACEHOLDER: address] · [PLACEHOLDER: phone number] · [PLACEHOLDER: opening hours]

## Meta
- Title: [PLACEHOLDER: meta title]
- Description: [PLACEHOLDER: meta description]
"""

_EMAIL = f"""# Cold outreach email (demo placeholder)

{_NOTICE}
**Subject:** [PLACEHOLDER: subject line]

Hello [PLACEHOLDER: owner's name],

[PLACEHOLDER: three short sentences presenting the new landing page and one clear call to action.]

Best regards,
Bottega
"""

_POSTS = f"""# Social posts (demo placeholder)

{_NOTICE}
1. **Instagram** — [PLACEHOLDER: post text] · Visual: [PLACEHOLDER] · #placeholder
2. **Facebook** — [PLACEHOLDER: post text] · Visual: [PLACEHOLDER]
3. **Google Business Profile** — [PLACEHOLDER: post text]
"""

_CSS = """/* Demo placeholder stylesheet, written by a script (not by Claude). */
:root {
  --bg: #fbf6ee; --surface: #ffffff; --text: #2a211b; --muted: #6f6258;
  --accent: #c8452c; --accent-2: #3f6b4f;
}
* { box-sizing: border-box; }
body { margin: 0; font: 17px/1.6 system-ui, sans-serif; color: var(--text); background: var(--bg); }
.wrap { max-width: 960px; margin: 0 auto; padding: 0 20px; }
.notice { background: var(--accent-2); color: #fff; text-align: center; padding: 8px 20px; font-size: 14px; }
.hero { padding: 72px 0 56px; }
.hero h1 { font: 700 44px/1.1 Georgia, 'Times New Roman', serif; margin: 0 0 16px; }
.hero p { font-size: 20px; color: var(--muted); max-width: 34em; margin: 0 0 28px; }
.button { display: inline-block; padding: 14px 26px; border-radius: 999px; background: var(--accent);
  color: #fff; text-decoration: none; font-weight: 600; }
.button:focus-visible { outline: 3px solid var(--accent-2); outline-offset: 3px; }
.reasons { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); padding-bottom: 56px; }
.card { background: var(--surface); border-radius: 16px; padding: 24px; box-shadow: 0 1px 2px rgba(42, 33, 27, 0.08); }
.card h2 { font: 700 22px/1.2 Georgia, serif; margin: 0 0 8px; }
footer { border-top: 1px solid rgba(42, 33, 27, 0.12); padding: 24px 0 40px; color: var(--muted); font-size: 15px; }
@media (max-width: 600px) { .hero { padding: 48px 0 40px; } .hero h1 { font-size: 34px; } }
"""


def _html(goal: str, fixed: bool) -> str:
    """The demo page. The first version has two flaws for QA to find."""
    html_tag = '<html lang="en">' if fixed else "<html>"
    button = '<a class="button" href="#contact">Book a table</a>' if fixed \
        else '<a class="button">Book a table</a>'
    return f"""<!doctype html>
{html_tag}
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Demo placeholder page</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <p class="notice">Demo placeholder: this page was produced by a script, not by Claude.</p>
  <main class="wrap">
    <section class="hero">
      <h1>{goal}</h1>
      <p>In a real run, the Developer builds this page from the Designer's system and the Marketer's copy.</p>
      {button}
    </section>
    <section class="reasons" aria-label="Three reasons to choose us">
      <article class="card"><h2>Reason one</h2><p>[PLACEHOLDER: written by the Marketer]</p></article>
      <article class="card"><h2>Reason two</h2><p>[PLACEHOLDER: written by the Marketer]</p></article>
      <article class="card"><h2>Reason three</h2><p>[PLACEHOLDER: written by the Marketer]</p></article>
    </section>
  </main>
  <footer id="contact"><div class="wrap">[PLACEHOLDER: address] · [PLACEHOLDER: phone number] · [PLACEHOLDER: opening hours]</div></footer>
</body>
</html>
"""


# ------------------------------------------------------------------ the client


class _DemoStream:
    def __init__(self, message: BetaMessage, delay: float) -> None:
        self._message = message
        self._delay = delay

    def __enter__(self) -> _DemoStream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_final_message(self) -> BetaMessage:
        time.sleep(self._delay)  # pretend the model is thinking
        return self._message


class DemoClient:
    """Mimics the one SDK call the agent loop uses: `client.beta.messages.stream()`.

    The client keeps no state. It works out who is asking from the system
    prompt, and how far along that agent is from the number of replies already
    in the conversation. That makes it safe to share between agents that run
    at the same time. Tests subclass it and override `qa_verdict`.
    """

    def __init__(self, delay: float = 1.0) -> None:
        self.delay = delay
        self.beta = self
        self.messages = self

    def stream(self, **request: Any) -> _DemoStream:
        messages = request["messages"]
        role = _find(r"You are (?:the )?(\w+) (?:at|of) Bottega", request["system"][0]["text"])
        first = messages[0]["content"] if isinstance(messages[0]["content"], str) else ""
        step = sum(1 for m in messages if m["role"] == "assistant")

        steps = self._script(role, first)
        blocks, tokens_in, tokens_out = steps[min(step, len(steps) - 1)]
        content = [dict(block, id=f"toolu_demo_{step}_{i}") if block["type"] == "tool_use" else block
                   for i, block in enumerate(blocks)]
        calls_tools = any(block["type"] == "tool_use" for block in content)
        message = BetaMessage.model_validate({
            "id": f"msg_demo_{step}",
            "type": "message",
            "role": "assistant",
            "model": request["model"],
            "content": content,
            "stop_reason": "tool_use" if calls_tools else "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
        })
        return _DemoStream(message, self.delay)

    # ---------------------------------------------------------------- scripts

    def qa_verdict(self, owner: str, revisions: int) -> tuple[str, str]:
        """(verdict, feedback) for a review. The website is sent back once."""
        if owner == "Developer" and revisions == 0:
            return "revise", (
                "1. site/index.html: the <html> tag has no lang attribute; add lang=\"en\".\n"
                "2. site/index.html: the hero button has no href; link it to the contact section."
            )
        return "approve", "Meets the task. (Demo review: the content is placeholder text.)"

    def _script(self, role: str, first: str) -> list[Step]:
        goal = _find(r"Client goal\W+(.+)", first, "your goal")
        task_id = _find(r"# (?:Your task:|Review task) (T\d+)", first)
        if role == "CEO":
            return self._ceo_wrap_up(goal) if first.startswith("Review") else self._ceo_plan(goal)
        if role == "QA":
            return self._qa(first, task_id)
        if role == "Researcher":
            return self._researcher(goal, task_id)
        if role == "Designer":
            return self._designer(task_id)
        if role == "Developer":
            return self._developer(goal, task_id, revision="QA asked for changes" in first)
        if role == "Marketer":
            title = _find(r"# Your task: T\d+ — (.+)", first).lower()
            return self._marketer(task_id, promo="email" in title or "social" in title)
        return [([_text("Done.")], 500, 10)]

    @staticmethod
    def _ceo_plan(goal: str) -> list[Step]:
        return [
            ([_text("Let me see what the studio already knows before I plan."),
              _tool("search_memory", query="landing page checklist playbook")], 1900, 70),
            ([_text("Five tasks: research first, then design and copy side by side."),
              _tool("create_task", owner="researcher", title="Research the business and its audience",
                    description=f"Goal: {goal}. Write research.md: business summary, audience "
                                "personas, competitor archetypes, positioning, recommendations."),
              _tool("create_task", owner="designer", title="Design system for the landing page",
                    description="Write design.md: concept, palette with hex values, typography, "
                                "layout and page structure, based on research.md.",
                    depends_on=["T1"]),
              _tool("create_task", owner="marketer", title="Website copy",
                    description="Write copy.md: every text on the page, section by section, "
                                "plus meta title and description.",
                    depends_on=["T1"]),
              _tool("create_task", owner="developer", title="Build the landing page",
                    description="Build site/index.html and site/style.css from design.md and "
                                "copy.md. Plain HTML and CSS, responsive, accessible.",
                    depends_on=["T2", "T3"]),
              _tool("create_task", owner="marketer", title="Outreach email and social posts",
                    description="Write marketing/outreach_email.md (one cold email) and "
                                "marketing/social_posts.md (three posts).",
                    depends_on=["T3"]),
              _tool("update_brief", key="summary", text=f"We are building: {goal}"),
              _tool("update_brief", key="deliverables",
                    text="research.md, design.md, copy.md, site/index.html, site/style.css, "
                         "marketing/outreach_email.md, marketing/social_posts.md")], 2600, 620),
            ([_tool("post_message", to="all",
                    text="Kickoff! Research goes first. Design and copy can then run side by "
                         "side. Use clearly marked placeholders for anything we don't know.")],
             3400, 90),
            ([_text("Plan ready: five tasks, research first, then design and copy in parallel, "
                    "then the website and the marketing material.")], 3600, 60),
        ]

    @staticmethod
    def _ceo_wrap_up(goal: str) -> list[Step]:
        return [
            ([_text("Everything passed QA. Let me check the files before I close the project."),
              _tool("list_files")], 3900, 60),
            ([_tool("finish_project", summary=(
                f"Delivered for: {goal}\n\n"
                "- Research: research.md\n- Design system: design.md\n- Website copy: copy.md\n"
                "- Website: site/index.html and site/style.css\n"
                "- Marketing: marketing/outreach_email.md and marketing/social_posts.md\n\n"
                "QA approved every task; the website needed one revision.\n\n"
                "Demo mode: all content is placeholder text produced by a script."))], 4300, 260),
            ([_text("The project is closed and the files are ready for the client.")], 4500, 30),
        ]

    @staticmethod
    def _researcher(goal: str, task_id: str) -> list[Step]:
        return [
            ([_text("First, what does the studio already know about projects like this?"),
              _tool("search_memory", query=f"landing page checklist {goal}")], 2100, 80),
            ([_text("The playbook applies. Writing the research brief for the team."),
              _tool("write_file", path="research.md", content=_research(goal)),
              _tool("update_brief", key="audience", text="(Demo) Local customers looking for a place they can trust."),
              _tool("update_brief", key="tone_of_voice", text="(Demo) Friendly, concrete, no clichés.")],
             2900, 760),
            ([_tool("save_memory", folder="lessons", title="Demo run notes", tags=["demo"],
                    content="Placeholder saved by demo mode to show how agents add to company "
                            f"memory.\n\nLast demo goal: {goal}\n\nRelated: [[Landing page checklist]]")],
             3800, 150),
            ([_tool("update_task", task_id=task_id, status="done",
                    output="Wrote research.md and saved audience and tone of voice to the brief.")],
             4000, 70),
            ([_text("Research handed in.")], 4100, 20),
        ]

    @staticmethod
    def _designer(task_id: str) -> list[Step]:
        return [
            ([_text("Reading the research before I choose a direction."),
              _tool("read_file", path="research.md")], 2300, 60),
            ([_text("Warm and welcoming it is. Writing the design system."),
              _tool("write_file", path="design.md", content=_DESIGN),
              _tool("update_brief", key="design_tokens",
                    text="bg #fbf6ee · text #2a211b · accent #c8452c · accent-2 #3f6b4f · "
                         "headings Georgia · body system-ui 17px")], 3200, 690),
            ([_tool("update_task", task_id=task_id, status="done",
                    output="Wrote design.md and saved the design tokens to the brief.")], 4000, 60),
            ([_text("Design system handed in.")], 4100, 20),
        ]

    @staticmethod
    def _marketer(task_id: str, promo: bool) -> list[Step]:
        if promo:
            return [
                ([_text("Reading the approved copy so the email and posts sound the same."),
                  _tool("read_file", path="copy.md")], 2500, 60),
                ([_tool("write_file", path="marketing/outreach_email.md", content=_EMAIL),
                  _tool("write_file", path="marketing/social_posts.md", content=_POSTS)], 3100, 540),
                ([_tool("update_task", task_id=task_id, status="done",
                        output="Wrote marketing/outreach_email.md and marketing/social_posts.md.")],
                 3700, 60),
                ([_text("Marketing material handed in.")], 3800, 20),
            ]
        return [
            ([_text("Reading the research to get the audience and the tone right."),
              _tool("read_file", path="research.md")], 2300, 60),
            ([_tool("write_file", path="copy.md", content=_COPY)], 3000, 610),
            ([_tool("update_task", task_id=task_id, status="done",
                    output="Wrote copy.md with every section of the page and the meta tags.")],
             3700, 60),
            ([_text("Copy handed in.")], 3800, 20),
        ]

    @staticmethod
    def _developer(goal: str, task_id: str, revision: bool) -> list[Step]:
        if revision:
            return [
                ([_text("QA found two problems. Opening the page to fix them."),
                  _tool("read_file", path="site/index.html")], 3600, 70),
                ([_tool("write_file", path="site/index.html", content=_html(goal, fixed=True))],
                 4300, 880),
                ([_tool("update_task", task_id=task_id, status="done",
                        output="Fixed site/index.html: added the lang attribute and linked the "
                               "hero button to the contact section.")], 5300, 70),
                ([_text("Fixes handed in.")], 5400, 20),
            ]
        return [
            ([_text("Reading the design system and the copy before writing any code."),
              _tool("read_file", path="design.md"), _tool("read_file", path="copy.md")], 2600, 90),
            ([_text("Building the page: one HTML file and one stylesheet."),
              _tool("write_file", path="site/index.html", content=_html(goal, fixed=False)),
              _tool("write_file", path="site/style.css", content=_CSS)], 3900, 1500),
            ([_tool("update_task", task_id=task_id, status="done",
                    output="Built site/index.html and site/style.css. Placeholders to replace: "
                           "all copy, address, phone number, opening hours.")], 5500, 80),
            ([_text("Website handed in.")], 5600, 20),
        ]

    def _qa(self, first: str, task_id: str) -> list[Step]:
        owner = _find(r"Owner: (\w+)", first)
        revisions = int(_find(r"Revisions so far: (\d+)", first, "0"))
        delivered = _find(r"## What the owner says was delivered\n(.+)", first)
        files = {
            "Researcher": ["research.md"],
            "Designer": ["design.md"],
            "Developer": ["site/index.html", "site/style.css"],
        }.get(owner) or (["marketing/outreach_email.md", "marketing/social_posts.md"]
                         if "marketing/" in delivered else ["copy.md"])
        verdict, feedback = self.qa_verdict(owner, revisions)
        closing = "Approved." if verdict == "approve" else "Sent back with two exact fixes."
        return [
            ([_text(f"Reviewing {task_id}: reading what was delivered."),
              *[_tool("read_file", path=path) for path in files]], 2800, 70),
            ([_tool("review_task", task_id=task_id, verdict=verdict, feedback=feedback)], 3900, 130),
            ([_text(closing)], 4000, 20),
        ]
