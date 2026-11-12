// Jarvis Bridge: connects this browser to the Jarvis program on the same computer (127.0.0.1 only)
// and answers its requests: list, read, open, switch to, change and close tabs, and click or type on a page.
// It never reads or fills in password fields.

const PORTS = Array.from({ length: 20 }, (_, i) => 8765 + i);
let socket = null;
let keepAlive = null;

function tryPort(port, key) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`ws://127.0.0.1:${port}/ext?key=${encodeURIComponent(key)}`);
    const timer = setTimeout(() => { ws.close(); reject(new Error("timeout")); }, 2500);
    ws.onopen = () => { clearTimeout(timer); resolve(ws); };
    ws.onerror = () => { clearTimeout(timer); reject(new Error("refused")); };
  });
}

async function connect() {
  if (socket && socket.readyState <= 1) return;
  const { jarvisKey } = await chrome.storage.local.get("jarvisKey");
  if (!jarvisKey) return setStatus("Not paired yet");
  for (const port of PORTS) {
    try {
      socket = await tryPort(port, jarvisKey);
    } catch { continue; }
    setStatus("Connected to Jarvis");
    socket.onmessage = onMessage;
    socket.onclose = () => { socket = null; clearInterval(keepAlive); setStatus("Jarvis is not running"); };
    clearInterval(keepAlive);
    keepAlive = setInterval(() => { if (socket && socket.readyState === 1) socket.send(JSON.stringify({ type: "ping" })); }, 20000);
    return;
  }
  setStatus("Jarvis is not running, or the pairing code is wrong");
}

function setStatus(text) { chrome.storage.local.set({ jarvisStatus: text }); }

async function onMessage(event) {
  let request;
  try { request = JSON.parse(event.data); } catch { return; }
  if (!request.id) return;
  let reply;
  try {
    reply = { id: request.id, ok: true, result: await handle(request.cmd, request.args || {}) };
  } catch (error) {
    reply = { id: request.id, ok: false, error: String(error && error.message ? error.message : error) };
  }
  if (socket && socket.readyState === 1) socket.send(JSON.stringify(reply));
}

const describe = (tab) => ({ tab_id: tab.id, title: tab.title || "", url: tab.url || "", active: Boolean(tab.active), window: tab.windowId });

function loaded(tabId) {
  return new Promise((resolve) => {
    const done = () => { chrome.tabs.onUpdated.removeListener(listener); clearTimeout(timer); resolve(); };
    const listener = (id, info) => { if (id === tabId && info.status === "complete") done(); };
    const timer = setTimeout(done, 30000);
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function pageTab(tabId, anyPage = false) {
  const tab = tabId ? await chrome.tabs.get(tabId) : (await chrome.tabs.query({ active: true, lastFocusedWindow: true }))[0];
  if (!tab) throw new Error("There is no such tab.");
  if (!anyPage && !/^https?:/.test(tab.url || "")) throw new Error("That tab is a browser page that cannot be worked with.");
  return tab;
}

async function inPage(tab, func, args) {
  const [frame] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func, args });
  if (!frame || !frame.result) throw new Error("The page did not respond.");
  if (frame.result.error) throw new Error(frame.result.error);
  return { tab_id: tab.id, ...frame.result };
}

// The three functions below run inside the web page. Each must be self-contained.

function listControls(limit) {
  const visible = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return r.width > 1 && r.height > 1 && s.visibility !== "hidden" && s.display !== "none"; };
  document.querySelectorAll("[data-jarvis-ref]").forEach((e) => e.removeAttribute("data-jarvis-ref"));
  const found = [...document.querySelectorAll('a[href], button, input:not([type=hidden]), textarea, select, [role=button], [role=link], [role=textbox], [role=tab], [role=menuitem], [contenteditable=""], [contenteditable=true]')].filter(visible);
  const controls = [];
  for (const e of found.slice(0, limit)) {
    const ref = controls.length + 1;
    e.setAttribute("data-jarvis-ref", String(ref));
    const type = (e.getAttribute("type") || "").toLowerCase();
    const secret = type === "password";
    const label = (e.getAttribute("aria-label") || e.innerText || e.getAttribute("placeholder") || e.getAttribute("title") || e.getAttribute("name") || e.getAttribute("alt") || "").trim().replace(/\s+/g, " ").slice(0, 80);
    controls.push({ ref, kind: e.tagName.toLowerCase() + (type ? ":" + type : e.isContentEditable ? ":editable" : ""), label: secret ? "(password field)" : label });
  }
  return { title: document.title, url: location.href, controls, more: found.length > limit };
}

function clickControl(ref) {
  const e = document.querySelector(`[data-jarvis-ref="${Number(ref)}"]`);
  if (!e) return { error: "That control is no longer on the page. List the controls again." };
  if ((e.getAttribute("type") || "").toLowerCase() === "password") return { error: "Jarvis does not touch password fields." };
  e.scrollIntoView({ block: "center" });
  e.focus();
  e.click();
  return { clicked: (e.getAttribute("aria-label") || e.innerText || e.value || "").trim().slice(0, 80), url: location.href };
}

function typeIntoControl(ref, text, submit) {
  const e = document.querySelector(`[data-jarvis-ref="${Number(ref)}"]`);
  if (!e) return { error: "That field is no longer on the page. List the controls again." };
  const type = (e.getAttribute("type") || "").toLowerCase();
  if (type === "password" || (e.getAttribute("autocomplete") || "").includes("password")) return { error: "Jarvis does not type into password fields." };
  e.scrollIntoView({ block: "center" });
  e.focus();
  if (e.isContentEditable) {
    document.execCommand("selectAll", false);
    document.execCommand("insertText", false, text);
  } else if (e instanceof HTMLInputElement || e instanceof HTMLTextAreaElement) {
    const proto = e instanceof HTMLInputElement ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(e, text);      // so pages built with frameworks notice the change
    e.dispatchEvent(new Event("input", { bubbles: true }));
    e.dispatchEvent(new Event("change", { bubbles: true }));
  } else if (e instanceof HTMLSelectElement) {
    const option = [...e.options].find((o) => o.text.trim().toLowerCase() === text.trim().toLowerCase() || o.value === text);
    if (!option) return { error: "That list has no such choice. Choices: " + [...e.options].map((o) => o.text.trim()).slice(0, 30).join(", ") };
    e.value = option.value;
    e.dispatchEvent(new Event("change", { bubbles: true }));
  } else {
    return { error: "That control is not something text can be typed into." };
  }
  if (submit) {
    for (const name of ["keydown", "keypress", "keyup"]) e.dispatchEvent(new KeyboardEvent(name, { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true }));
    if (e.form) { if (e.form.requestSubmit) e.form.requestSubmit(); else e.form.submit(); }
  }
  return { typed: text.length, submitted: Boolean(submit), url: location.href };
}

async function handle(command, args) {
  if (command === "list_tabs") return (await chrome.tabs.query({})).map(describe);
  if (command === "read_tab") {
    const tab = args.tab_id ? await chrome.tabs.get(args.tab_id) : (await chrome.tabs.query({ active: true, lastFocusedWindow: true }))[0];
    if (!tab) throw new Error("There is no such tab.");
    if (!/^https?:/.test(tab.url || "")) throw new Error("That tab is a browser page that cannot be read.");
    const [frame] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: (limit) => ({ title: document.title, url: location.href, text: (document.body ? document.body.innerText : "").slice(0, limit) }),
      args: [args.limit || 8000],
    });
    return { tab_id: tab.id, ...frame.result };
  }
  if (command === "open_tab") {
    if (!/^https?:\/\//.test(args.url || "")) throw new Error("Only http and https addresses can be opened.");
    const tab = await chrome.tabs.create({ url: args.url, active: false });      // in the background: nothing is interrupted
    await new Promise((resolve) => {
      const done = () => { chrome.tabs.onUpdated.removeListener(listener); clearTimeout(timer); resolve(); };
      const listener = (id, info) => { if (id === tab.id && info.status === "complete") done(); };
      const timer = setTimeout(done, 30000);
      chrome.tabs.onUpdated.addListener(listener);
    });
    return describe(await chrome.tabs.get(tab.id));
  }
  if (command === "switch_tab") {
    const tab = await chrome.tabs.update(args.tab_id, { active: true });
    await chrome.windows.update(tab.windowId, { focused: true });
    return describe(tab);
  }
  if (command === "close_tab") {
    const tab = await chrome.tabs.get(args.tab_id);
    await chrome.tabs.remove(tab.id);
    return describe(tab);
  }
  if (command === "navigate_tab") {
    if (!/^https?:\/\//.test(args.url || "")) throw new Error("Only http and https addresses can be opened.");
    const tab = await pageTab(args.tab_id, true);
    await chrome.tabs.update(tab.id, { url: args.url });
    await loaded(tab.id);
    return describe(await chrome.tabs.get(tab.id));
  }
  if (command === "list_controls") return inPage(await pageTab(args.tab_id), listControls, [args.limit || 80]);
  if (command === "click") return inPage(await pageTab(args.tab_id), clickControl, [args.ref]);
  if (command === "type") return inPage(await pageTab(args.tab_id), typeIntoControl, [args.ref, args.text || "", Boolean(args.submit)]);
  throw new Error(`Unknown request: ${command}`);
}

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.alarms.create("jarvis-reconnect", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(connect);
chrome.storage.onChanged.addListener((changes) => { if (changes.jarvisKey) { if (socket) socket.close(); connect(); } });
connect();
