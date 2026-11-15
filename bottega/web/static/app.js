"use strict";

/* Bottega command center: frontend logic, plain JavaScript, no build step.

   How the page stays up to date:
     1. GET api/state returns a snapshot: agents, projects, spend. It is the
        single source of truth for everything with a number or a status.
     2. A Server-Sent Events stream (api/events) pushes each event the moment it
        happens. An event (a) adds a line to the live feed and (b) tells us
        "something changed", so we re-fetch the snapshot.
   Events are signals, the snapshot is truth. That keeps this file simple:
   we never try to rebuild totals from events, so they can't drift.

   Security rule: everything agents write is untrusted text. We only ever put
   it in the page with `textContent`, never `innerHTML`, so a model can't
   inject markup or scripts into the dashboard. */

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

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
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

const STATUS = {
  running: { icon: "◉", label: "Running" },
  done: { icon: "✓", label: "Done" },
  incomplete: { icon: "▲", label: "Incomplete" },
  over_budget: { icon: "▲", label: "Over budget" },
  stopped: { icon: "■", label: "Stopped" },
  failed: { icon: "✕", label: "Failed" },
};

const STOP_REASON = {
  end_turn: "task complete",
  cancelled: "stopped",
  budget_exceeded: "budget reached",
  max_turns: "turn limit reached",
  max_tokens: "output limit reached",
};

// ------------------------------------------------------------------ state

const state = {
  snapshot: null,          // last api/state response
  lastEventId: 0,          // highest event id handled (used to skip duplicates)
  activity: {},            // agent title -> what it is doing right now
  selectedProjectId: null,
  tab: "deliverables",
  notes: [],               // company memory index
  message: null,           // { text, tone } shown under the command bar
  stopRequested: false,
  lastWrite: null,         // { projectId, path } of the latest write_file call
  pendingOpen: null,       // file to open once the next snapshot arrives
  sseAlive: false,
  lastSseAt: 0,
  reachable: true,
};

// What the viewer shows on each tab: { key, title, content } or null.
const view = { deliverables: null, memory: null };

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

const selectedProject = () =>
  state.snapshot?.projects.find((p) => p.id === state.selectedProjectId) ?? null;

// --------------------------------------------------------------- rendering

function render() {
  if (!state.snapshot) return;
  renderTop();
  renderSpend();
  renderProjects();
  renderAgents();
  renderCommand();
  renderBrowser();
}

function renderTop() {
  const { settings, demo } = state.snapshot;
  $("sys-model").textContent = settings.model;
  $("sys-effort").textContent = settings.effort;
  $("demo-badge").hidden = !demo;
}

function renderLink() {
  const link = $("sys-link");
  const [name, label] = state.sseAlive ? ["live", "Live"]
    : state.reachable ? ["polling", "Polling"] : ["offline", "Offline"];
  link.dataset.state = name;
  $("sys-link-text").textContent = label;
}

function renderSpend() {
  const { totals, settings, projects, demo } = state.snapshot;
  $("total-cost").textContent = fmtCost(totals.cost_usd);
  document.querySelector(".hero-label").textContent =
    demo ? "Total this session (simulated)" : "Total this session";
  $("total-calls").textContent = fmtCount(totals.calls);
  $("total-in").textContent = fmtCount(totals.input_tokens);
  $("total-out").textContent = fmtCount(totals.output_tokens);

  // The meter shows one ratio against a limit: this project's cost vs its budget.
  const budget = settings.max_cost_usd;
  const current = projects.find((p) => p.status === "running") ?? projects[0];
  const ratio = current ? current.cost_usd / budget : 0;
  const meter = $("budget-meter");
  meter.dataset.level = ratio >= 1 ? "over" : ratio >= 0.7 ? "warn" : "ok";
  meter.setAttribute("aria-valuenow", Math.min(1, ratio).toFixed(2));
  $("budget-fill").style.width = `${Math.min(100, ratio * 100)}%`;
  if (!current) {
    $("budget-label").textContent = "Project budget";
    $("budget-value").textContent = `${fmtCost(budget)} per project`;
    $("budget-note").textContent = "A project stops by itself when it reaches this limit.";
  } else {
    $("budget-label").textContent = current.status === "running" ? "Current project" : "Latest project";
    $("budget-value").textContent = `${fmtCost(current.cost_usd)} of ${fmtCost(budget)}`;
    $("budget-note").textContent = ratio >= 1
      ? "Budget reached: the project was stopped."
      : `${Math.round(ratio * 100)}% of the budget used`;
  }
}

function renderProjects() {
  const { projects } = state.snapshot;
  $("projects-count").textContent = projects.length;
  $("projects-empty").hidden = projects.length > 0;
  $("projects").replaceChildren(...projects.map((project) => {
    const status = STATUS[project.status] ?? STATUS.incomplete;
    return el("li", {},
      el("button", {
        class: "project", type: "button",
        "aria-current": project.id === state.selectedProjectId ? "true" : null,
        onclick: () => selectProject(project.id),
      },
        el("span", { class: "status-icon", "data-status": project.status, "aria-hidden": "true", text: status.icon }),
        el("span", { class: "goal", text: project.goal }),
        el("span", { class: "meta", text:
          `${status.label} · ${project.agent} · ${fmtTime(project.started_at)} · ${fmtCost(project.cost_usd)}` }),
      ));
  }));
}

// Agent cards are built once and then updated in place. Rebuilding them on
// every refresh would restart the spinning-ring animation each time.
const agentCards = new Map();

const INITIALS = { ceo: "CE", researcher: "RS", designer: "DS", developer: "DV", marketer: "MK", qa: "QA" };

function buildAgentCard(agent, index) {
  const reactor = svgEl("svg", { class: "reactor", viewBox: "0 0 64 64", "aria-hidden": "true" });
  reactor.append(
    svgEl("circle", { class: "r-track", cx: 32, cy: 32, r: 28 }),
    svgEl("circle", { class: "r-arc", cx: 32, cy: 32, r: 28 }),
    svgEl("circle", { class: "r-inner", cx: 32, cy: 32, r: 21 }),
    svgEl("circle", { class: "r-core", cx: 32, cy: 32, r: 16 }),
  );
  const initials = svgEl("text", { x: 32, y: 37.5, "text-anchor": "middle" });
  initials.textContent = INITIALS[agent.name] ?? agent.title.slice(0, 2).toUpperCase();
  reactor.append(initials);

  const pillIcon = el("i", { "aria-hidden": "true" });
  const pillText = el("span");
  const activity = el("p", { class: "agent-activity" });
  const stats = el("p", { class: "agent-stats" });

  // Agents on duty are buttons: clicking one assigns the next project to it.
  const card = el(agent.available ? "button" : "div", {
    class: "agent",
    type: agent.available ? "button" : null,
    title: agent.available ? `Assign the next project to the ${agent.title}` : null,
    onclick: agent.available ? () => { $("target").value = agent.name; $("goal").focus(); } : null,
  },
    reactor,
    el("div", { class: "agent-head" },
      el("span", { class: "agent-name", text: agent.title }),
      el("span", { class: "pill" }, pillIcon, pillText)),
    el("p", { class: "agent-desc", text: agent.description, title: agent.description }),
    activity,
    stats,
  );
  // Each seat keeps the same identity color everywhere (card ring, feed dot).
  card.style.setProperty("--id", `var(--agent-${index + 1})`);
  return { card, pillIcon, pillText, activity, stats };
}

const PILL = { working: ["◉", "Working"], idle: ["●", "Ready"], offline: ["○", "Offline"] };

function renderAgents() {
  const { agents } = state.snapshot;
  agents.forEach((agent, index) => {
    let card = agentCards.get(agent.name);
    if (!card) {
      card = buildAgentCard(agent, index);
      agentCards.set(agent.name, card);
      $("agents").append(card.card);
    }
    card.card.dataset.status = agent.status;
    [card.pillIcon.textContent, card.pillText.textContent] = PILL[agent.status] ?? PILL.idle;

    const doing = state.activity[agent.title];
    card.activity.textContent = !agent.available ? agent.note || "Not built yet"
      : doing || (agent.status === "working" ? "Starting…" : "Standing by");
    card.stats.textContent = !agent.available ? ""
      : `${fmtCost(agent.cost_usd)} · ${fmtCount(agent.calls)} calls · ` +
        `${fmtCount(agent.input_tokens + agent.output_tokens)} tokens`;
  });
  const onDuty = agents.filter((a) => a.available).length;
  $("team-count").textContent = `${onDuty} of ${agents.length} on duty`;
}

function renderCommand() {
  const { busy, agents, demo } = state.snapshot;
  const available = agents.filter((a) => a.available);
  const select = $("target");
  if (select.options.length !== available.length) {
    select.replaceChildren(...available.map((a) => el("option", { value: a.name, text: a.title })));
  }
  if (!busy) state.stopRequested = false;
  $("launch").disabled = busy;
  $("launch").textContent = busy ? "Working…" : "Launch";
  $("stop").hidden = !busy;
  $("stop").disabled = state.stopRequested;

  const message = state.message ?? {
    text: busy ? "A project is in progress. You can stop it at any time."
      : demo ? "Demo mode: type any goal and press Launch to watch a scripted run."
      : "Describe a goal and press Enter. The assigned agent starts right away.",
  };
  const node = $("command-msg");
  node.textContent = message.text;
  node.dataset.tone = message.tone ?? "info";
}

function setMessage(text, tone) {
  state.message = text ? { text, tone } : null;
  if (state.snapshot) renderCommand();
}

// ------------------------------------------- deliverables & memory browser

function renderBrowser() {
  for (const tab of ["deliverables", "memory"]) {
    $(`tab-${tab}`).setAttribute("aria-selected", String(state.tab === tab));
  }
  $("memory-count").textContent = state.notes.length;

  const current = view[state.tab];
  const items = [];
  let context = "";

  if (state.tab === "deliverables") {
    const project = selectedProject();
    if (!project) {
      context = "Launch a project to see what your agents deliver.";
    } else {
      context = `${(STATUS[project.status] ?? STATUS.incomplete).label}: ${project.goal}`;
      if (project.summary || project.error) {
        items.push(browserItem("Agent's report", project.error ? "What went wrong" : "Final summary",
          current?.key === `${project.id}#report`, () => openReport(project)));
      }
      for (const path of project.files) {
        items.push(browserItem(path, "File", current?.key === `${project.id}/${path}`,
          () => openFile(project.id, path)));
      }
      if (!items.length) items.push(el("li", { class: "empty", text: "No files yet." }));
    }
  } else {
    context = "What the company remembers across projects. The same folder opens in Obsidian.";
    const fresh = new Set(selectedProject()?.new_notes ?? []);
    for (const note of state.notes) {
      const sub = [note.folder || "top level", ...note.tags.slice(0, 3).map((t) => `#${t}`)].join(" · ");
      items.push(browserItem(note.title, sub, current?.key === note.path,
        () => openNote(note), fresh.has(note.path)));
    }
    if (!items.length) items.push(el("li", { class: "empty", text: "The memory is empty." }));
  }

  $("browser-context").textContent = context;
  $("browser-items").replaceChildren(...items);
  $("viewer-title").textContent = current?.title ?? "Viewer";
  $("viewer").textContent = current?.content ??
    (state.tab === "deliverables" ? "Files your agents write open here." : "Select a note to read it.");
}

function browserItem(name, sub, isCurrent, onclick, isNew = false) {
  return el("li", {},
    el("button", { class: "item", type: "button", "aria-current": isCurrent ? "true" : null, onclick },
      el("span", { class: "name" }, document.createTextNode(name), isNew ? el("span", { class: "new", text: "new" }) : null),
      el("span", { class: "sub", text: sub })));
}

/** Show a document in the viewer, loading its content with `load()`. */
async function openDocument(tab, key, title, load) {
  view[tab] = { key, title, content: "Loading…" };
  renderBrowser();
  let content;
  try { content = await load(); } catch (error) { content = `Could not open this: ${error.message}`; }
  if (view[tab]?.key === key) {   // ignore the answer if the user moved on meanwhile
    view[tab].content = content;
    renderBrowser();
  }
}

const openFile = (projectId, path) => openDocument("deliverables", `${projectId}/${path}`, path, async () =>
  (await api(`api/projects/${encodeURIComponent(projectId)}/file?path=${encodeURIComponent(path)}`)).content);

// Notes start with a "frontmatter" block (author, date, tags). The list already
// shows that information, so the viewer hides the block and shows the text.
const FRONTMATTER = /^---\n[\s\S]*?\n---\n*/;

const openNote = (note) => openDocument(
  "memory", note.path,
  [note.path, note.author && `by ${note.author}`, note.updated].filter(Boolean).join(" · "),
  async () => (await api(`api/memory/note?path=${encodeURIComponent(note.path)}`)).content.replace(FRONTMATTER, ""));

const openReport = (project) => openDocument("deliverables", `${project.id}#report`, "Agent's report",
  async () => project.error ?? project.summary);

function selectProject(projectId, { open = true } = {}) {
  if (state.selectedProjectId !== projectId) view.deliverables = null;
  state.selectedProjectId = projectId;
  state.tab = "deliverables";
  const project = selectedProject();
  if (open && project && !view.deliverables) {
    if (project.files.length) openFile(project.id, project.files[0]);
    else if (project.summary || project.error) openReport(project);
  }
  render();
}

async function loadMemory() {
  try {
    state.notes = (await api("api/memory")).notes;
    if (state.snapshot) renderBrowser();
  } catch { /* the watchdog reports connection problems */ }
}

// -------------------------------------------------------------- live feed

const FEED_LIMIT = 400;

function agentColor(title) {
  const index = state.snapshot?.agents.findIndex((a) => a.title === title) ?? -1;
  return index >= 0 ? `var(--agent-${index + 1})` : "var(--accent)";
}

/** Describe an event as feed content, or null if it shouldn't be listed. */
function describe(event) {
  const d = event.data ?? {};
  switch (event.kind) {
    case "project_start":
      return { system: true, glyph: "▶", text: `New project: ${d.goal}` };
    case "project_done": {
      const status = STATUS[d.status] ?? STATUS.incomplete;
      const files = `${d.files} file${d.files === 1 ? "" : "s"}`;
      return {
        system: true, glyph: status.icon,
        ok: d.status === "done" ? "true" : d.status === "failed" ? "false" : "warn",
        text: `Project ${status.label.toLowerCase()} · ${fmtCost(d.cost_usd)} · ${files}` +
              (d.error ? ` · ${d.error}` : ""),
      };
    }
    case "stop_requested":
      return { system: true, glyph: "■", text: "Stop requested. The agent stops after its current step." };
    case "agent_start":
      return { glyph: "›", text: "Picked up the task" };
    case "text":
      return { text: d.text };
    case "tool_call": {
      const args = Object.entries(d.input ?? {})
        .map(([key, value]) => `${key}: ${clip(oneLine(typeof value === "string" ? value : JSON.stringify(value)), 80)}`)
        .join(" · ");
      return { glyph: "▸", tool: d.tool, text: args };
    }
    case "tool_result":
      return { glyph: d.ok ? "✓" : "✕", ok: String(Boolean(d.ok)), text: clip(oneLine(d.output), 220) };
    case "agent_done":
      return { glyph: "■", text: `Finished (${STOP_REASON[d.stop_reason] ?? d.stop_reason}) · ${fmtCost(d.cost_usd)}` };
    case "error":
      return { glyph: "✕", text: d.message };
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

  const message = el("div", { class: "feed-msg", title: "Click to expand" });
  if (info.glyph) message.append(el("span", { class: "glyph", "aria-hidden": "true", text: info.glyph }));
  if (info.tool) message.append(el("span", { class: "tool", text: `${info.tool} ` }));
  message.append(document.createTextNode(info.text ?? ""));
  message.addEventListener("click", () => message.classList.toggle("open"));

  const row = el("li", {
    class: info.system ? "feed-row system" : "feed-row",
    "data-kind": event.kind, "data-ok": info.ok ?? null,
  },
    el("div", { class: "feed-who" },
      el("span", { class: "mark", "aria-hidden": "true" }),
      el("b", { text: event.agent }),
      el("time", { text: fmtTime(event.ts) })),
    message);
  row.style.setProperty("--id", agentColor(event.agent));

  list.append(row);
  while (list.children.length > FEED_LIMIT) list.firstElementChild.remove();
  $("feed-empty").hidden = true;
  if (pinned) list.scrollTop = list.scrollHeight;
}

// ------------------------------------------------------------------ events

function handleEvent(event) {
  if (event.id <= state.lastEventId) return;   // already seen (replay or polling overlap)
  state.lastEventId = event.id;
  appendFeed(event);

  const d = event.data ?? {};
  switch (event.kind) {
    case "project_start":
      state.message = null;
      selectProject(event.project_id, { open: false });   // follow the new project
      break;
    case "agent_start":
      state.activity[event.agent] = "Reading the brief…";
      break;
    case "text":
      state.activity[event.agent] = clip(oneLine(d.text), 140);
      break;
    case "tool_call": {
      const input = d.input ?? {};
      const subject = input.path ?? input.query ?? input.note ?? input.title ?? "";
      state.activity[event.agent] = `▸ ${d.tool} ${oneLine(subject)}`;
      if (d.tool === "write_file" && input.path) state.lastWrite = { projectId: event.project_id, path: input.path };
      break;
    }
    case "tool_result":
      if (!d.ok) state.activity[event.agent] = `✕ ${clip(oneLine(d.output), 120)}`;
      else if (d.tool === "save_memory") loadMemory();
      else if (d.tool === "write_file") state.pendingOpen = state.lastWrite;   // show it as soon as it exists
      break;
    case "agent_done":
      state.activity[event.agent] = `Finished: ${STOP_REASON[d.stop_reason] ?? d.stop_reason}`;
      break;
  }
  scheduleRefresh();
}

let refreshTimer = null;

/** Many events can arrive at once; fetch the snapshot once for the whole burst. */
function scheduleRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(async () => {
    refreshTimer = null;
    try { await refreshState(); } catch { /* the watchdog reports connection problems */ }
  }, 120);
}

async function refreshState() {
  state.snapshot = await api("api/state");
  state.reachable = true;
  if (!state.selectedProjectId && state.snapshot.projects.length) {
    state.selectedProjectId = state.snapshot.projects[0].id;
  }
  render();

  // Open a file that was just written, now that the snapshot lists it.
  const project = selectedProject();
  const pending = state.pendingOpen;
  state.pendingOpen = null;
  if (pending && project && pending.projectId === project.id && project.files.includes(pending.path)) {
    openFile(project.id, pending.path);
  } else if (project && !view.deliverables && project.status !== "running") {
    if (project.files.length) openFile(project.id, project.files[0]);
    else if (project.summary || project.error) openReport(project);
  }
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
      if (!state.snapshot) await refreshState();
      state.reachable = true;
    } catch {
      state.reachable = false;
    }
  }
  renderLink();
}

// ----------------------------------------------------------------- actions

async function launch(event) {
  event.preventDefault();
  const goal = $("goal").value.trim();
  if (goal.length < 3) {
    setMessage("Describe the goal in a few words first.", "error");
    $("goal").focus();
    return;
  }
  $("launch").disabled = true;
  try {
    const project = await api("api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal, role: $("target").value }),
    });
    $("goal").value = "";
    state.message = null;
    state.activity = {};
    view.deliverables = null;
    state.selectedProjectId = project.id;
  } catch (error) {
    setMessage(error.message, "error");
  }
  try { await refreshState(); } catch { /* the watchdog reports connection problems */ }
}

async function stop() {
  const running = state.snapshot?.projects.find((p) => p.status === "running");
  if (!running) return;
  state.stopRequested = true;
  $("stop").disabled = true;
  try {
    await api(`api/projects/${encodeURIComponent(running.id)}/stop`, { method: "POST" });
    setMessage("Stopping after the current step…");
  } catch (error) {
    state.stopRequested = false;
    setMessage(error.message, "error");
  }
}

// -------------------------------------------------------------------- init

async function init() {
  $("command-form").addEventListener("submit", launch);
  $("stop").addEventListener("click", stop);
  $("goal").addEventListener("input", () => { if (state.message?.tone === "error") setMessage(""); });
  for (const tab of ["deliverables", "memory"]) {
    $(`tab-${tab}`).addEventListener("click", () => {
      state.tab = tab;
      if (tab === "memory" && !view.memory && state.notes.length) openNote(state.notes[0]);
      else renderBrowser();
    });
  }

  $("expand").addEventListener("click", () => {
    const reading = document.querySelector(".col-center").classList.toggle("reading");
    $("expand").textContent = reading ? "Collapse" : "Expand";
    $("expand").setAttribute("aria-pressed", String(reading));
  });

  const tick = () => { $("sys-clock").textContent = fmtTime(Date.now() / 1000); };
  tick();
  setInterval(tick, 1000);

  try { await refreshState(); } catch { state.reachable = false; }
  // Replay the most recent events so the feed isn't empty after a page reload.
  state.lastEventId = Math.max(0, (state.snapshot?.last_event_id ?? 0) - 200);
  connectEvents();
  loadMemory();
  setInterval(watchdog, 2000);
  renderLink();
}

init();
