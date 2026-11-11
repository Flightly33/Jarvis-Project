// Interface logic for Jarvis. No dependencies: everything the page needs is in this file.

const $ = (selector, root = document) => root.querySelector(selector);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

const state = {
  processing: false,
  voice: { state: "off", available: false },
  level: 0,
  approvals: new Map(),
  streaming: null,
  settings: {},
  connected: false,
};

// ------------------------------------------------------------------ server

async function api(path, method = "GET", body) {
  const response = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await response.json(); } catch { /* no body */ }
  if (!response.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : `Request failed (${response.status})`);
  }
  return data;
}

let retry = 500;
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => { retry = 500; };
  ws.onmessage = (message) => {
    try { handle(JSON.parse(message.data)); } catch (error) { console.error(error); }
  };
  ws.onclose = () => {
    state.connected = false;
    setConnection("offline");
    setTimeout(connect, retry);
    retry = Math.min(retry * 2, 5000);
  };
}

function setConnection(value) {
  const pill = $("#conn");
  pill.dataset.state = value;
  pill.textContent = value;
}

// ------------------------------------------------------------------ events

function handle(event) {
  switch (event.type) {
    case "hello": return onHello(event);
    case "state": state.processing = event.processing; return refreshStatus();
    case "voice": state.voice = event; return refreshStatus();
    case "voice_level": state.level = event.level; return;
    case "user_message": return addMessage("user", event.text, { source: event.source });
    case "assistant_delta": return onDelta(event.text);
    case "assistant_message": return onReply(event);
    case "tool_start": return activityStart(event);
    case "tool_end": return activityEnd(event);
    case "approval_request": return addApproval(event.request);
    case "approval_resolved": return removeApproval(event.id);
    case "notice": return toast(event.text, event.level, event.level === "warning" ? "Heads up" : "Note");
    case "reminder": toast(event.text, "info", event.late ? "Reminder (missed earlier)" : "Reminder"); return loadReminders();
    case "reminders_changed": return loadReminders();
    case "calendar_changed": return loadCalendar();
    case "plan": return showPlan(event);
    case "estop": return toast("Everything Jarvis was doing has been stopped.", "warning", "Stopped");
    case "conversation": return divider("New conversation");
    case "job": if (event.finished) toast(`${event.job.command}: ${event.job.status}`, event.job.status === "finished" ? "info" : "warning", "Background command"); return;
    case "settings_changed": return $("#settings").open ? refreshSettingsQuietly() : loadSettings();
    case "backend": $("#brain").textContent = event.label; return;
    default: return;
  }
}

function onHello(snapshot) {
  state.connected = true;
  setConnection("online");
  state.processing = snapshot.processing;
  state.voice = snapshot.voice;
  state.settings = snapshot.settings;
  $("#brain").textContent = snapshot.backend.label;
  if (snapshot.backend.plan && snapshot.backend.plan.status) showPlan(snapshot.backend.plan);
  $("#log").replaceChildren();
  state.streaming = null;
  if (snapshot.history.length === 0) welcome();
  for (const entry of snapshot.history) addMessage(entry.role, entry.text, { source: entry.meta && entry.meta.source, quiet: true });
  $("#approvals .list").replaceChildren();
  state.approvals.clear();
  snapshot.approvals.forEach(addApproval);
  renderReminders(snapshot.reminders);
  loadCalendar();
  refreshStatus();
  scrollDown(true);
}

// ------------------------------------------------------------ conversation

function welcome() {
  const box = el("div", "welcome");
  box.append(el("strong", "", greeting()));
  box.append(el("span", "", "Ask in plain words. Jarvis works out the steps, asks before anything risky, and tells you what it did."));
  const list = el("ul");
  for (const idea of ["What's using the most memory on this computer?", "Create a folder called school with a todo.md inside it", "Remind me in ten minutes to take a break"]) {
    const item = el("li");
    const button = el("button", "", idea);
    button.type = "button";
    button.addEventListener("click", () => send(idea));
    item.append(button);
    list.append(item);
  }
  box.append(list);
  $("#log").append(box);
}

function greeting() {
  const hour = new Date().getHours();
  const part = hour < 5 ? "Good evening" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  const name = state.settings.honorific || state.settings.name;
  return name ? `${part}, ${name}.` : `${part}.`;
}

function addMessage(role, text, options = {}) {
  const welcomeBox = $("#log .welcome");
  if (welcomeBox) welcomeBox.remove();
  const wrap = el("div", `msg ${role}`);
  const who = el("div", "who", role === "user" ? "You" : "Jarvis");
  if (options.source && options.source.startsWith("voice")) who.append(el("span", "tag", "spoken"));
  const body = el("div", "body");
  if (role === "user") body.textContent = text; else body.innerHTML = markdown(text);
  wrap.append(who, body);
  $("#log").append(wrap);
  if (!options.quiet) scrollDown();
  return wrap;
}

function onDelta(text) {
  if (!state.streaming) {
    const wrap = addMessage("assistant", "");
    state.streaming = { wrap, body: $(".body", wrap), text: "", pending: false };
    state.streaming.body.classList.add("caret");
  }
  const stream = state.streaming;
  stream.text += text;
  if (!stream.pending) {
    stream.pending = true;
    requestAnimationFrame(() => {
      stream.pending = false;
      stream.body.innerHTML = markdown(stream.text);
      scrollDown();
    });
  }
}

function onReply(event) {
  const stream = state.streaming;
  state.streaming = null;
  let wrap = stream && stream.wrap;
  if (event.text) {
    if (!wrap) wrap = addMessage("assistant", event.text);
    const body = $(".body", wrap);
    body.classList.remove("caret");
    body.innerHTML = markdown(event.text);
  } else if (wrap) {
    wrap.remove();
    wrap = null;
  }
  if (event.interrupted && wrap) $(".who", wrap).append(el("span", "tag", "stopped"));
  if (event.error) {
    const failure = addMessage("assistant", "");
    failure.classList.add("error");
    $(".body", failure).textContent = event.error;
  }
  scrollDown();
}

function divider(label) {
  const line = el("div", "msg");
  line.append(el("div", "who", `- ${label} -`));
  $("#log").append(line);
  scrollDown();
}

function scrollDown(force) {
  const log = $("#log");
  if (force || log.scrollHeight - log.scrollTop - log.clientHeight < 240) log.scrollTop = log.scrollHeight;
}

async function send(text) {
  text = text.trim();
  if (!text) return;
  try { await api("/api/message", "POST", { text }); } catch (error) { toast(error.message, "error", "Could not send"); }
}

// Markdown, rendered safely: everything is escaped first, then a small set of patterns is turned into tags.
const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function inline(source) {
  let s = escapeHtml(source);
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  return s;
}

function markdown(source) {
  const parts = (source || "").split("```");
  let html = "";
  parts.forEach((part, index) => {
    if (index % 2 === 1) {
      const newline = part.indexOf("\n");
      const code = newline >= 0 ? part.slice(newline + 1) : part;
      html += `<pre><code>${escapeHtml(code.replace(/\n$/, ""))}</code></pre>`;
      return;
    }
    let list = null;
    let paragraph = [];
    const flushParagraph = () => { if (paragraph.length) { html += `<p>${paragraph.map(inline).join("<br>")}</p>`; paragraph = []; } };
    const flushList = () => { if (list) { html += `</${list}>`; list = null; } };
    for (const raw of part.split("\n")) {
      const line = raw.trimEnd();
      const bullet = line.match(/^\s*[-*]\s+(.*)$/);
      const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
      const heading = line.match(/^#{1,4}\s+(.*)$/);
      if (bullet || numbered) {
        flushParagraph();
        const kind = bullet ? "ul" : "ol";
        if (list !== kind) { flushList(); html += `<${kind}>`; list = kind; }
        html += `<li>${inline((bullet || numbered)[1])}</li>`;
      } else if (heading) {
        flushParagraph(); flushList();
        html += `<h3>${inline(heading[1])}</h3>`;
      } else if (line.trim() === "") {
        flushParagraph(); flushList();
      } else {
        flushList();
        paragraph.push(line);
      }
    }
    flushParagraph(); flushList();
  });
  return html;
}

// --------------------------------------------------------------- activity

const activity = new Map();

function activityStart(event) {
  $("#activity .empty").hidden = true;
  const item = el("li", "act");
  item.dataset.state = "running";
  const text = el("div");
  text.append(el("div", "what", event.title || event.name), el("div", "result", "working…"));
  item.append(el("span", "dot"), text);
  const list = $("#activity .list");
  list.prepend(item);
  while (list.children.length > 60) list.lastChild.remove();
  activity.set(event.id, item);
}

function activityEnd(event) {
  let item = activity.get(event.id);
  if (!item) {                       // refused before it started: show it anyway
    activityStart(event);
    item = activity.get(event.id);
  }
  item.dataset.state = event.ok ? "ok" : "fail";
  const duration = event.duration_ms > 900 ? ` · ${(event.duration_ms / 1000).toFixed(1)}s` : "";
  $(".result", item).textContent = `${event.summary || (event.ok ? "done" : "failed")}${duration}`;
  activity.delete(event.id);
}

// -------------------------------------------------------------- approvals

function addApproval(request) {
  if (state.approvals.has(request.id)) return;
  const card = el("div", "card");
  card.dataset.risk = request.risk === "high" ? "high" : "normal";
  card.append(el("span", "risk", request.risk === "high" ? "High risk" : request.risk), el("div", "title", request.title));
  if (request.reasons.length) {
    const reasons = el("ul");
    request.reasons.forEach((reason) => reasons.append(el("li", "", reason)));
    card.append(reasons);
  }
  if (request.detail) card.append(el("pre", "", request.detail));
  const actions = el("div", "actions");
  const answer = (approved, scope) => async () => {
    try { await api(`/api/approvals/${request.id}`, "POST", { approved, scope }); } catch (error) { toast(error.message, "error", "Approval"); removeApproval(request.id); }
  };
  const button = (label, cls, handler) => { const b = el("button", `btn small ${cls}`, label); b.type = "button"; b.addEventListener("click", handler); return b; };
  actions.append(button("Approve", "approve", answer(true, "once")));
  if (request.allow_request_scope) actions.append(button("Approve for this request", "", answer(true, "request")));
  if (request.allow_always) actions.append(button("Always allow", "", answer(true, "always")));
  actions.append(button("Deny", "danger", answer(false, "once")));
  const timer = el("div", "timer");
  const bar = el("span");
  timer.append(bar);
  card.append(actions, timer);
  $("#approvals .list").append(card);
  const total = Math.max(1, request.expires_at - request.created_at);
  const tick = setInterval(() => {
    const left = request.expires_at - Date.now() / 1000;
    bar.style.width = `${Math.max(0, Math.min(100, (left / total) * 100))}%`;
    if (left <= 0) clearInterval(tick);
  }, 500);
  state.approvals.set(request.id, { card, tick });
  $("#approvals").hidden = false;
  refreshStatus();
}

function removeApproval(id) {
  const entry = state.approvals.get(id);
  if (!entry) return;
  clearInterval(entry.tick);
  entry.card.remove();
  state.approvals.delete(id);
  $("#approvals").hidden = state.approvals.size === 0;
  refreshStatus();
}

// ---------------------------------------------------------------- status

function mode() {
  if (!state.connected) return "off";
  if (state.approvals.size) return "approval";
  if (state.voice.state === "speaking") return "speaking";
  if (state.voice.state === "listening") return "listening";
  if (state.voice.state === "transcribing" || state.processing) return "processing";
  return "idle";
}

function refreshStatus() {
  const current = mode();
  const voice = state.voice;
  const labels = { off: "Disconnected", approval: "Waiting for your approval", speaking: "Speaking", listening: "Listening", processing: "Working", idle: "Standing by" };
  let label = labels[current];
  let sub = "";
  if (current === "idle") {
    if (voice.state === "loading") { label = "Preparing voice"; sub = voice.detail || "Getting the speech models ready…"; }
    else if (voice.state === "error") sub = voice.detail || "Voice is unavailable. You can still type.";
    else if (voice.state === "paused") sub = "Microphone paused";
    else if (voice.state === "idle" && voice.wake_enabled) sub = `Say "${capitalise(voice.wake_word || "Jarvis")}" or type below`;
    else if (voice.state === "idle") sub = "Press Talk or type below";
    else sub = "Type below";
  } else if (current === "listening") sub = voice.detail || "Go ahead";
  else if (current === "approval") sub = "See the panel on the right";
  else if (current === "processing" && voice.state === "transcribing") sub = "Working out what you said";
  $("#state-label").textContent = label;
  $("#state-sub").textContent = sub;

  const ready = ["idle", "listening", "speaking", "transcribing"].includes(voice.state);
  const mic = $("#mic");
  mic.disabled = !ready;
  mic.dataset.live = String(voice.state === "listening");
  $("#mic-label").textContent = voice.state === "listening" ? "Listening" : voice.state === "speaking" ? "Interrupt" : "Talk";
  mic.title = ready ? "Speak to Jarvis without the wake word" : (voice.detail || "Voice is not running");
  const wake = $("#wake-toggle");
  wake.disabled = !voice.available;
  wake.checked = Boolean(voice.wake_enabled);
  $("#wake-text").textContent = `Wake word "${capitalise(voice.wake_word || "jarvis")}"`;
  const pause = $("#pause");
  pause.hidden = !(ready || voice.state === "paused");
  pause.textContent = voice.state === "paused" ? "Resume microphone" : "Pause microphone";
  document.title = current === "approval" ? "Approval needed - J.A.R.V.I.S." : "J.A.R.V.I.S.";
}

const capitalise = (s) => s.replace(/\b\w/g, (c) => c.toUpperCase());

function showPlan(plan) {
  const box = $("#plan");
  if (plan.utilization === null || plan.utilization === undefined) {
    box.hidden = !plan.status;
    $("#plan-fill").style.width = "0%";
    $("#plan-text").textContent = plan.status === "rejected" ? "Plan limit reached" : "Within your Claude plan";
    return;
  }
  box.hidden = false;
  const percent = Math.round(plan.utilization * 100);
  const fill = $("#plan-fill");
  fill.style.width = `${percent}%`;
  fill.dataset.level = percent >= 80 ? "high" : "normal";
  $("#plan-text").textContent = `${percent}% of plan window`;
}

function toast(text, level = "info", title = "") {
  const note = el("div", "toast");
  note.dataset.level = level;
  if (title) note.append(el("b", "", title));
  note.append(document.createTextNode(text));
  $("#toasts").append(note);
  setTimeout(() => note.remove(), level === "info" ? 8000 : 14000);
}

// -------------------------------------------------------------- reminders

async function loadReminders() {
  try { renderReminders((await api("/api/state")).reminders); } catch { /* offline */ }
}

function renderReminders(reminders) {
  const list = $("#reminders .list");
  list.replaceChildren();
  for (const reminder of reminders) {
    const item = el("li");
    const when = el("time", "", new Date(reminder.due_at * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }));
    item.append(el("span", "", reminder.text), when);
    list.append(item);
  }
  $("#reminders").hidden = reminders.length === 0;
}

// --------------------------------------------------------------- calendar

const pad = (n) => String(n).padStart(2, "0");
const dayKey = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const cal = { month: new Date(new Date().getFullYear(), new Date().getMonth(), 1), selected: dayKey(new Date()), events: [] };

async function loadCalendar() {
  const first = new Date(cal.month);
  first.setDate(1 - ((first.getDay() + 6) % 7));               // the Monday on or before the 1st
  const last = new Date(first);
  last.setDate(first.getDate() + 42);
  try { cal.events = (await api(`/api/events?start=${first.getTime() / 1000}&end=${last.getTime() / 1000}`)).events; } catch { return; }
  renderCalendar(first);
}

function renderCalendar(first) {
  $("#cal-title").textContent = cal.month.toLocaleDateString([], { month: "long", year: "numeric" });
  const grid = $("#cal-grid");
  grid.replaceChildren(...["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"].map((d) => el("div", "dow", d)));
  const busy = new Set(cal.events.map((e) => dayKey(new Date(e.start * 1000))));
  const today = dayKey(new Date());
  for (let i = 0; i < 42; i++) {
    const day = new Date(first);
    day.setDate(first.getDate() + i);
    const key = dayKey(day);
    const cell = el("button", "", String(day.getDate()));
    cell.type = "button";
    if (day.getMonth() !== cal.month.getMonth()) cell.classList.add("other");
    if (key === today) cell.classList.add("today");
    if (key === cal.selected) cell.classList.add("selected");
    if (busy.has(key)) cell.classList.add("has");
    cell.addEventListener("click", () => { cal.selected = key; renderCalendar(first); });
    grid.append(cell);
  }
  const [y, m, d] = cal.selected.split("-").map(Number);
  $("#cal-day-title").textContent = new Date(y, m - 1, d).toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
  const list = $("#cal-events");
  list.replaceChildren();
  const todays = cal.events.filter((e) => dayKey(new Date(e.start * 1000)) === cal.selected);
  if (!todays.length) list.append(el("li", "none", "Nothing planned."));
  for (const event of todays) {
    const item = el("li");
    const when = event.all_day ? "all day" : new Date(event.start * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const remove = el("button", "link", "Remove");
    remove.type = "button";
    remove.addEventListener("click", () => api(`/api/events/${event.id}`, "DELETE").catch((error) => toast(error.message, "error")));
    item.append(el("time", "", when), el("span", "", event.title), remove);
    list.append(item);
  }
}

function shiftMonth(by) {
  cal.month = new Date(cal.month.getFullYear(), cal.month.getMonth() + by, 1);
  loadCalendar();
}

function tickClock() {
  const now = new Date();
  $("#clock-time").textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  $("#clock-date").textContent = now.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
}

// --------------------------------------------------------------- settings

async function loadSettings() {
  try { state.settings = await api("/api/settings"); } catch { return; }
  fillSettings();
}

async function refreshSettingsQuietly() {
  // The dialog is open: update what we know without overwriting what the user is in the middle of choosing.
  try { state.settings = await api("/api/settings"); } catch { return; }
  showBrainFields();
}

function fillSettings() {
  const form = $("#settings-form");
  const s = state.settings;
  for (const [key, value] of Object.entries(s)) {
    const field = form.elements[key];
    if (!field) continue;
    if (field.type === "checkbox") field.checked = Boolean(value); else field.value = value ?? "";
  }
  form.elements.brain.value = s.backend === "openai" ? s.ai_provider : s.backend;
  form.elements.ai_key.value = "";
  showBrainFields();
  const list = $("#folder-list");
  list.replaceChildren();
  const row = (path, mode) => {
    const item = el("li");
    const label = el("span", "", path);
    label.append(el("em", "", mode === "write" ? "read and change" : "read only"));
    const remove = el("button", "link", "Remove");
    remove.type = "button";
    remove.addEventListener("click", async () => {
      try { state.settings = await api("/api/folders", "DELETE", { path, mode }); fillSettings(); } catch (error) { showSettingsError(error.message); }
    });
    item.append(label, remove);
    list.append(item);
  };
  (s.approved_dirs || []).forEach((p) => row(p, "write"));
  (s.read_only_dirs || []).forEach((p) => row(p, "read"));
  $("#settings-file").textContent = s.settings_file ? `More options in ${s.settings_file}` : "";
}

function showBrainFields() {
  const form = $("#settings-form");
  const choice = form.elements.brain.value;
  const api = !["claude", "demo"].includes(choice);
  $("#brain-claude").hidden = choice !== "claude";
  $("#brain-api").hidden = !api;
  if (!api) return;
  const preset = (state.settings.ai_presets || {})[choice];
  $("#ai-url-row").hidden = choice !== "custom";
  $("#ai-key-row").hidden = Boolean(preset) && !preset.needs_key;
  const sameService = state.settings.ai_provider === choice;
  form.elements.ai_key.placeholder = sameService && state.settings.ai_key_saved ? "A key is saved - leave empty to keep it" : "Paste your key";
  const link = $("#ai-keys-link");
  link.hidden = !(preset && preset.keys_page);
  if (preset && preset.keys_page) link.href = preset.keys_page;
}

function serviceBody() {
  const form = $("#settings-form");
  const choice = form.elements.brain.value;
  const body = { ai_provider: choice };
  if (choice === "custom") body.ai_base_url = form.elements.ai_base_url.value.trim();
  if (form.elements.ai_key.value.trim()) body.ai_key = form.elements.ai_key.value.trim();
  return body;
}

async function listModels() {
  try {
    state.settings = await api("/api/settings", "PATCH", serviceBody());      // save the service and key first
    $("#settings-form").elements.ai_key.value = "";
    showBrainFields();
    const found = (await api("/api/models")).models;
    const list = $("#ai-models");
    list.replaceChildren();
    for (const name of found) { const option = el("option"); option.value = name; list.append(option); }
    showSettingsError("");
    toast(`${found.length} models available. Click the Model box to choose one.`, "info");
  } catch (error) { showSettingsError(error.message); }
}

function showSettingsError(message) {
  const box = $("#settings-error");
  box.textContent = message;
  box.hidden = !message;
}

async function saveSettings() {
  const form = $("#settings-form");
  const body = {};
  for (const name of ["name", "honorific", "autonomy", "wake_word", "wake_sensitivity"]) body[name] = form.elements[name].value;
  const choice = form.elements.brain.value;
  if (choice === "claude") { body.model = form.elements.model.value; body.effort = form.elements.effort.value; }
  if (!["claude", "demo"].includes(choice)) {
    Object.assign(body, serviceBody(), { ai_model: form.elements.ai_model.value.trim(), ai_vision: form.elements.ai_vision.checked });
  }
  body.backend = ["claude", "demo"].includes(choice) ? choice : "openai";
  for (const name of ["voice_enabled", "wake_enabled"]) body[name] = form.elements[name].checked;
  for (const name of ["followup_seconds", "tts_speed", "tts_volume"]) body[name] = Number(form.elements[name].value);
  try {
    state.settings = await api("/api/settings", "PATCH", body);
    showSettingsError("");
    $("#settings").close();
    toast("Settings saved.", "info");
  } catch (error) { showSettingsError(error.message); }
}

// --------------------------------------------------------------------- orb

const canvas = $("#orb");
const ctx = canvas.getContext("2d");
const orb = { energy: 0.1, spin: 0, hue: [79, 216, 255], last: performance.now() };
const palette = { idle: [79, 216, 255], listening: [110, 235, 255], processing: [79, 216, 255], speaking: [130, 240, 220], approval: [255, 180, 84], off: [110, 125, 145] };
const calm = window.matchMedia("(prefers-reduced-motion: reduce)");

function sizeOrb() {
  const ratio = window.devicePixelRatio || 1;
  const size = canvas.clientWidth;
  canvas.width = canvas.height = Math.round(size * ratio);
}

function drawOrb(now) {
  const dt = Math.min(0.05, (now - orb.last) / 1000);
  orb.last = now;
  const current = mode();
  const t = now / 1000;
  const target = {
    off: 0.04, idle: 0.14 + 0.03 * Math.sin(t * 1.2), approval: 0.34 + 0.08 * Math.sin(t * 3),
    processing: 0.5, listening: 0.36 + Math.min(0.6, state.level * 4),
    speaking: 0.45 + 0.22 * Math.abs(Math.sin(t * 7.3) * Math.sin(t * 3.1)),
  }[current];
  orb.energy += (target - orb.energy) * Math.min(1, dt * 8);
  const speed = calm.matches ? 0 : { off: 0.02, idle: 0.12, approval: 0.3, processing: 1.5, listening: 0.4, speaking: 0.6 }[current];
  orb.spin += dt * speed;
  orb.hue = orb.hue.map((v, i) => v + (palette[current][i] - v) * Math.min(1, dt * 5));
  const [r, g, b] = orb.hue.map(Math.round);
  const rgba = (a) => `rgba(${r},${g},${b},${a})`;

  const w = canvas.width, cx = w / 2, cy = w / 2, R = w * 0.33, e = orb.energy;
  ctx.clearRect(0, 0, w, w);

  const glow = ctx.createRadialGradient(cx, cy, R * 0.2, cx, cy, R * 1.45);
  glow.addColorStop(0, rgba(0.1 + e * 0.3));
  glow.addColorStop(1, rgba(0));
  ctx.fillStyle = glow;
  ctx.beginPath(); ctx.arc(cx, cy, R * 1.45, 0, Math.PI * 2); ctx.fill();

  // outer scale: fine tick marks
  ctx.save(); ctx.translate(cx, cy); ctx.rotate(-orb.spin * 0.25);
  ctx.strokeStyle = rgba(0.28); ctx.lineWidth = Math.max(1, w / 520);
  for (let i = 0; i < 72; i++) {
    const a = (i / 72) * Math.PI * 2, long = i % 6 === 0;
    ctx.beginPath();
    ctx.moveTo(Math.cos(a) * R * 1.2, Math.sin(a) * R * 1.2);
    ctx.lineTo(Math.cos(a) * R * (long ? 1.28 : 1.24), Math.sin(a) * R * (long ? 1.28 : 1.24));
    ctx.stroke();
  }
  ctx.restore();

  // two counter-rotating segmented rings
  const ring = (radius, segments, gap, rotation, alpha, width) => {
    ctx.strokeStyle = rgba(alpha); ctx.lineWidth = width; ctx.lineCap = "round";
    for (let i = 0; i < segments; i++) {
      const start = rotation + (i / segments) * Math.PI * 2;
      ctx.beginPath(); ctx.arc(cx, cy, radius, start, start + (Math.PI * 2) / segments - gap); ctx.stroke();
    }
  };
  ring(R * 1.08, 3, 0.5, orb.spin, 0.55 + e * 0.4, w / 130);
  ring(R * 0.94, 5, 0.32, -orb.spin * 1.6, 0.3 + e * 0.3, w / 260);

  // the voice line: a closed curve that swells with sound
  ctx.beginPath();
  const points = 96, amp = R * 0.16 * e;
  for (let i = 0; i <= points; i++) {
    const a = (i / points) * Math.PI * 2;
    const wobble = Math.sin(a * 5 + t * 4.1) * 0.5 + Math.sin(a * 9 - t * 6.3) * 0.3 + Math.sin(a * 2 + t * 1.7) * 0.2;
    const rad = R * 0.74 + amp * wobble * (calm.matches ? 0.3 : 1);
    const x = cx + Math.cos(a) * rad, y = cy + Math.sin(a) * rad;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.closePath();
  ctx.strokeStyle = rgba(0.75); ctx.lineWidth = w / 220; ctx.stroke();

  const coreRadius = R * (0.4 + e * 0.1);
  const core = ctx.createRadialGradient(cx, cy - coreRadius * 0.3, coreRadius * 0.1, cx, cy, coreRadius);
  core.addColorStop(0, rgba(0.5 + e * 0.45));
  core.addColorStop(0.6, rgba(0.16 + e * 0.2));
  core.addColorStop(1, rgba(0.02));
  ctx.fillStyle = core;
  ctx.beginPath(); ctx.arc(cx, cy, coreRadius, 0, Math.PI * 2); ctx.fill();

  state.level *= 0.9;   // the microphone level decays between updates
  requestAnimationFrame(drawOrb);
}

// ------------------------------------------------------------------ wiring

const input = $("#input");
const grow = () => { input.style.height = "auto"; input.style.height = `${Math.min(180, input.scrollHeight)}px`; };
input.addEventListener("input", grow);
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("#composer").requestSubmit(); }
});
$("#composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = input.value;
  input.value = "";
  grow();
  send(text);
});

const stop = () => api("/api/stop", "POST").catch((error) => toast(error.message, "error"));
$("#stop").addEventListener("click", stop);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("#settings").open && (state.processing || state.approvals.size || ["speaking", "listening"].includes(state.voice.state))) stop();
});

$("#mic").addEventListener("click", () => {
  const action = state.voice.state === "speaking" || state.voice.state === "listening" ? "silence" : "listen";
  api(`/api/voice/${action}`, "POST").catch((error) => toast(error.message, "error", "Voice"));
});
$("#pause").addEventListener("click", () => {
  api(`/api/voice/${state.voice.state === "paused" ? "resume" : "pause"}`, "POST").catch((error) => toast(error.message, "error", "Voice"));
});
$("#wake-toggle").addEventListener("change", (event) => {
  api("/api/settings", "PATCH", { wake_enabled: event.target.checked }).then((s) => { state.settings = s; }).catch((error) => toast(error.message, "error"));
});
$("#new-chat").addEventListener("click", () => api("/api/conversation/new", "POST").catch((error) => toast(error.message, "error")));
$("#open-settings").addEventListener("click", async () => { await loadSettings(); showSettingsError(""); $("#settings").showModal(); });
$("#settings-save").addEventListener("click", saveSettings);
$("#settings-form").elements.brain.addEventListener("change", showBrainFields);
$("#ai-list").addEventListener("click", listModels);
$("#folder-add").addEventListener("click", async () => {
  const path = $("#folder-path").value.trim();
  if (!path) return;
  try {
    state.settings = await api("/api/folders", "POST", { path, mode: $("#folder-mode").value });
    $("#folder-path").value = "";
    showSettingsError("");
    fillSettings();
  } catch (error) { showSettingsError(error.message); }
});

$("#cal-prev").addEventListener("click", () => shiftMonth(-1));
$("#cal-next").addEventListener("click", () => shiftMonth(1));
$("#cal-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const title = $("#cal-text").value.trim();
  if (!title) return;
  const time = $("#cal-time").value;
  try {
    await api("/api/events", "POST", { title, start: time ? `${cal.selected}T${time}` : cal.selected, all_day: !time });
    $("#cal-text").value = "";
    $("#cal-time").value = "";
  } catch (error) { toast(error.message, "error", "Calendar"); }
});
tickClock();
setInterval(tickClock, 15000);

window.addEventListener("resize", sizeOrb);
sizeOrb();
requestAnimationFrame(drawOrb);
refreshStatus();
connect();
