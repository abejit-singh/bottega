"use strict";

/* Bottega dashboard: frontend logic, plain JavaScript, no build step.

   How the page stays up to date:
     1. GET api/state returns a snapshot of the company (agents, projects,
        spend), and GET api/projects/<id> the project you are looking at
        (board, messages, files). Together they are the single source of truth.
     2. A Server-Sent Events stream (api/events) pushes each event the moment it
        happens. An event (a) adds a line to the activity feed and (b) tells us
        "something changed", so we fetch the snapshot again.
   Events are signals, the snapshot is truth. We never rebuild the board or the
   totals from events, so they cannot drift out of sync with the server.

   Security rule: everything agents write is untrusted text. It only ever
   enters the page through `textContent`, never `innerHTML`, so a model cannot
   inject markup or scripts into the dashboard. Pages built by agents are shown
   inside a sandboxed frame for the same reason. */

// ------------------------------------------------------------ DOM helpers

const $ = (id) => document.getElementById(id);

/** Create an element. `text` is set with textContent; `on*` adds a listener. */
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

// ------------------------------------------------------------- formatting

function fmtCost(usd) {
  if (!usd) return "$0.00";
  return usd < 0.1 ? `$${usd.toFixed(3)}` : `$${usd.toFixed(2)}`;
}

function fmtCount(n) {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e4) return `${(n / 1e3).toFixed(1)}K`;
  return n.toLocaleString("en-US");
}

function fmtTime(seconds) {
  return new Date(seconds * 1000).toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
}

const oneLine = (text) => String(text ?? "").replace(/\s+/g, " ").trim();
const clip = (text, max) => (text.length > max ? `${text.slice(0, max - 1)}…` : text);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

const STATUS = {
  running: "Running", done: "Done", incomplete: "Finished with problems",
  over_budget: "Stopped: budget reached", stopped: "Stopped", failed: "Failed",
};

const COLUMNS = [
  { title: "To do", holds: ["todo"] },
  { title: "In progress", holds: ["in_progress"] },
  { title: "In review", holds: ["review"] },
  { title: "Done", holds: ["done", "failed"] },
];

// ------------------------------------------------------------ characters
// Each agent is a small figure drawn with basic SVG shapes in a 120 x 120
// box: a body, a head with two eyes, and the tool of its trade. The markup
// below is fixed text written by us (no data from agents goes into it), which
// is the one case where building elements from a string is safe.

const BODY = `
  <ellipse class="shadow" cx="60" cy="113" rx="31" ry="4.5"/>
  <g class="figure">
    <rect class="arm arm-l" x="26" y="70" width="11" height="27" rx="5.5"/>
    <rect class="arm arm-r" x="83" y="70" width="11" height="27" rx="5.5"/>
    <rect class="torso" x="36" y="63" width="48" height="45" rx="16"/>
    HEAD
    <ellipse class="shine" cx="46" cy="25" rx="8" ry="3.6" transform="rotate(-16 46 25)"/>
    <rect class="visor" x="40" y="31" width="40" height="22" rx="11"/>
    <circle class="eye" cx="52" cy="42" r="3.6"/>
    <circle class="eye" cx="68" cy="42" r="3.6"/>
    EXTRAS
  </g>`;

const CHARACTERS = {
  // The CEO: a square head, a tie and an antenna that lights up with ideas.
  ceo: {
    head: `<rect class="skin" x="32" y="18" width="56" height="48" rx="13"/>`,
    extras: `<line class="stem" x1="60" y1="18" x2="60" y2="10"/>
      <g transform="rotate(45 60 5)"><rect class="light gem" x="56" y="1" width="8" height="8" rx="1.5"/></g>
      <path class="light tie" d="M60 66l-5 5 5 17 5-17z"/>`,
  },
  // The Researcher: a round head, glasses and a magnifying glass.
  researcher: {
    head: `<circle class="skin" cx="60" cy="42" r="27"/>`,
    extras: `<g class="glasses"><circle cx="52" cy="42" r="7.5"/><circle cx="68" cy="42" r="7.5"/></g>
      <g class="lens"><circle cx="99" cy="77" r="9"/><line x1="105.5" y1="84" x2="112" y2="92"/></g>`,
  },
  // The Designer: a beret and a pencil.
  designer: {
    head: `<rect class="skin" x="32" y="18" width="56" height="48" rx="19"/>`,
    extras: `<ellipse class="light" cx="55" cy="18" rx="25" ry="9" transform="rotate(-9 55 18)"/>
      <circle class="light" cx="59" cy="8" r="2.6"/>
      <g class="pencil"><rect class="light" x="96" y="60" width="6.5" height="26" rx="1.5"/><path class="tip" d="M96 86h6.5l-3.25 7z"/></g>`,
  },
  // The Developer: headphones and a laptop full of code.
  developer: {
    head: `<rect class="skin" x="30" y="19" width="60" height="47" rx="15"/>`,
    extras: `<path class="band" d="M31 42C31 7 89 7 89 42"/>
      <rect class="light" x="23" y="35" width="10" height="19" rx="5"/>
      <rect class="light" x="87" y="35" width="10" height="19" rx="5"/>
      <rect class="screen" x="39" y="82" width="42" height="27" rx="5"/>
      <rect class="code c1" x="45" y="89" width="18" height="2.6" rx="1.3"/>
      <rect class="code c2" x="45" y="94.5" width="27" height="2.6" rx="1.3"/>
      <rect class="code c3" x="45" y="100" width="12" height="2.6" rx="1.3"/>`,
  },
  // The Marketer: a megaphone.
  marketer: {
    head: `<rect class="skin" x="33" y="17" width="54" height="49" rx="24"/>`,
    extras: `<g class="horn"><path class="light" d="M88 70l20-11v31l-20-10z"/><rect class="light" x="84" y="69" width="6" height="12" rx="2"/></g>
      <path class="wave w1" d="M112 66q5 8.5 0 17"/>
      <path class="wave w2" d="M116 61q8 13.5 0 27"/>`,
  },
  // QA: a monocle and a clipboard with a check mark.
  qa: {
    head: `<path class="skin" d="M47 21h26l12 12v18l-12 12H47L35 51V33z"/>`,
    extras: `<circle class="monocle" cx="68" cy="42" r="8"/>
      <rect class="light board" x="43" y="80" width="34" height="29" rx="5"/>
      <rect class="clip" x="54" y="77" width="12" height="6" rx="2"/>
      <path class="check" d="M51 95l6 6 12-13"/>`,
  },
};

function drawCharacter(role, { avatar = false } = {}) {
  const parts = CHARACTERS[role] ?? CHARACTERS.ceo;
  const holder = document.createElement("template");
  // An avatar is the same drawing, framed on the head and shoulders.
  const frame = avatar ? 'class="face" viewBox="22 2 76 76"' : 'class="char" viewBox="0 0 120 120"';
  holder.innerHTML = `<svg ${frame} aria-hidden="true">${
    BODY.replace("HEAD", parts.head).replace("EXTRAS", parts.extras)}</svg>`;
  return holder.content.firstElementChild;
}

const ROLE_BY_TITLE = { CEO: "ceo", Researcher: "researcher", Designer: "designer", Developer: "developer", Marketer: "marketer", QA: "qa" };

/** A small round portrait: an agent's face, or a monogram for Bottega and for you. */
function avatar(who, size = "") {
  const role = ROLE_BY_TITLE[who] ?? who;
  const node = el("span", { class: `av ${size}`.trim(), "aria-hidden": "true" });
  if (CHARACTERS[role]) {
    node.style.setProperty("--c", `var(--${role})`);
    node.append(drawCharacter(role, { avatar: true }));
  } else {
    node.classList.add("mono");
    node.textContent = who === "Founder" ? "You" : "B";
  }
  return node;
}

// What the speech bubble says while an agent uses a tool.
function describeTool(tool, input = {}) {
  const file = input.path ? input.path.split("/").pop() : "";
  switch (tool) {
    case "write_file": return `Writing ${file}`;
    case "read_file": return `Reading ${file}`;
    case "list_files": return "Looking at the files";
    case "search_memory": return "Searching memory";
    case "read_memory": return "Reading a note";
    case "save_memory": return "Saving a note";
    case "create_task": return "Adding a task";
    case "update_task": return "Handing in";
    case "review_task": return input.verdict === "revise" ? "Sending it back" : "Approving";
    case "post_message": return "Writing to the team";
    case "update_brief": return "Updating the brief";
    case "finish_project": return "Closing the project";
    default: return "Working";
  }
}

// ------------------------------------------------------------------ state

const state = {
  snapshot: null,        // api/state: the whole company
  detail: null,          // api/projects/<id>: the project being looked at
  selectedId: null,
  lastEventId: 0,        // highest event id handled (used to skip duplicates)
  bubbles: {},           // agent title -> what its speech bubble says
  tab: "board",
  openTask: null,        // id of the task card that is expanded
  agentFilter: null,     // show only this agent in the feed
  message: null,         // { text, tone } shown under the goal box
  stopRequested: false,
  // `picked` is true once the user chose a file; until then we show the best one.
  file: { path: null, picked: false, mode: "preview", width: "desktop", stamp: 0, shown: null, content: "", renderedDoc: null },
  notes: [],
  note: { path: null, title: "", content: "" },
  sseAlive: false,
  lastSseAt: 0,
  reachable: true,
};

async function api(path, options) {
  const response = await fetch(path, options);
  let body = null;
  try { body = await response.json(); } catch { /* no JSON body */ }
  if (!response.ok) {
    const detail = body && body.detail;
    throw new Error(typeof detail === "string" ? detail : `Request failed (${response.status})`);
  }
  return body;
}

const agentByTitle = (title) => state.snapshot?.agents.find((a) => a.title === title);
const agentByRole = (role) => state.snapshot?.agents.find((a) => a.name === role);
const colorOf = (title) => {
  const agent = agentByTitle(title);
  return agent ? `var(--${agent.name})` : "var(--muted)";
};
const isRunning = () => state.detail?.status === "running";

// --------------------------------------------------------------- rendering

function render() {
  if (!state.snapshot) return;
  renderTop();
  renderCommand();
  renderAgents();
  renderTabs();
  renderBoard();
  renderFiles();
  renderMemory();
  renderCosts();
  renderComposer();
}

function setMeter(meter, fill, ratio) {
  meter.dataset.level = ratio >= 1 ? "over" : ratio >= 0.7 ? "warn" : "ok";
  meter.setAttribute("aria-valuenow", Math.min(1, ratio).toFixed(2));
  fill.style.width = `${Math.min(100, ratio * 100)}%`;
}

let switcherSignature = "";

// The online preview has no server: replay.js stands in for it and defines this helper.
const PREVIEW_ONLY = typeof window.bottegaPreviewDoc === "function";

const CONNECTION = {
  demo: "Demo · scripted answers, nothing is billed",
  preview: "Online preview · a recorded demo run",
  checking: "Checking Claude…",
  missing_key: "API key missing",
  error: "Claude not reachable",
};

function renderTop() {
  const { settings, projects } = state.snapshot;
  const connection = state.snapshot.connection ?? { status: "checking" };
  $("mode-badge").dataset.status = connection.status;
  $("mode-badge").title = connection.message ?? "";
  $("mode-text").textContent = CONNECTION[connection.status] ?? `Claude ready · ${settings.model}`;

  // The meter shows one ratio against a limit: this project's cost vs its budget.
  const cost = state.detail?.cost_usd ?? 0;
  $("spend-value").textContent = fmtCost(cost);
  $("spend-limit").textContent = `of ${fmtCost(settings.max_cost_usd)}`;
  setMeter($("spend-meter"), $("spend-fill"), cost / settings.max_cost_usd);

  // Rebuild the project menu only when the list really changed, so an open
  // menu is not closed under the user's finger by a refresh.
  const signature = projects.map((p) => `${p.id}:${p.status}`).join("|");
  if (signature !== switcherSignature) {
    switcherSignature = signature;
    $("project-select").replaceChildren(...projects.map((p) =>
      el("option", { value: p.id, text: `${STATUS[p.status] ?? p.status} · ${clip(p.goal, 64)}` })));
  }
  $("switcher").hidden = projects.length < 2;
  if (state.selectedId) $("project-select").value = state.selectedId;
}

function renderLink() {
  const [name, label] = state.sseAlive ? ["live", "Live"]
    : state.reachable ? ["polling", "Reconnecting"] : ["offline", "Offline"];
  $("link").dataset.state = name;
  $("link-text").textContent = label;
}

function describeProject(project) {
  if (!project) {
    return { text: state.snapshot.demo
      ? "Demo mode: type any goal and press Start. The team follows a script, so nothing is billed."
      : "Describe a goal. The CEO plans the work and the team gets started." };
  }
  const progress = `${project.tasks_done} of ${plural(project.tasks_total, "task")} done`;
  switch (project.status) {
    case "running":
      if (project.phase === "planning") return { text: "The CEO is planning the work." };
      if (project.phase === "wrap_up") return { text: `${progress}. The CEO is checking the result.` };
      return { text: `${progress}.` };
    case "done":
      return { text: `Finished: ${plural(project.tasks_total, "task")} approved by QA, ` +
        `${plural(project.files.length, "file")} delivered, ${fmtCost(project.cost_usd)}.` };
    case "failed":
      return { text: project.error ?? "The project failed.", tone: "error" };
    case "incomplete":
      return { text: project.error ?? `Finished with problems: ${progress}.`, tone: "warn" };
    case "over_budget":
      return { text: `Stopped: the budget is used up. ${progress}.`, tone: "warn" };
    default:
      return { text: `Stopped. ${progress}.` };
  }
}

function renderCommand() {
  const { busy, connection } = state.snapshot;
  if (!busy) state.stopRequested = false;
  const blocked = Boolean(connection?.blocking);
  $("start").hidden = busy;
  $("start").disabled = blocked;
  $("stop").hidden = !busy;
  $("stop").disabled = state.stopRequested;
  $("stop").textContent = state.stopRequested ? "Stopping…" : "Stop";
  const goal = $("goal");
  goal.disabled = busy;
  if (busy) {
    const running = state.snapshot.projects.find((p) => p.status === "running");
    goal.value = running?.goal ?? "";
  } else if (goal.dataset.wasBusy === "true") {
    goal.value = "";   // the project just ended: make room for the next goal
  }
  goal.dataset.wasBusy = String(busy);

  const status = state.message
    ?? (blocked && !busy ? { text: `${connection.message} Then restart Bottega.`, tone: "error" } : null)
    ?? describeProject(state.detail);
  $("status-line").textContent = status.text;
  $("status-line").dataset.tone = status.tone ?? "info";
  renderStrip();
}

const TASK_STATUS = { todo: "to do", in_progress: "in progress", review: "in review", done: "approved", failed: "failed" };

/** One small bar per task, in its owner's color: empty, pulsing, half full, full. */
function renderStrip() {
  const tasks = state.detail?.tasks ?? [];
  $("task-strip").hidden = !tasks.length;
  $("task-strip").replaceChildren(...tasks.map((task) => {
    const pip = el("li", { class: "pip", "data-status": task.status,
      title: `${task.id} · ${task.title} · ${TASK_STATUS[task.status] ?? task.status}` });
    pip.style.setProperty("--c", `var(--${task.owner})`);
    return pip;
  }));
}

function setMessage(text, tone) {
  state.message = text ? { text, tone } : null;
  if (state.snapshot) renderCommand();
}

// ---- the studio: one station per agent ----------------------------------

// Stations are built once and updated in place. Rebuilding them on every
// refresh would restart the characters' animations each time.
const stations = new Map();

function buildStation(agent, index) {
  const bubbleText = el("i");
  const caption = el("span", { class: "caption" });
  const node = el("button", {
    class: "station", type: "button", "aria-pressed": "false",
    title: `${agent.description} Click to see only the ${agent.title}'s activity.`,
    onclick: () => setAgentFilter(state.agentFilter === agent.title ? null : agent.title),
  },
    el("span", { class: "spot", "aria-hidden": "true" }),
    el("span", { class: "bubble" }, el("span", {}, bubbleText)),
    drawCharacter(agent.name),
    el("span", { class: "stand", "aria-hidden": "true" }),
    el("span", { class: "name", text: agent.title }),
    caption,
  );
  node.style.setProperty("--c", `var(--${agent.name})`);
  node.style.setProperty("--delay", `${(index * 0.9) % 5}s`);   // so they don't all blink together
  return { node, bubbleText, caption };
}

function captionFor(agent) {
  if (agent.status === "working") {
    if (agent.task_id) {
      const verb = agent.doing === "reviewing" ? "Reviewing" : "Working on";
      return `${verb} ${agent.task_id}: ${agent.task_title}`;
    }
    return agent.doing === "planning" ? "Planning the project" : "Checking the result";
  }
  const tasks = (state.detail?.tasks ?? []).filter((t) => t.owner === agent.name);
  const next = tasks.find((t) => t.status === "todo");
  if (next && isRunning()) {
    return next.revisions ? `Up next: ${next.id}, with QA's fixes` : `Up next: ${next.id}`;
  }
  const done = tasks.filter((t) => t.status === "done").length;
  if (done) return `${plural(done, "task")} done`;
  return isRunning() ? "Standing by" : "Ready";
}

function renderAgents() {
  state.snapshot.agents.forEach((agent, index) => {
    let station = stations.get(agent.name);
    if (!station) {
      station = buildStation(agent, index);
      stations.set(agent.name, station);
      $("agents").append(station.node);
    }
    station.node.dataset.status = agent.status;
    station.node.setAttribute("aria-pressed", String(state.agentFilter === agent.title));
    station.bubbleText.textContent = state.bubbles[agent.title] ?? "Thinking…";
    station.caption.textContent = captionFor(agent);
  });
}

// ---- tabs ----------------------------------------------------------------

const TABS = ["board", "files", "memory", "costs"];

function renderTabs() {
  for (const tab of TABS) {
    $(`tab-${tab}`).setAttribute("aria-selected", String(state.tab === tab));
    $(`panel-${tab}`).hidden = state.tab !== tab;
  }
  $("n-board").textContent = state.detail?.tasks.length || "";
  $("n-files").textContent = state.detail?.files.length || "";
  $("n-memory").textContent = state.notes.length || "";
}

// ---- board ---------------------------------------------------------------

function taskCard(task) {
  const owner = agentByRole(task.owner);
  const limit = state.snapshot.settings.max_revisions;
  const meta = [];
  if (task.status === "failed") meta.push(el("span", { class: "flag failed", text: "Failed" }));
  if (task.status === "done") meta.push(el("span", { class: "flag approved", text: "Approved by QA" }));
  if (task.revisions) meta.push(el("span", { class: "flag revised", text: `Sent back ${task.revisions} of ${limit}` }));
  if (task.depends_on.length && task.status === "todo") {
    meta.push(el("span", { text: `After ${task.depends_on.join(", ")}` }));
  }

  const more = el("dl", { class: "card-more" },
    el("dt", { text: "What was asked" }), el("dd", { text: task.description }));
  const add = (label, text) => text && more.append(el("dt", { text: label }), el("dd", { text }));
  add("Delivered", task.output);
  task.feedback.forEach((text, i) => add(`QA asked for changes (round ${i + 1})`, text));
  add("QA's note", task.review_note);
  add("Why it failed", task.error);

  const card = el("button", {
    class: "card", type: "button", "data-status": task.status,
    "aria-expanded": String(state.openTask === task.id),
    onclick: () => { state.openTask = state.openTask === task.id ? null : task.id; renderBoard(); },
  },
    el("span", { class: "card-top" },
      el("span", { class: "card-id", text: task.id }),
      el("span", { class: "owner" }, avatar(task.owner, "xs"), document.createTextNode(owner?.title ?? task.owner))),
    el("span", { class: "card-title", text: task.title }),
    meta.length ? el("span", { class: "card-meta" }, ...meta) : null,
    more,
  );
  card.style.setProperty("--c", `var(--${task.owner})`);
  return card;
}

const EXAMPLES = [
  "A landing page for a pizzeria in Mantova, Italy",
  "A website for a bike repair shop in Bologna",
  "A launch page for a handmade ceramics studio in Lisbon",
];

const STEPS = [
  ["The CEO plans", "The goal becomes a handful of tasks, each with an owner and an order."],
  ["The team builds", "Agents work in parallel, share decisions in the brief and write real files."],
  ["QA checks everything", "Every task is reviewed and sent back, up to twice, until it holds up."],
];

function startScreen() {
  const pick = (goal) => { $("goal").value = goal; $("goal").focus(); setMessage(""); };
  return el("div", { class: "start" },
    el("h3", { text: "Try one of these, or write your own:" }),
    el("div", { class: "examples" }, ...EXAMPLES.map((goal) =>
      el("button", { class: "example", type: "button", text: goal, onclick: () => pick(goal) }))),
    el("div", { class: "steps" }, ...STEPS.map(([title, text]) =>
      el("div", { class: "step" }, el("b", { text: title }), el("p", { text })))));
}

/** The CEO's closing summary for the client, once the project is over. */
function summaryBlock(project) {
  const text = project.summary?.trim();
  if (project.status === "running" || !text) return null;
  const body = el("p", { class: "summary-text", text, title: "Click to expand" });
  body.addEventListener("click", () => body.classList.toggle("open"));
  return el("section", { class: "summary" },
    el("h3", { text: project.status === "done" ? "Summary for the client" : "Where things stand" }), body);
}

function renderBoard() {
  const panel = $("panel-board");
  const tasks = state.detail?.tasks ?? [];
  if (!state.detail && !state.snapshot.busy) {
    if (!panel.querySelector(".start")) panel.replaceChildren(startScreen());
    return;
  }
  if (!tasks.length) {
    const text = isRunning() ? "The CEO is breaking the goal into tasks…"
      : "This project ended before any task was planned.";
    panel.replaceChildren(el("p", { class: "empty", text }));
    return;
  }
  const columns = COLUMNS.map((column) => {
    const mine = tasks.filter((t) => column.holds.includes(t.status));
    return el("section", { class: "column" },
      el("h3", {}, document.createTextNode(column.title), el("span", { text: String(mine.length) })),
      el("div", { class: "cards" }, ...mine.map(taskCard)));
  });
  // replaceChildren would turn a missing summary (null) into the text "null".
  panel.replaceChildren(...[summaryBlock(state.detail), ...columns].filter(Boolean));
}

// ---- files ---------------------------------------------------------------

const isPage = (path) => /\.html?$/i.test(path ?? "");
const previewUrl = (projectId, path, stamp) =>
  `api/projects/${encodeURIComponent(projectId)}/preview/${path.split("/").map(encodeURIComponent).join("/")}?v=${stamp}`;

function renderFiles() {
  const files = state.detail?.files ?? [];
  const { file } = state;
  $("file-list").replaceChildren(...(files.length ? files.map((path) =>
    el("li", {}, el("button", {
      class: "item", type: "button", "aria-current": path === file.path ? "true" : null,
      onclick: () => { file.path = path; file.picked = true; file.mode = "preview"; renderFiles(); loadFile(); },
    }, el("span", { class: "item-name", text: path })))) :
    [el("li", { class: "empty", text: state.detail ? "No files yet." : "Files the team writes will appear here." })]));

  const project = state.detail;
  const page = isPage(file.path);
  const doc = isDoc(file.path);
  const preview = file.mode === "preview";
  $("file-title").textContent = file.path ?? "";
  $("file-mode").hidden = !(page || doc);
  for (const button of $("file-mode").children) {
    button.setAttribute("aria-pressed", String(button.dataset.mode === file.mode));
  }
  $("file-width").hidden = !(page && preview);
  for (const button of $("file-width").children) {
    button.setAttribute("aria-pressed", String(button.dataset.width === file.width));
  }
  $("frame-stage").hidden = !(page && preview);
  $("frame-stage").dataset.width = file.width;
  $("open-tab").hidden = !page || PREVIEW_ONLY;
  if (page && project) $("open-tab").href = previewUrl(project.id, file.path, file.stamp);
  $("download").hidden = !files.length || PREVIEW_ONLY;
  if (project) $("download").href = `api/projects/${encodeURIComponent(project.id)}/download`;

  // Re-render a document only when its text changed, so scrolling isn't reset.
  const text = file.path ? file.content : "";
  $("file-doc").hidden = !(doc && preview);
  if (doc && preview && file.renderedDoc !== text) {
    $("file-doc").replaceChildren(Markdown.render(text));
    file.renderedDoc = text;
  }
  $("file-text").hidden = (page || doc) && preview;
  if ($("file-text").textContent !== text) $("file-text").textContent = text;
}

const isDoc = (path) => /\.(md|markdown)$/i.test(path ?? "");

/** The file most worth showing: the web page the client will open, then the
    research, then any other document. The brief is bookkeeping, so it comes last. */
function bestFile(files) {
  const rank = (f) => (f.endsWith("index.html") ? 0 : f === "research.md" ? 1
    : f.endsWith(".md") ? 2 : f === "project_brief.json" ? 4 : 3);
  return [...files].sort((a, b) => rank(a) - rank(b))[0] ?? null;
}

/** Keep the open file in step with the project: pick one, and reload it when it changes. */
async function loadFile() {
  const { file } = state;
  const project = state.detail;
  const files = project?.files ?? [];
  if (!file.picked || !files.includes(file.path)) {
    const best = bestFile(files);
    if (best !== file.path) { file.path = best; file.mode = "preview"; }
  }
  // A document needs its text in both modes; a web page only in Source mode.
  const needsText = !isPage(file.path) || file.mode === "code";
  const key = file.path ? `${project.id}|${file.path}|${file.stamp}|${needsText ? "text" : "frame"}` : null;
  if (key === file.shown) return;
  file.shown = key;
  if (!file.path) { file.content = ""; renderFiles(); return; }

  if (isPage(file.path) && file.mode === "preview") {
    const frame = $("file-frame");
    // The online preview has no server: there, a helper hands us the page itself.
    const doc = window.bottegaPreviewDoc?.(project.id, file.path);
    if (doc !== undefined) frame.srcdoc = doc;
    else frame.src = previewUrl(project.id, file.path, file.stamp);
    renderFiles();
    return;
  }
  try {
    const body = await api(`api/projects/${encodeURIComponent(project.id)}/file?path=${encodeURIComponent(file.path)}`);
    if (file.shown === key) file.content = body.content;
  } catch (error) {
    if (file.shown === key) file.content = `Could not open this file: ${error.message}`;
  }
  renderFiles();
}

// ---- memory --------------------------------------------------------------

// Notes start with a "frontmatter" block (author, date, tags). The list already
// shows that information, so the viewer hides the block and shows the text.
const FRONTMATTER = /^---\n[\s\S]*?\n---\n*/;

function renderMemory() {
  const fresh = new Set(state.detail?.new_notes ?? []);
  $("note-list").replaceChildren(...(state.notes.length ? state.notes.map((note) =>
    el("li", {}, el("button", {
      class: "item", type: "button", "aria-current": note.path === state.note.path ? "true" : null,
      onclick: () => openNote(note),
    },
      el("span", { class: "item-name" }, document.createTextNode(note.title),
        fresh.has(note.path) ? el("span", { class: "new", text: "new" }) : null),
      el("span", { class: "item-sub", text: [note.folder || "top level", ...note.tags.slice(0, 3).map((t) => `#${t}`)].join(" · ") })))) :
    [el("li", { class: "empty", text: "The company's memory is empty." })]));
  $("note-title").textContent = state.note.title;
  const text = state.note.path ? state.note.content : null;
  if (state.note.rendered !== text) {
    state.note.rendered = text;
    $("note-text").replaceChildren(text === null
      ? el("p", { class: "empty", text: "What the company remembers across projects. The same folder opens in Obsidian." })
      : Markdown.render(text));
  }
}

async function openNote(note) {
  state.note = {
    path: note.path, content: "Loading…",
    title: [note.path, note.author && `by ${note.author}`, note.updated].filter(Boolean).join(" · "),
  };
  renderMemory();
  let content;
  try {
    content = (await api(`api/memory/note?path=${encodeURIComponent(note.path)}`)).content.replace(FRONTMATTER, "");
  } catch (error) {
    content = `Could not open this note: ${error.message}`;
  }
  if (state.note.path === note.path) { state.note.content = content; renderMemory(); }
}

async function loadMemory() {
  try {
    state.notes = (await api("api/memory")).notes;
    if (state.snapshot) { renderTabs(); renderMemory(); }
  } catch { /* the watchdog reports connection problems */ }
}

// ---- costs ---------------------------------------------------------------

function renderCosts() {
  const panel = $("panel-costs");
  const { settings, totals, agents, demo } = state.snapshot;
  const project = state.detail;
  if (!project) {
    panel.replaceChildren(el("p", { class: "empty", text: "What each agent spends will appear here." }));
    return;
  }
  const ratio = project.cost_usd / settings.max_cost_usd;
  const meter = el("div", { class: "meter", role: "meter", "aria-label": "Project budget used",
    "aria-valuemin": "0", "aria-valuemax": "1" }, el("div", { class: "meter-fill" }));
  setMeter(meter, meter.firstChild, ratio);

  // One row per agent: a name, a bar and a figure. Bars share one scale.
  const rows = agents.map((agent) => ({ agent, spend: project.costs[agent.title] }));
  const top = Math.max(...rows.map((r) => r.spend?.cost_usd ?? 0), 0.000001);
  const grid = el("div", { class: "cost-rows" });
  for (const { agent, spend } of rows) {
    const cost = spend?.cost_usd ?? 0;
    const tokens = spend ? spend.input_tokens + spend.output_tokens + spend.cache_read_tokens + spend.cache_write_tokens : 0;
    const name = el("span", { class: "cost-name" }, avatar(agent.name, "sm"), document.createTextNode(agent.title));
    const bar = el("div", { class: "cost-bar" }, el("div"));
    bar.firstChild.style.width = `${(cost / top) * 100}%`;
    bar.firstChild.hidden = !cost;
    for (const node of [name, bar]) node.style.setProperty("--c", `var(--${agent.name})`);
    grid.append(name, bar,
      el("span", { class: "cost-figure" }, document.createTextNode(fmtCost(cost)),
        el("small", { text: spend ? `${plural(spend.calls, "call")} · ${fmtCount(tokens)} tokens` : "no activity" })));
  }

  panel.replaceChildren(
    el("p", { class: "cost-hero", text: fmtCost(project.cost_usd) }),
    el("p", { class: "cost-sub", text:
      `This project${demo ? " (simulated)" : ""} · ${Math.round(ratio * 100)}% of the ${fmtCost(settings.max_cost_usd)} budget` }),
    meter,
    grid,
    el("p", { class: "cost-foot", text:
      `All projects: ${fmtCost(totals.cost_usd)} · ${plural(totals.calls, "call")} to Claude · ` +
      `${fmtCount(totals.input_tokens)} tokens in, ${fmtCount(totals.output_tokens)} out` }),
  );
}

// -------------------------------------------------------------- activity

const FEED_LIMIT = 500;
const TASK_VERB = { created: "Planned", started: "Started", submitted: "Handed in", released: "Put back in the queue" };

/** Describe an event for the feed, or null if it shouldn't be listed.
    `level` is "highlight" (the story of the project) or "detail" (every step). */
function describe(event) {
  const d = event.data ?? {};
  const highlight = (text, extra = {}) => ({ level: "highlight", text, ...extra });
  const detail = (text, extra = {}) => ({ level: "detail", text, ...extra });
  switch (event.kind) {
    case "project_start":
      return highlight(`New project: ${d.goal}`, { icon: "▸" });
    case "project_done": {
      const tone = d.status === "done" ? "good" : d.status === "failed" ? "bad" : "warn";
      const icon = tone === "good" ? "✓" : tone === "bad" ? "✕" : "▲";
      const text = `${STATUS[d.status] ?? d.status} · ${fmtCost(d.cost_usd)} · ${plural(d.files, "file")}`;
      return highlight(d.error ? `${text}\n${d.error}` : text, { icon, tone });
    }
    case "stop_requested":
      return highlight("Stop requested. Each agent stops after its current step.", { icon: "■" });
    case "task": {
      const task = d.task;
      const name = `${task.id} · ${task.title}`;
      if (d.change === "approved") return highlight(`Approved ${name}`, { icon: "✓", tone: "good" });
      if (d.change === "revision") return highlight(`Sent back ${name}\n${task.feedback.at(-1)}`, { icon: "↩", tone: "warn" });
      if (d.change === "failed") return highlight(`${name} failed\n${task.error}`, { icon: "✕", tone: "bad" });
      if (d.change === "created") {
        return highlight(`Planned ${name} for the ${agentByRole(task.owner)?.title ?? task.owner}`);
      }
      const verb = TASK_VERB[d.change] ?? d.change;
      return d.change === "released" ? detail(`${verb}: ${name}`) : highlight(`${verb} ${name}`);
    }
    case "message":
      return highlight(d.text, { to: d.to });
    case "brief":
      return detail(`Updated the brief: ${d.key}`);
    case "agent_start":
      return detail("Picked up the work");
    case "text":
      return detail(d.text);
    case "tool_call": {
      const args = Object.entries(d.input ?? {})
        .map(([key, value]) => `${key}: ${clip(oneLine(typeof value === "string" ? value : JSON.stringify(value)), 70)}`)
        .join(" · ");
      return detail(`${d.tool}  ${args}`, { icon: "▸" });
    }
    case "tool_result":
      if (!d.ok) return highlight(`${d.tool} did not work: ${clip(oneLine(d.output), 200)}`, { icon: "▲", tone: "warn" });
      if (d.tool === "write_file") return highlight(`Wrote ${d.input?.path ?? "a file"}`);
      if (d.tool === "save_memory") return highlight(`Saved a note to memory: ${d.input?.title ?? ""}`);
      return detail(clip(oneLine(d.output), 200), { icon: "✓" });
    case "agent_done":
      return detail(`Finished this turn · ${fmtCost(d.cost_usd)}`);
    case "error":
      return highlight(d.message, { icon: "✕", tone: "bad" });
    default:
      return null;   // e.g. "usage": only a signal to refresh the numbers
  }
}

function appendFeed(event) {
  const info = describe(event);
  if (!info) return;
  const list = $("feed");
  // Only follow new lines if the reader is already at the bottom.
  const pinned = list.scrollHeight - list.scrollTop - list.clientHeight < 80;

  const text = el("p", { class: "row-text", title: "Click to expand" });
  if (info.icon) text.append(el("span", { class: "icon", "aria-hidden": "true", text: info.icon }));
  text.append(document.createTextNode(info.text ?? ""));
  text.addEventListener("click", () => text.classList.toggle("open"));

  const who = info.to && info.to !== "all" ? `${event.agent} to ${info.to}` : event.agent;
  const row = el("li", {
    class: "row", "data-kind": event.kind, "data-level": info.level,
    "data-agent": event.agent, "data-tone": info.tone ?? null,
  },
    avatar(event.agent),
    el("div", {},
      el("p", { class: "row-head" }, el("b", { text: who }), el("time", { text: fmtTime(event.ts) })),
      text));
  row.style.setProperty("--c", colorOf(event.agent));
  row.hidden = Boolean(state.agentFilter) && event.agent !== state.agentFilter;

  list.append(row);
  while (list.children.length > FEED_LIMIT) list.firstElementChild.remove();
  $("feed-empty").hidden = true;
  if (pinned) list.scrollTop = list.scrollHeight;
}

function setAgentFilter(title) {
  state.agentFilter = title;
  for (const row of $("feed").children) row.hidden = Boolean(title) && row.dataset.agent !== title;
  $("agent-filter").hidden = !title;
  $("agent-filter-name").textContent = title ?? "";
  // Looking at one agent means wanting every step it takes.
  if (title) setFeedFilter("all");
  renderAgents();
  $("feed").scrollTop = $("feed").scrollHeight;
}

function setFeedFilter(filter) {
  $("feed").dataset.filter = filter;
  for (const button of $("feed-filter").children) {
    button.setAttribute("aria-pressed", String(button.dataset.filter === filter));
  }
  $("feed").scrollTop = $("feed").scrollHeight;
}

function renderComposer() {
  const running = isRunning();
  $("message").disabled = !running;
  $("send").disabled = !running;
  $("message").placeholder = running ? "Tell the team something…" : "Opens while the team works";
}

// ------------------------------------------------------------- handoffs
// When work changes hands, a sheet of paper flies between the two agents:
// the CEO hands out a task, the owner hands it to QA, QA approves it or sends
// it back. It is the board's story told with the characters themselves.

const REDUCED_MOTION = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

function fly(fromRole, toRole, colorRole, mark = "") {
  const from = stations.get(fromRole)?.node.querySelector(".char");
  const to = stations.get(toRole)?.node.querySelector(".char");
  if (REDUCED_MOTION || !from || !to || fromRole === toRole || typeof document.body.animate !== "function") return;
  const a = from.getBoundingClientRect();
  const b = to.getBoundingClientRect();
  if (!a.width || !b.width) return;   // the studio is not on screen
  const sheet = el("span", { class: "sheet", "aria-hidden": "true" }, el("i"), el("i"), el("i"));
  if (mark) sheet.append(el("b", { text: mark }));
  sheet.style.setProperty("--c", `var(--${colorRole})`);
  document.body.append(sheet);
  const x0 = a.left + a.width / 2 - 11;
  const y0 = a.top + a.height * 0.45;
  const x1 = b.left + b.width / 2 - 11;
  const y1 = b.top + b.height * 0.45;
  const lift = Math.min(90, 40 + Math.abs(x1 - x0) / 6);
  sheet.animate([
    { transform: `translate(${x0}px, ${y0}px) scale(0.5) rotate(-8deg)`, opacity: 0 },
    { transform: `translate(${x0}px, ${y0 - 26}px) scale(1) rotate(-4deg)`, opacity: 1, offset: 0.18 },
    { transform: `translate(${(x0 + x1) / 2}px, ${Math.min(y0, y1) - lift}px) scale(1.05) rotate(6deg)`, opacity: 1, offset: 0.55 },
    { transform: `translate(${x1}px, ${y1 - 10}px) scale(0.85) rotate(0deg)`, opacity: 1, offset: 0.9 },
    { transform: `translate(${x1}px, ${y1}px) scale(0.4)`, opacity: 0 },
  ], { duration: 1150, easing: "cubic-bezier(.45,.05,.35,1)" }).finished.finally(() => sheet.remove());
}

function flyForTaskChange(change, task, event) {
  if (event.ts < Date.now() / 1000 - 5) return;   // only live events, not replayed history
  if (change === "created") fly("ceo", task.owner, task.owner);
  else if (change === "submitted") fly(task.owner, "qa", task.owner);
  else if (change === "approved") fly("qa", task.owner, task.owner, "✓");
  else if (change === "revision") fly("qa", task.owner, task.owner, "↩");
}

// ------------------------------------------------------------------ events

/** A short reaction from a character: a hop when its work is approved, a
    shake when QA sends it back. Only for live events, not replayed history. */
function react(role, kind, event) {
  if (event.ts < Date.now() / 1000 - 5) return;
  const station = stations.get(role);
  if (!station) return;
  station.node.classList.remove("cheer", "oops");
  void station.node.offsetWidth;   // restart the animation if it is already running
  station.node.classList.add(kind);
  clearTimeout(station.reaction);
  station.reaction = setTimeout(() => station.node.classList.remove(kind), 1400);
}

function handleEvent(event) {
  if (event.id <= state.lastEventId) return;   // already seen (replay or polling overlap)
  state.lastEventId = event.id;
  appendFeed(event);

  const d = event.data ?? {};
  switch (event.kind) {
    case "project_start":
      state.message = null;
      state.bubbles = {};
      state.openTask = null;
      state.selectedId = event.project_id;   // follow the new project
      state.file.picked = false;
      break;
    case "agent_start":
      state.bubbles[event.agent] = "Thinking…";
      break;
    case "tool_call":
      state.bubbles[event.agent] = describeTool(d.tool, d.input);
      break;
    case "tool_result":
      if (d.ok && d.tool === "write_file") state.file.stamp += 1;   // reload the open file
      if (d.ok && d.tool === "save_memory") loadMemory();
      break;
    case "agent_done":
      delete state.bubbles[event.agent];
      break;
    case "task":
      flyForTaskChange(d.change, d.task, event);
      // The reaction lands as the sheet arrives.
      if (d.change === "approved") setTimeout(() => react(d.task.owner, "cheer", event), REDUCED_MOTION ? 0 : 1000);
      if (d.change === "revision") setTimeout(() => react(d.task.owner, "oops", event), REDUCED_MOTION ? 0 : 1000);
      break;
    case "project_done":
      if (d.status === "done") for (const role of stations.keys()) react(role, "cheer", event);
      break;
  }
  // "text" and "tool_call" never change the snapshot, only the speech bubbles.
  if (event.kind === "text" || event.kind === "tool_call") {
    if (state.snapshot) renderAgents();
  } else {
    scheduleRefresh();
  }
}

let refreshTimer = null;

/** Many events can arrive at once; fetch the snapshot once for the whole burst. */
function scheduleRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(async () => {
    refreshTimer = null;
    try { await refresh(); } catch { /* the watchdog reports connection problems */ }
  }, 120);
}

let refreshCount = 0;

async function refresh() {
  const ticket = ++refreshCount;
  const snapshot = await api("api/state");
  const known = snapshot.projects.map((p) => p.id);
  let selectedId = state.selectedId;
  if (!known.includes(selectedId)) {
    selectedId = (snapshot.projects.find((p) => p.status === "running") ?? snapshot.projects[0])?.id ?? null;
  }
  const detail = selectedId ? await api(`api/projects/${encodeURIComponent(selectedId)}`) : null;
  if (ticket !== refreshCount) return;   // a newer refresh started meanwhile: let it win

  state.snapshot = snapshot;
  state.detail = detail;
  state.selectedId = selectedId;
  state.reachable = true;
  render();
  loadFile();
}

function connectEvents() {
  const source = new EventSource(`api/events?after=${state.lastEventId}`);
  const alive = () => {
    state.lastSseAt = Date.now();
    if (!state.sseAlive) { state.sseAlive = true; renderLink(); }
  };
  source.addEventListener("ping", alive);
  source.onmessage = (message) => { alive(); handleEvent(JSON.parse(message.data)); };
  // EventSource reconnects by itself; we only note that the link is down.
  source.onerror = () => { state.sseAlive = false; renderLink(); };
}

/** Safety net: if the live stream is silent (blocked or buffered by a proxy),
    fall back to asking for new events every two seconds. */
async function watchdog() {
  if (Date.now() - state.lastSseAt > 25000) state.sseAlive = false;
  if (!state.sseAlive) {
    try {
      const { events } = await api(`api/events/history?after=${state.lastEventId}`);
      events.forEach(handleEvent);
      if (!state.snapshot) await refresh();
      state.reachable = true;
    } catch {
      state.reachable = false;
    }
  }
  renderLink();
}

// ----------------------------------------------------------------- actions

async function start(event) {
  event.preventDefault();
  const goal = $("goal").value.trim();
  if (goal.length < 3) {
    setMessage("Describe the goal in a few words first.", "error");
    $("goal").focus();
    return;
  }
  $("start").disabled = true;
  try {
    const project = await api("api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal }),
    });
    state.message = null;
    state.selectedId = project.id;
    state.tab = "board";
  } catch (error) {
    setMessage(error.message, "error");
  }
  $("start").disabled = false;
  try { await refresh(); } catch { /* the watchdog reports connection problems */ }
}

async function stop() {
  const running = state.snapshot?.projects.find((p) => p.status === "running");
  if (!running) return;
  state.stopRequested = true;
  renderCommand();
  try {
    await api(`api/projects/${encodeURIComponent(running.id)}/stop`, { method: "POST" });
  } catch (error) {
    state.stopRequested = false;
    setMessage(error.message, "error");
  }
}

async function sendMessage(event) {
  event.preventDefault();
  const text = $("message").value.trim();
  if (!text || !isRunning()) return;
  try {
    await api(`api/projects/${encodeURIComponent(state.detail.id)}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    $("message").value = "";
  } catch (error) {
    setMessage(error.message, "error");
  }
}

// -------------------------------------------------------------------- init

async function init() {
  $("command").addEventListener("submit", start);
  $("stop").addEventListener("click", stop);
  $("composer").addEventListener("submit", sendMessage);
  $("goal").addEventListener("input", () => { if (state.message?.tone === "error") setMessage(""); });
  $("agent-filter-clear").addEventListener("click", () => setAgentFilter(null));
  $("project-select").addEventListener("change", (event) => {
    state.selectedId = event.target.value;
    state.openTask = null;
    state.file.picked = false;
    refresh().catch(() => {});
  });
  for (const tab of TABS) {
    $(`tab-${tab}`).addEventListener("click", () => {
      state.tab = tab;
      renderTabs();
      if (tab === "memory" && !state.note.path && state.notes.length) openNote(state.notes[0]);
    });
  }
  for (const button of $("feed-filter").children) {
    button.addEventListener("click", () => setFeedFilter(button.dataset.filter));
  }
  for (const button of $("file-mode").children) {
    button.addEventListener("click", () => { state.file.mode = button.dataset.mode; renderFiles(); loadFile(); });
  }
  for (const button of $("file-width").children) {
    button.addEventListener("click", () => { state.file.width = button.dataset.width; renderFiles(); });
  }
  setFeedFilter("highlights");

  try { await refresh(); } catch { state.reachable = false; }
  // Replay recent events so the feed isn't empty after a page reload.
  state.lastEventId = Math.max(0, (state.snapshot?.last_event_id ?? 0) - 300);
  connectEvents();
  loadMemory();
  setInterval(watchdog, 2000);
  renderLink();
}

init();
