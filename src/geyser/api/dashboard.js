"use strict";
/* Geyser dashboard (DESIGN.md sections 15.3 and 15.4).

   Two views over one project:
   - Agents: each agent's turns (what it received, what it said, the tools it called),
     its open requests, and the operator's messages to it.
   - Tasks: two layers over the same tasks. "Publication" shows where each task was
     published from; "Dependencies and waits" shows what each task needs from others.

   Apart from the operator's own buttons the page only reads: viewing never starts a
   model and never acknowledges a message. Everything is built as DOM from data (no
   HTML strings), and a part is redrawn only when its data changed, so open folds,
   scroll positions and half-typed answers survive the periodic refresh. */

const REFRESH_MS = 4000;
const TURNS_STEP = 10;

const ui = {view: "agents", agent: null, turns: TURNS_STEP, task: null,
            layer: "publication", hideMet: false};
let data = {status: null, agents: [], requests: []};

// ============================================================= building blocks
const $ = (id) => document.getElementById(id);
const present = (value) => value !== null && value !== undefined && value !== "" && value !== false;

/** An HTML element. Props: class, text, dataset, on<event>, or plain attributes. */
function h(tag, props = {}, ...children) {
  return build(document.createElement(tag), props, children);
}

/** An SVG element, with the same props as h(). */
function svg(tag, props = {}, ...children) {
  return build(document.createElementNS("http://www.w3.org/2000/svg", tag), props, children);
}

function build(node, props, children) {
  for (const [key, value] of Object.entries(props)) {
    if (!present(value)) continue;
    if (key === "class") node.setAttribute("class", value);
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  node.append(...children.flat(Infinity).filter(present));
  return node;
}

const badge = (text, tone = "") => h("span", {class: tone ? `badge ${tone}` : "badge"}, text);
const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;
const wait = (count) => (count === 1 ? "waits" : "wait");
const unique = (items) => [...new Set(items)];

function groupBy(items, key) {
  const groups = new Map();
  for (const item of items) {
    const name = key(item);
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(item);
  }
  return groups;
}

function clock(iso) {
  const date = new Date(iso);
  return iso && !Number.isNaN(date.getTime())
    ? date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"}) : "";
}

function pretty(value) {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return text && text.length > 20000 ? `${text.slice(0, 20000)}\n…` : text;
}

// ================================================================ the server
async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.message || payload.detail || `${response.status} ${response.statusText}`);
  }
  return payload;
}

/** Problems shown in the notice line (and in an open dialog), one per source. */
const problems = new Map();

function report(source, message) {
  if (message) problems.set(source, message); else problems.delete(source);
  const text = [...problems.values()].join("\n");
  $("notice").textContent = text;
  for (const line of document.querySelectorAll(".dialog-error")) line.textContent = text;
}

/** An operator action: post it, report a failure, refresh. Returns whether it worked. */
async function act(what, path, body = {}) {
  try {
    await api(path, body);
    report("action", null);
  } catch (error) {
    report("action", `${what} failed: ${error.message}`);
    return false;
  }
  await refresh();
  return true;
}

/** Run one part of a refresh; a failure is reported and never stops the other parts. */
async function guard(part, work) {
  try {
    await work();
    report(part, null);
  } catch (error) {
    console.error(error);
    report(part, `Could not show ${part}: ${error.message}`);
  }
}

// ======================================================== redrawing on change
const drawn = new Map();

/** Redraw a container only when its data changed, keeping folds and scroll position. */
function draw(id, value, render, {stickToBottom = false} = {}) {
  const signature = JSON.stringify(value);
  if (drawn.get(id) === signature) return;
  drawn.set(id, signature);
  const container = $(id);
  const folds = new Map([...container.querySelectorAll("details[data-key]")]
    .map((fold) => [fold.dataset.key, fold.open]));
  const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 40;
  const top = container.scrollTop;
  container.replaceChildren(...[render()].flat(Infinity).filter(present));
  for (const fold of container.querySelectorAll("details[data-key]")) {
    if (folds.has(fold.dataset.key)) fold.open = folds.get(fold.dataset.key);
  }
  container.scrollTop = stickToBottom && atBottom ? container.scrollHeight : top;
}

function clear(id) {
  drawn.delete(id);
  $(id).replaceChildren();
}

// =================================================================== refresh
let refreshing = null;
let refreshAgain = false;

/** Refresh everything shown; overlapping calls fold into one more pass. */
async function refresh() {
  if (refreshing) {
    refreshAgain = true;
    return refreshing;
  }
  refreshing = (async () => {
    do {
      refreshAgain = false;
      await refreshOnce();
    } while (refreshAgain);
  })();
  try {
    await refreshing;
  } finally {
    refreshing = null;
  }
}

async function refreshOnce() {
  try {
    const [status, agents, requests] = await Promise.all([
      api("/v1/status"), api("/v1/agents"), api("/v1/requests")]);
    data = {status, agents, requests};
    report("connection", null);
  } catch (error) {
    report("connection", `Cannot reach the Geyser service: ${error.message}`);
    return;
  }
  const parts = [guard("the status", async () => drawFrame())];
  parts.push(ui.view === "agents" ? guard("the agents", refreshAgentView)
                                  : guard("the tasks", refreshTaskView));
  if ($("requests-dialog").open) parts.push(guard("the requests", refreshRequests));
  if ($("activity-dialog").open) parts.push(guard("the activity", refreshActivity));
  await Promise.all(parts);
}

// ====================================================== header and status bar
const RUN_LABELS = {running: "Running", paused: "Paused", stopped: "Stopped",
                    pause_after_turn: "Pausing after the turns in progress",
                    completed: "Task 0 is completed"};
const RUN_BUTTONS = {
  stopped: [["start", "Start"]],
  running: [["pause-after-turn", "Pause after turn"], ["pause", "Pause"], ["stop", "Stop"]],
  paused: [["resume", "Resume"], ["stop", "Stop"]],
  pause_after_turn: [["resume", "Resume"], ["pause", "Pause"], ["stop", "Stop"]],
  completed: [["resume", "Resume"]],
};
const CONFIRM = {
  start: "Start the run? Agents begin model turns on your accounts.",
  resume: "Resume the run? Agents continue model turns on your accounts.",
  stop: "Stop the run? Turns in progress are interrupted.",
};
let setupOffered = false;

function drawFrame() {
  const status = data.status;
  $("project-name").textContent = status.project || "";
  document.title = status.project ? `Geyser · ${status.project}` : "Geyser";
  const asks = data.requests.length;
  const requests = $("open-requests");
  requests.textContent = asks ? `Requests · ${asks}` : "Requests";
  requests.classList.toggle("attention", asks > 0);
  $("open-activity").textContent = status.notices.length
    ? `Activity · ${plural(status.notices.length, "notice")}` : "Activity";
  $("open-setup").classList.toggle("hidden", Boolean(status.task0));
  if (!status.task0 && !setupOffered) {
    setupOffered = true;
    $("setup-dialog").showModal();
  }
  const bar = $("status-bar");
  bar.classList.toggle("active", status.run === "running" && status.live_activations > 0);
  const missing = status.missing_choices || [];
  bar.classList.toggle("attention", status.holds.length > 0 || missing.length > 0);
  $("status-text").textContent = statusLine(status);
  $("status-text").title = missing.length
    ? `Before a run, choose with \`geyser choose\`:\n${missing.join("\n")}` : "";
  draw("run-actions", status.run, () => (RUN_BUTTONS[status.run] || []).map(([action, label]) =>
    h("button", {type: "button", class: ["start", "resume"].includes(action) ? "primary" : "",
                 onclick: () => runAction(action)}, label)));
}

function statusLine(status) {
  const total = Object.values(status.tasks).reduce((sum, count) => sum + count, 0);
  const parts = [RUN_LABELS[status.run] || status.run];
  if (status.live_activations) parts.push(`${plural(status.live_activations, "turn")} in progress`);
  parts.push(`${status.tasks.completed || 0} of ${plural(total, "task")} completed`);
  if (status.open_requests) {
    parts.push(`${plural(status.open_requests, "request")} ${wait(status.open_requests)} for you`);
  }
  for (const hold of status.holds) parts.push(`account ${hold.account} on hold: ${hold.reason}`);
  const missing = status.missing_choices || [];
  if (missing.length) {
    const roles = missing.map((entry) => entry.split(":")[0]).join(", ");
    parts.push(`to choose before a run (geyser choose): ${roles}`);
  }
  return parts.join(" · ");
}

async function runAction(action) {
  if (CONFIRM[action] && !window.confirm(CONFIRM[action])) return;
  await act(`Run ${action.replaceAll("-", " ")}`, `/v1/run/${action}`);
}

// ================================================================ navigation
function setView(view) {
  ui.view = view;
  for (const tab of document.querySelectorAll(".view-tab")) {
    tab.classList.toggle("active", tab.dataset.view === view);
    tab.setAttribute("aria-selected", String(tab.dataset.view === view));
  }
  $("agents-view").classList.toggle("hidden", view !== "agents");
  $("tasks-view").classList.toggle("hidden", view !== "tasks");
  saveLocation();
  refresh();
}

function selectAgent(id) {
  ui.agent = id;
  ui.turns = TURNS_STEP;
  clear("history");
  clear("queued");
  saveLocation();
  refresh();
}

function selectTask(id) {
  ui.task = id;
  saveLocation();
  refresh();
}

function showTask(id) {
  for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
  ui.task = id;
  setView("tasks");
}

function setLayer(layer) {
  ui.layer = layer;
  saveLocation();
  refresh();
}

/** The view and selection live in the address (#agents/worker-0, #tasks/dependencies/task-3). */
function saveLocation() {
  const parts = ui.view === "agents" ? ["agents", ui.agent] : ["tasks", ui.layer, ui.task];
  history.replaceState(null, "", `#${parts.filter(present).join("/")}`);
}

function loadLocation() {
  const [view, first, second] = decodeURIComponent(location.hash.slice(1)).split("/");
  if (view === "tasks") {
    ui.view = "tasks";
    ui.layer = first === "dependencies" ? "dependencies" : "publication";
    ui.task = second || null;
  } else if (view === "agents") {
    ui.agent = first || null;
  }
}

/** A task ID as a link into the task view; any other ID as plain text. */
function taskLink(id) {
  if (id === "operator") return "you";
  if (!/^task-\d+$/.test(id || "")) return id || "";
  return h("button", {type: "button", class: "link", onclick: () => showTask(id)}, id);
}

const taskLinks = (ids) => ids.flatMap((id, index) => [index ? ", " : "", taskLink(id)]);

// ==================================================================== agents
const KIND_GROUPS = [["worker", "Workers"], ["reviewer", "Reviewers"], ["integrator", "Integrator"]];
const CONTROL_LABELS = {running: "Running", paused: "Paused", pause_after_turn: "Pausing after this turn",
                        stopped: "Stopped", completed: "Completed"};
const PURPOSES = {task: "Task turn", review: "Review turn", message: "Message turn",
                  answer: "Answer turn", integration: "Integration turn"};
const OUTCOME_TONES = {completed: "ok", interrupted: "attention", failed: "bad",
                       closed_unknown: "bad", abandoned: "bad"};
const DELIVERY = {pending: ["queued", "quiet"], prepared: ["being delivered", ""],
                  delivered: ["delivered", "ok"], uncertain: ["delivery unknown", "attention"],
                  dropped: ["withdrawn", "quiet"]};

const currentWork = (agent) => agent.current_task || agent.current_review || agent.current_batch || "";
const ownControl = (agent) => (agent.control !== data.status.run ? agent.control : null);

async function refreshAgentView() {
  drawAgentList();
  const agent = data.agents.find((candidate) => candidate.id === ui.agent);
  $("compose").classList.toggle("hidden", !agent || agent.state === "retired");
  if (!agent) return;
  drawAgentHead(agent);
  const id = encodeURIComponent(agent.id);
  const [turns, queued] = await Promise.all([
    api(`/v1/agents/${id}/turns?limit=${ui.turns}`), api(`/v1/agents/${id}/queued`)]);
  if (agent.id !== ui.agent) return; // The selection changed meanwhile.
  draw("history", {turns, limit: ui.turns}, () => historyView(agent, turns), {stickToBottom: true});
  draw("queued", queued, () => queued.map(queuedBubble));
}

function drawAgentList() {
  const filter = $("agent-filter").value.trim().toLowerCase();
  const asking = new Set(data.requests.map((request) => request.asker));
  const shown = data.agents
    .filter((agent) => agent.state !== "retired" || agent.id === ui.agent)
    .filter((agent) => !filter || [agent.id, agent.backend, currentWork(agent)].join(" ")
      .toLowerCase().includes(filter));
  draw("agent-list", {shown, asking: [...asking], selected: ui.agent, run: data.status.run}, () => {
    if (!shown.length) {
      return h("p", {class: "muted group-label"}, data.agents.length ? "No agent matches."
        : "No agents yet. The run creates them as it assigns tasks.");
    }
    return KIND_GROUPS.map(([kind, label]) => {
      const members = shown.filter((agent) => agent.kind === kind);
      return members.length
        ? [h("div", {class: "group-label"}, label), members.map((a) => agentButton(a, asking.has(a.id)))]
        : null;
    });
  });
}

function agentButton(agent, asks) {
  const dot = agent.live ? "active" : asks || agent.failures ? "alert" : "";
  const control = ownControl(agent);
  return h("button", {type: "button", class: agent.id === ui.agent ? "worker selected" : "worker",
                      onclick: () => selectAgent(agent.id)},
    h("span", {class: `worker-state ${dot}`, "aria-hidden": "true"}),
    h("strong", {}, agent.id),
    h("span", {class: "muted"}, [agent.backend, currentWork(agent), agent.state].filter(present).join(" · ")),
    asks ? h("span", {class: "attention-line"}, "Waiting for your answer") : null,
    control ? h("span", {class: "attention-line"}, CONTROL_LABELS[control] || control) : null);
}

function drawAgentHead(agent) {
  $("agent-title").textContent = agent.id;
  const control = ownControl(agent);
  draw("agent-badges", {agent, control}, () => [
    badge(agent.kind, "quiet"), badge(agent.backend, "quiet"),
    agent.move_to ? badge(`moving to ${agent.move_to}`, "attention") : null,
    badge(agent.state, agent.state === "retired" ? "quiet" : ""),
    agent.live ? badge("in a turn", "ok") : null,
    agent.failures ? badge(plural(agent.failures, "failure"), "bad") : null,
    control ? badge(CONTROL_LABELS[control] || control, "attention") : null,
  ]);
  draw("agent-actions", {id: agent.id, control: agent.control, state: agent.state}, () => {
    if (agent.state === "retired") return null;
    const action = (name, label) => h("button", {type: "button", class: "small",
      onclick: () => act(`${label} ${agent.id}`, `/v1/agents/${encodeURIComponent(agent.id)}/${name}`)}, label);
    return agent.control === "running"
      ? [action("pause-after-turn", "Pause after turn"), action("pause", "Pause")]
      : action("resume", "Resume");
  });
  const work = currentWork(agent);
  draw("agent-summary", {work}, () => (work ? ["Current work: ", taskLink(work)] : "No current work."));
  const asked = data.requests.filter((request) => request.asker === agent.id);
  $("agent-requests").classList.toggle("hidden", !asked.length);
  draw("agent-requests", asked.map((request) => request.id), () => [
    h("strong", {}, `Waiting for your answer · ${asked.length}`), asked.map(requestCard)]);
}

function historyView(agent, turns) {
  if (!turns.length) {
    return h("div", {class: "empty"}, h("h2", {}, "No turns yet"),
      h("p", {}, `${agent.id} has not run a turn.`));
  }
  const earlier = turns.length >= ui.turns ? h("button", {type: "button", class: "small",
    onclick: () => { ui.turns += TURNS_STEP; refresh(); }}, "Show earlier turns") : null;
  return [earlier, turns.map((turn) => turnView(agent, turn))];
}

function turnView(agent, turn) {
  const events = turn.events || [];
  const outcome = turn.outcome
    ? badge(turn.outcome.replaceAll("_", " "), OUTCOME_TONES[turn.outcome] || "quiet")
    : badge(turn.state, "ok");
  return h("article", {class: "turn"},
    h("div", {class: "turn-heading"},
      h("h3", {}, `${PURPOSES[turn.purpose] || turn.purpose} · ${turn.activation}`), outcome,
      turn.task_id ? taskLink(turn.task_id) : turn.review_id,
      h("span", {class: "muted", title: turn.planned_at}, clock(turn.planned_at))),
    receivedPanel(turn, events.find((event) => event.type === "input")),
    h("div", {class: "stream"}, streamItems(agent, events)));
}

/** What the turn received, and the delivery state of each inbox item it carried. */
function receivedPanel(turn, input) {
  const received = turn.received || [];
  const states = [...groupBy(received, (item) => item.state)]
    .map(([state, items]) => `${items.length} ${(DELIVERY[state] || [state])[0]}`);
  const summary = received.length
    ? `What it received · ${plural(received.length, "item")}: ${states.join(", ")}`
    : "What it received";
  return h("details", {class: "input-panel", dataset: {key: `input:${turn.activation}`}},
    h("summary", {}, summary),
    received.length ? h("ul", {class: "received"}, received.map((item) => {
      const [label, tone] = DELIVERY[item.state] || [item.state, "quiet"];
      return h("li", {}, badge(label, tone), " ", item.summary);
    })) : null,
    input ? h("pre", {class: "exact-input"}, input.text)
          : h("p", {class: "muted"}, "The exact input was not recorded for this turn."));
}

/** Messages as bubbles; consecutive tool calls folded together with their results. */
function streamItems(agent, events) {
  const results = new Map(events.filter((event) => event.type === "tool_result")
    .map((event) => [event.call_id, event.result]));
  const items = [];
  let calls = [];
  const flush = () => {
    if (calls.length) items.push(toolFold(calls));
    calls = [];
  };
  for (const event of events) {
    if (event.type === "tool_call") {
      calls.push({call: event, result: results.get(event.call_id)});
      continue;
    }
    if (["tool_result", "start", "input", "turn_started"].includes(event.type)) continue;
    flush();
    if (event.type === "agent_message") items.push(bubble("left", agent.id, event.at, event.text));
    else if (event.type === "steer") {
      const [label, tone] = DELIVERY[event.delivery] || [event.delivery, "attention"];
      items.push(bubble("right", "Steered into the turn", event.at, event.text, badge(label, tone)));
    } else if (event.type === "outcome") items.push(outcomeLine(event));
    else items.push(h("div", {class: "muted"}, `${event.type}: ${pretty(event)}`));
  }
  flush();
  return items;
}

function bubble(side, who, at, text, extra = null) {
  return h("div", {class: `bubble ${side}`},
    h("div", {class: "bubble-head"}, h("strong", {}, who), extra,
      h("span", {class: "time", title: at}, clock(at))),
    h("pre", {}, text));
}

function toolFold(calls) {
  const names = unique(calls.map(({call}) => operationName(call)));
  const failed = calls.filter(({result}) => result && result.error).length;
  const summary = `${plural(calls.length, "tool call")}: ${names.join(", ")}`
    + (failed ? ` · ${failed} failed` : "");
  return h("details", {class: "tools", dataset: {key: `tools:${calls[0].call.call_id}`}},
    h("summary", {}, summary),
    calls.map(({call, result}) => h("div", {class: "action"},
      h("strong", {}, operationName(call)), h("span", {class: "muted"}, ` ${clock(call.at)}`),
      h("pre", {}, pretty(call.arguments?.arguments ?? call.arguments)),
      result === undefined ? h("p", {class: "muted"}, "No result recorded.")
        : h("pre", {class: result.error ? "error" : ""}, pretty(result)))));
}

function operationName(call) {
  const args = call.arguments || {};
  if ((call.tool || "").endsWith("_actions")) return args.category ? `actions · ${args.category}` : "actions";
  return args.operation || call.tool;
}

function outcomeLine(event) {
  const text = [`Ended: ${event.status}`, event.failure, event.detail].filter(present).join(" · ");
  return h("p", {class: "turn-summary"}, text);
}

function queuedBubble(message) {
  const [label, tone] = DELIVERY[message.state] || [message.state, "quiet"];
  return h("div", {class: "bubble right"},
    h("div", {class: "bubble-head"}, h("strong", {}, `Your message ${message.id}`), badge(label, tone),
      h("button", {type: "button", class: "small right", disabled: message.state !== "pending",
                   onclick: () => act("Withdrawing the message", `/v1/messages/${message.id}/withdraw`)},
        "Withdraw")),
    h("pre", {}, message.summary));
}

async function sendMessage(event) {
  event.preventDefault();
  const text = $("message").value.trim();
  const button = event.submitter || $("compose").querySelector("button[type=submit]");
  if (!text || !ui.agent) return;
  button.disabled = true;
  try {
    if (await act("Sending the message", "/v1/messages", {to: ui.agent, text})) $("message").value = "";
  } finally {
    button.disabled = false;
  }
}

// ================================================================== requests
function requestCard(request) {
  const answer = h("textarea", {placeholder: "Your answer", "aria-label": `Answer to ${request.id}`});
  const send = async () => {
    if (answer.value.trim()) {
      await act(`Answering ${request.id}`, `/v1/requests/${request.id}/answer`, {text: answer.value});
    }
  };
  return h("div", {class: "request"},
    h("div", {class: "row muted"}, badge(request.kind, "attention"), h("strong", {}, request.id),
      `from ${request.asker}`, request.task_id ? taskLink(request.task_id) : null,
      h("span", {title: request.created_at}, clock(request.created_at))),
    h("h4", {}, request.title),
    h("pre", {}, request.body.replace(`# ${request.title}
`, "").trim()),
    answer,
    h("div", {class: "row"}, h("button", {type: "button", class: "primary right", onclick: send}, "Answer")));
}

async function openRequests() {
  $("requests-dialog").showModal();
  await guard("the requests", refreshRequests);
}

async function refreshRequests() {
  const inbound = await api("/v1/messages/inbound");
  draw("requests-list", data.requests.map((request) => request.id), () => (data.requests.length
    ? data.requests.map(requestCard) : h("p", {class: "muted"}, "No request waits for you.")));
  draw("inbound-list", inbound, () => (inbound.length ? inbound.map((message) =>
    h("details", {class: "line-item", dataset: {key: `inbound:${message.id}`}},
      h("summary", {}, `${message.sender} · ${clock(message.created_at)} · ${message.summary}`),
      h("pre", {}, message.body)))
    : h("p", {class: "muted"}, "No agent has written to you.")));
}

async function openActivity() {
  $("activity-dialog").showModal();
  await guard("the activity", refreshActivity);
}

async function refreshActivity() {
  const events = await api("/v1/activity?limit=100");
  const notices = data.status.notices;
  draw("notices-list", notices, () => (notices.length ? notices.map((item) =>
    h("div", {class: "line-item"}, h("span", {class: "muted", title: item.last_at}, clock(item.last_at)),
      item.message, h("span", {class: "muted"}, ` ×${item.count}`)))
    : h("p", {class: "muted"}, "None.")));
  draw("activity-list", events, () => events.map((event) =>
    h("div", {class: "line-item"}, h("span", {class: "muted", title: event.at}, clock(event.at)),
      h("strong", {}, event.kind), " ", taskLink(event.subject),
      event.actor ? h("span", {class: "muted"}, ` by ${event.actor}`) : null)));
}

async function createTaskZero(event) {
  event.preventDefault();
  const acceptance = $("setup-acceptance").value.split("\n").map((line) => line.trim()).filter(present);
  const created = await act("Creating Task 0", "/v1/setup", {
    title: $("setup-title").value.trim(), goal: $("setup-goal").value.trim(), acceptance});
  if (created) $("setup-dialog").close();
}

// ===================================================================== tasks
const TASK_STATES = {open: ["open", "quiet"], set_aside: ["set aside", "attention"], active: ["active", ""],
                     waiting: ["waiting", "attention"], completing: ["completing", ""],
                     completed: ["completed", "ok"], retired: ["retired", "quiet"]};
const CANDIDATE_TONES = {published: "ok", accepted: "ok", rejected: "bad", invalidated: "bad",
                         returned: "attention", withdrawn: "quiet"};

async function refreshTaskView() {
  const graph = await api("/v1/task-graph");
  const model = taskModel(graph);
  if (ui.task && !model.byId.has(ui.task)) ui.task = null;
  drawLayerTabs(model);
  draw("task-banners", [graph.deadlocks, graph.waiting_on_operator, graph.unreadable],
       () => banners(graph));
  draw("layer", {graph, layer: ui.layer, hideMet: ui.hideMet, task: ui.task},
       () => (ui.layer === "publication" ? publicationLayer(model) : dependencyLayer(model)));
  await refreshTaskDetail(model);
}

/** The graph from the server, indexed for both layers. */
function taskModel(graph) {
  const byId = new Map(graph.tasks.map((task) => [task.id, task]));
  const hasParent = (task) => task.parent && byId.has(task.parent);
  return {
    graph, byId,
    roots: graph.tasks.filter((task) => !hasParent(task)),
    children: groupBy(graph.tasks.filter(hasParent), (task) => task.parent),
    needs: groupBy(graph.links, (link) => link.target),
    neededBy: groupBy(graph.links, (link) => link.source),
    notes: groupBy(graph.notes, (note) => note.task),
    deadlockOf: new Map(graph.deadlocks.flatMap((group, index) => group.map((id) => [id, index]))),
    onOperator: new Set(graph.waiting_on_operator),
  };
}

function drawLayerTabs(model) {
  for (const tab of document.querySelectorAll(".tab[data-layer]")) {
    tab.classList.toggle("active", tab.dataset.layer === ui.layer);
    tab.setAttribute("aria-selected", String(tab.dataset.layer === ui.layer));
  }
  $("hide-met-label").classList.toggle("hidden", ui.layer !== "dependencies");
  const {tasks, links} = model.graph;
  const unmet = links.filter((link) => !link.met).length;
  $("layer-summary").textContent = ui.layer === "publication"
    ? `${plural(tasks.length, "task")} · ${model.roots.length} at the top level`
    : `${plural(links.length, "link")} · ${unmet} not met`;
}

function banners(graph) {
  return [
    graph.deadlocks.map((group) => h("div", {class: "banner bad"}, h("strong", {}, "Deadlock: "),
      taskLinks(group), " need each other through unmet links, so none of them can go on. ",
      "Revise one of the contracts, or message an executor.")),
    graph.waiting_on_operator.length ? h("div", {class: "banner attention"},
      `${plural(graph.waiting_on_operator.length, "task")} ${wait(graph.waiting_on_operator.length)} for your answer, directly or `,
      "through other tasks: ", taskLinks(graph.waiting_on_operator), ". ",
      h("button", {type: "button", class: "link", onclick: openRequests}, "Open the requests")) : null,
    graph.unreadable.length ? h("div", {class: "banner bad"}, "The contract files of ",
      taskLinks(graph.unreadable), " cannot be read, so their requirements are not shown.") : null,
  ];
}

function taskMarks(task, model) {
  const [label, tone] = TASK_STATES[task.state] || [task.state, "quiet"];
  const unmet = (model.needs.get(task.id) || []).filter((link) => !link.met).length;
  return [
    badge(label, tone),
    model.deadlockOf.has(task.id) ? badge("deadlock", "bad") : null,
    model.onOperator.has(task.id) ? badge("waits on you", "attention") : null,
    unmet ? badge(`blocked by ${unmet}`, "attention") : null,
    task.relation === "repair" ? badge("repair", "bad") : null,
    task.evidence_valid === 0 ? badge("evidence invalid", "bad") : null,
  ];
}

function publisherText(task) {
  if (task.publisher === "operator") return "published by you";
  return task.relation ? `published by ${task.publisher}` : `published by ${task.publisher} outside a task turn`;
}

function taskRow(task, model) {
  const executors = task.executors.map((member) => member.agent).join(", ");
  return h("button", {type: "button", class: task.id === ui.task ? "task-row selected" : "task-row",
                      onclick: (event) => { event.preventDefault(); selectTask(task.id); }},
    h("span", {class: `state-dot ${task.state}`, "aria-hidden": "true"}),
    h("strong", {}, task.id), h("span", {}, task.title), taskMarks(task, model),
    h("span", {class: "muted"}, [publisherText(task), executors && `executors ${executors}`]
      .filter(present).join(" · ")));
}

// -------------------------------------------------------- publication layer
function publicationLayer(model) {
  if (!model.roots.length) return emptyTasks();
  return [
    h("p", {class: "muted"}, "Each task sits under the task in whose turn it was published, and a ",
      "repair task under the task it repairs. Top-level tasks were published by you, or by an ",
      "agent outside a task turn."),
    h("ul", {class: "outline-list roots"}, model.roots.map((task) => outlineItem(task, model, new Set()))),
  ];
}

function outlineItem(task, model, seen) {
  seen.add(task.id);
  const children = (model.children.get(task.id) || []).filter((child) => !seen.has(child.id));
  const row = taskRow(task, model);
  if (!children.length) return h("li", {class: "outline-item"}, row);
  const below = descendants(task.id, model);
  const done = below.filter((other) => other.state === "completed").length;
  return h("li", {class: "outline-item"},
    h("details", {open: true, dataset: {key: `publication:${task.id}`}},
      h("summary", {}, row, h("span", {class: "subtree"}, `${done}/${below.length} below completed`)),
      h("ul", {class: "outline-list"}, children.map((child) => outlineItem(child, model, seen)))));
}

function descendants(id, model) {
  const found = [];
  const stack = [...(model.children.get(id) || [])];
  const seen = new Set([id]);
  while (stack.length) {
    const task = stack.pop();
    if (seen.has(task.id)) continue;
    seen.add(task.id);
    found.push(task);
    stack.push(...(model.children.get(task.id) || []));
  }
  return found;
}

function emptyTasks() {
  return h("div", {class: "empty"}, h("h2", {}, "No tasks yet"),
    h("p", {}, "Set up Task 0 to give the project its goal."));
}

// -------------------------------------------------------- dependency layer
const NODE_W = 230;
const NODE_H = 66;
const GAP_X = 90;
const GAP_Y = 22;
const PAD = 12;

function dependencyLayer(model) {
  const links = model.graph.links.filter((link) => !(ui.hideMet && link.met));
  const edges = mergeLinks(links, model);
  const ids = unique(edges.flatMap((edge) => [edge.source, edge.target]));
  const inner = innerWaits(model, new Set(ids));
  if (!model.graph.tasks.length) return emptyTasks();
  if (!ids.length) {
    const text = model.graph.links.length ? "Every link between tasks is met."
      : "No task needs another task, and no task waits on you.";
    return [h("div", {class: "empty small"}, h("p", {}, text)), inner];
  }
  const layout = layered(ids, edges);
  const canvas = h("div", {class: "graph"});
  canvas.style.width = `${layout.width}px`;
  canvas.style.height = `${layout.height}px`;
  canvas.append(edgeLayer(edges, layout), // Waypoints in layout.position are left empty.
    ...ids.map((id) => graphNode(id, model, layout.position.get(id), edges)));
  return [legend(), canvas, inner];
}

function legend() {
  const item = (kind, text) => h("span", {}, h("span", {class: `swatch ${kind}`}), text);
  return h("div", {class: "legend"},
    h("span", {}, "Arrows run from what is needed to what needs it."),
    item("", "contract requirement"), item("input", "candidate input"),
    item("wait", "an agent's wait"), item("met", "met"), item("deadlock", "deadlock"));
}

/** One edge per pair of nodes, styled by its most pressing unmet link. */
function mergeLinks(links, model) {
  const pairs = groupBy(links.filter((link) => link.source !== link.target),
                        (link) => `${link.source}>${link.target}`);
  return [...pairs.values()].map((group) => {
    const {source, target} = group[0];
    const unmet = group.filter((link) => !link.met);
    const cycle = model.deadlockOf.get(source);
    const deadlock = unmet.length > 0 && cycle !== undefined && cycle === model.deadlockOf.get(target);
    const kind = deadlock ? "deadlock" : !unmet.length ? "met"
      : unmet.some((link) => link.kind === "wait") ? "wait"
      : unmet.some((link) => link.kind === "requirement") ? "requirement" : "input";
    const labels = group.map((link) => (link.source === "operator" ? link.term
      : link.kind === "wait" ? link.agent : link.kind === "input" ? `input ${link.term}` : ""));
    return {source, target, links: group, kind, label: unique(labels.filter(present)).join(", ")};
  });
}

/** A left-to-right layout: what is needed stands left of what needs it.
    An edge across several columns passes through an empty slot in each column between,
    so no edge ever runs behind a node. */
function layered(ids, edges) {
  const order = (id) => (id === "operator" ? -1 : parseInt(id.split("-")[1], 10) || 0);
  const nodes = [...ids].sort((a, b) => order(a) - order(b));
  // 1. Edges that close a cycle are left out of the columns; they are drawn as loops.
  const outgoing = groupBy(edges, (edge) => edge.source);
  const back = new Set();
  const visiting = new Map();
  const visit = (id) => {
    visiting.set(id, true);
    for (const edge of outgoing.get(id) || []) {
      if (visiting.get(edge.target) === true) back.add(edge);
      else if (!visiting.has(edge.target)) visit(edge.target);
    }
    visiting.set(id, false);
  };
  nodes.forEach((id) => visiting.has(id) || visit(id));
  const forward = edges.filter((edge) => !back.has(edge));
  // 2. A node's column is the length of the longest chain of needs leading to it.
  const column = new Map(nodes.map((id) => [id, 0]));
  const pending = new Map(nodes.map((id) => [id, 0]));
  for (const edge of forward) pending.set(edge.target, pending.get(edge.target) + 1);
  const ready = nodes.filter((id) => pending.get(id) === 0);
  const forwardOut = groupBy(forward, (edge) => edge.source);
  while (ready.length) {
    const id = ready.shift();
    for (const edge of forwardOut.get(id) || []) {
      column.set(edge.target, Math.max(column.get(edge.target), column.get(id) + 1));
      pending.set(edge.target, pending.get(edge.target) - 1);
      if (pending.get(edge.target) === 0) ready.push(edge.target);
    }
  }
  // 3. Each forward edge becomes a chain of points, one per column it crosses.
  const columns = [];
  const place = (id, index) => {
    column.set(id, index);
    (columns[index] ||= []).push(id);
  };
  for (const id of nodes) place(id, column.get(id));
  const chains = new Map();
  const before = new Map(); // A point -> the points just left of it on some chain.
  for (const edge of forward) {
    const chain = [edge.source];
    for (let index = column.get(edge.source) + 1; index < column.get(edge.target); index += 1) {
      const waypoint = `${edge.source}>${edge.target}@${index}`;
      place(waypoint, index);
      chain.push(waypoint);
    }
    chain.push(edge.target);
    chains.set(edge, chain);
    chain.slice(1).forEach((point, index) => {
      if (!before.has(point)) before.set(point, []);
      before.get(point).push(chain[index]);
    });
  }
  // 4. Rows: each column is ordered by the mean row of the points just left of its own.
  const row = new Map();
  const meanRow = (id) => {
    const rows = (before.get(id) || []).map((point) => row.get(point)).filter((r) => r !== undefined);
    return rows.length ? rows.reduce((sum, r) => sum + r, 0) / rows.length : Number.MAX_SAFE_INTEGER;
  };
  columns.forEach((members, index) => {
    if (index > 0) members.sort((a, b) => meanRow(a) - meanRow(b) || order(a) - order(b));
    members.forEach((id, position) => row.set(id, position));
  });
  const position = new Map([...column.keys()].map((id) => [id, {
    x: PAD + column.get(id) * (NODE_W + GAP_X), y: PAD + row.get(id) * (NODE_H + GAP_Y)}]));
  const tallest = Math.max(...columns.filter(Boolean).map((members) => members.length));
  return {
    position, back, chains,
    width: PAD * 2 + columns.length * (NODE_W + GAP_X) - GAP_X,
    height: PAD * 2 + tallest * (NODE_H + GAP_Y) - GAP_Y,
  };
}

function edgeLayer(edges, layout) {
  const heads = ["plain", "wait", "met", "deadlock"].map((kind) =>
    svg("marker", {id: `head-${kind}`, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7,
                   markerHeight: 7, orient: "auto-start-reverse"},
      svg("path", {d: "M0,0 L10,5 L0,10 z", class: `head ${kind}`})));
  const paths = edges.map((edge) => {
    const {d, mid} = route(edge, layout);
    const touches = edge.source === ui.task || edge.target === ui.task;
    const focus = !ui.task ? "" : touches ? " focus" : " dim";
    const head = ["wait", "met", "deadlock"].includes(edge.kind) ? edge.kind : "plain";
    const style = edge.kind === "requirement" ? "" : edge.kind;
    return [
      svg("path", {d, class: `edge ${style}${focus}`, "marker-end": `url(#head-${head})`},
        svg("title", {}, edge.links.map(linkSentence).join("\n"))),
      edge.label ? svg("text", {x: mid.x, y: mid.y - 5, "text-anchor": "middle",
                                class: `edge-label${focus}`}, edge.label) : null,
    ];
  });
  return svg("svg", {width: layout.width, height: layout.height, "aria-hidden": "true"},
    svg("defs", {}, heads), paths);
}

/** The path of an edge: curves between columns, straight across each empty slot it passes.
    An edge that closes a cycle always points to an earlier column; it runs back from the
    left of its source to the right of its target. */
function route(edge, layout) {
  if (layout.back.has(edge)) {
    const from = layout.position.get(edge.source);
    const to = layout.position.get(edge.target);
    const x1 = from.x;
    const y1 = from.y + NODE_H / 2 + 16; // Below the forward edge between the same two tasks.
    const x2 = to.x + NODE_W;
    const y2 = to.y + NODE_H / 2 + 16;
    const bend = Math.max(30, (x1 - x2) / 2);
    return {d: `M${x1},${y1} C${x1 - bend},${y1} ${x2 + bend},${y2} ${x2},${y2}`,
            mid: {x: (x1 + x2) / 2, y: (y1 + y2) / 2 + 17}};
  }
  const points = layout.chains.get(edge).map((id) => layout.position.get(id));
  let x = points[0].x + NODE_W;
  let y = points[0].y + NODE_H / 2;
  let d = `M${x},${y}`;
  let mid = null;
  points.slice(1).forEach((point, index) => {
    const tx = point.x;
    const ty = point.y + NODE_H / 2;
    const bend = Math.max(30, (tx - x) / 2);
    d += ` C${x + bend},${y} ${tx - bend},${ty} ${tx},${ty}`;
    mid ??= {x: (x + tx) / 2, y: (y + ty) / 2};
    x = tx;
    y = ty;
    if (index < points.length - 2) { // Across an empty slot.
      x = point.x + NODE_W;
      d += ` L${x},${y}`;
    }
  });
  return {d, mid};
}

function graphNode(id, model, position, edges) {
  const node = id === "operator" ? operatorNode(model) : taskNode(id, model);
  node.style.left = `${position.x}px`;
  node.style.top = `${position.y}px`;
  const near = (edge) => (edge.source === id && edge.target === ui.task)
    || (edge.target === id && edge.source === ui.task);
  if (ui.task && id !== ui.task && !edges.some(near)) node.classList.add("dim");
  return node;
}

function operatorNode(model) {
  const asks = model.graph.links.filter((link) => link.source === "operator" && !link.met);
  return h("button", {type: "button", class: "gnode operator", onclick: openRequests,
                      title: "Open the requests that wait for your answer"},
    h("span", {class: "line"}, h("strong", {}, "You")),
    h("span", {class: "line title"}, `${plural(unique(asks.map((link) => link.term)).length, "answer")} awaited`),
    h("span", {class: "line"}, "Open the requests"));
}

function taskNode(id, model) {
  const task = model.byId.get(id) || {id, title: "", state: "open", executors: []};
  const waits = (model.notes.get(id) || []).filter((note) => !note.met).map(noteSentence);
  const waiting = task.executors.filter((member) => member.state === "waiting").map((m) => m.agent);
  const third = waits.length ? waits.join("; ")
    : waiting.length ? `${waiting.join(", ")} waiting`
    : task.executors.length ? `executors ${task.executors.map((member) => member.agent).join(", ")}`
    : publisherText(task);
  const [label, tone] = TASK_STATES[task.state] || [task.state, "quiet"];
  const classes = ["gnode", model.deadlockOf.has(id) && "deadlock", id === ui.task && "selected"];
  return h("button", {type: "button", class: classes.filter(present).join(" "),
                      title: `${id}: ${task.title}\n${third}`, onclick: () => selectTask(id)},
    h("span", {class: "line"}, h("span", {class: `state-dot ${task.state}`}), " ", h("strong", {}, id),
      " ", badge(label, tone),
      model.onOperator.has(id) ? [" ", badge("waits on you", "attention")] : null),
    h("span", {class: "line title"}, task.title),
    h("span", {class: "line muted"}, third));
}

/** Waits that involve no other task, for tasks that are not in the drawing. */
function innerWaits(model, drawnIds) {
  const byTask = groupBy(model.graph.notes.filter((note) => !note.met && !drawnIds.has(note.task)),
                         (note) => note.task);
  if (!byTask.size) return null;
  return h("section", {class: "inner-waits"},
    h("h3", {}, "Waits within a task"),
    h("p", {class: "muted"}, "These waits involve no other task: an agent waits on its own ",
      "candidate, a validation, a decision, or a peer's answer."),
    h("ul", {class: "outline-list"}, [...byTask].map(([id, notes]) => h("li", {class: "outline-item"},
      model.byId.has(id) ? taskRow(model.byId.get(id), model) : id,
      h("div", {class: "muted"}, notes.map(noteSentence).join("; "))))));
}

function noteSentence(note) {
  const who = note.agent || "Its contract";
  if (note.kind === "resume") return `${who} paused: ${note.detail}`;
  if (note.kind === "unreadable") return `${who}: ${note.detail}`;
  return `${who} waits for ${note.detail}`;
}

function linkSentence(link) {
  const what = link.kind === "wait" ? `${link.agent} on ${link.target} waits for ${link.detail}`
    : `${link.target}: ${link.detail}`;
  return `${what} (${link.met ? "met" : "not met"})`;
}

// ------------------------------------------------------------- task detail
async function refreshTaskDetail(model) {
  const id = ui.task;
  if (!id) {
    draw("task-detail", null, () => h("div", {class: "empty small"}, h("p", {},
      "Select a task to see its contract, its executors and what links it to other tasks.")));
    return;
  }
  const detail = await api(`/v1/tasks/${encodeURIComponent(id)}`);
  if (id !== ui.task) return; // The selection changed meanwhile.
  const related = [model.needs.get(id), model.neededBy.get(id), model.notes.get(id),
                   (model.children.get(id) || []).map((task) => [task.id, task.state])];
  draw("task-detail", {detail, related}, () => taskDetailView(detail, model));
}

function taskDetailView(detail, model) {
  const id = detail.id;
  const task = model.byId.get(id);
  const met = (yes) => badge(yes ? "met" : "not met", yes ? "ok" : "attention");
  const linkWhat = (link) => (link.kind === "wait" ? `${link.agent} waits for ${link.detail}` : link.detail);
  return [
    h("div", {class: "eyebrow"}, "TASK"),
    h("h2", {}, `${id}: ${detail.title}`),
    h("div", {class: "row"}, task ? taskMarks(task, model) : null,
      badge(`revision ${detail.revision}`, "quiet"), badge(`priority ${detail.priority}`, "quiet")),
    task ? h("p", {class: "muted"}, origin(task)) : null,
    section("Published from this task", model.children.get(id), (child) => [
      h("span", {class: `state-dot ${child.state}`}), " ", taskLink(child.id), ` ${child.title}`,
      child.relation === "repair" ? " (repair)" : ""]),
    section("Needs", model.needs.get(id), (link) => [met(link.met), " ", taskLink(link.source), " · ", linkWhat(link)]),
    section("Needed by", model.neededBy.get(id), (link) => [met(link.met), " ", taskLink(link.target), " · ",
                                                           linkWhat(link)]),
    section("Waits within the task", model.notes.get(id), (note) => [met(note.met), " ", noteSentence(note)]),
    section("Executors", detail.members, (member) => [h("strong", {}, member.agent_id),
      ` · ${member.role} · ${member.state}`, member.state === "waiting" && member.wait_reason
        ? ` · ${member.wait_reason}` : ""]),
    section("Candidates", detail.candidates, (candidate) => [h("strong", {}, candidate.id), " ",
      badge(candidate.state, CANDIDATE_TONES[candidate.state] || "quiet"),
      ` ${candidate.kind} by ${candidate.submitter}`, candidate.reason ? ` · ${candidate.reason}` : ""]),
    h("details", {dataset: {key: `contract:${id}`}}, h("summary", {}, "Contract"),
      h("pre", {class: "contract"}, detail.markdown)),
  ];
}

function origin(task) {
  if (task.relation === "subtask") return [`Published by ${task.publisher} in a turn on `, taskLink(task.parent), "."];
  if (task.relation === "repair") return ["Repairs the evidence of ", taskLink(task.parent), `; ${publisherText(task)}.`];
  return [`A top-level task, ${publisherText(task)}.`];
}

function section(title, items, line) {
  if (!items || !items.length) return null;
  return [h("h3", {}, title), h("ul", {}, items.map((item) => h("li", {}, line(item))))];
}

// =================================================================== wiring
function start() {
  for (const tab of document.querySelectorAll(".view-tab")) {
    tab.addEventListener("click", () => setView(tab.dataset.view));
  }
  for (const tab of document.querySelectorAll(".tab[data-layer]")) {
    tab.addEventListener("click", () => setLayer(tab.dataset.layer));
  }
  $("hide-met").addEventListener("change", (event) => { ui.hideMet = event.target.checked; refresh(); });
  $("agent-filter").addEventListener("input", () => data.status && drawAgentList());
  $("open-requests").addEventListener("click", openRequests);
  $("open-activity").addEventListener("click", openActivity);
  $("open-setup").addEventListener("click", () => $("setup-dialog").showModal());
  $("compose").addEventListener("submit", sendMessage);
  $("setup-form").addEventListener("submit", createTaskZero);
  document.addEventListener("click", (event) => {
    const close = event.target.closest("[data-close]");
    if (close) close.closest("dialog").close();
  });
  for (const dialog of document.querySelectorAll("dialog")) {
    dialog.querySelector(".dialog-body").prepend(h("p", {class: "dialog-error", role: "alert"}));
  }
  document.addEventListener("visibilitychange", () => document.hidden || refresh());
  window.addEventListener("hashchange", () => {
    loadLocation();
    setView(ui.view);
  });
  loadLocation();
  setView(ui.view);
  setInterval(() => document.hidden || refresh(), REFRESH_MS);
}

start();
