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

// Real keyboard and mouse input through the browser's debugging interface, for pages (Google Docs, games,
// many web apps) that ignore scripted clicks and typing. Chrome shows a notice while this is in use.
async function realInput(tabId, work) {
  const target = { tabId };
  await chrome.debugger.attach(target, "1.3");
  try {
    await work((method, params) => chrome.debugger.sendCommand(target, method, params));
  } finally {
    try { await chrome.debugger.detach(target); } catch { /* the tab closed or navigated away */ }
  }
}

const KEYS = {
  enter: ["Enter", "Enter", 13, "\r"], tab: ["Tab", "Tab", 9], escape: ["Escape", "Escape", 27], backspace: ["Backspace", "Backspace", 8],
  delete: ["Delete", "Delete", 46], space: [" ", "Space", 32, " "], home: ["Home", "Home", 36], end: ["End", "End", 35],
  pageup: ["PageUp", "PageUp", 33], pagedown: ["PageDown", "PageDown", 34], arrowleft: ["ArrowLeft", "ArrowLeft", 37],
  arrowup: ["ArrowUp", "ArrowUp", 38], arrowright: ["ArrowRight", "ArrowRight", 39], arrowdown: ["ArrowDown", "ArrowDown", 40],
  period: [".", "Period", 190], comma: [",", "Comma", 188], slash: ["/", "Slash", 191], minus: ["-", "Minus", 189], equal: ["=", "Equal", 187],
};

async function pressKey(send, name, modifiers) {
  let spec = KEYS[String(name).toLowerCase()];
  if (!spec && /^[a-z]$/i.test(name)) spec = [modifiers & 8 ? name.toUpperCase() : name.toLowerCase(), "Key" + name.toUpperCase(), name.toUpperCase().charCodeAt(0)];
  if (!spec && /^[0-9]$/.test(name)) spec = [name, "Digit" + name, name.charCodeAt(0)];
  if (!spec && /^f([1-9]|1[0-2])$/i.test(name)) spec = [name.toUpperCase(), name.toUpperCase(), 111 + Number(name.slice(1))];
  if (!spec) throw new Error(`Unknown key: ${name}`);
  const [key, code, vk, text] = spec;
  const base = { key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers };
  const plain = !(modifiers & 7);           // with Ctrl/Alt/Meta held, a key is a shortcut, not a character
  const char = text || (plain && key.length === 1 ? key : undefined);
  await send("Input.dispatchKeyEvent", { type: char ? "keyDown" : "rawKeyDown", ...base, ...(char ? { text: char } : {}) });
  await send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
}

// The functions below run inside the web page. Each must be self-contained.

function locateControl(ref) {
  const e = document.querySelector(`[data-jarvis-ref="${Number(ref)}"]`);
  if (!e) return { error: "That control is no longer on the page. List the controls again." };
  if ((e.getAttribute("type") || "").toLowerCase() === "password") return { error: "Jarvis does not touch password fields." };
  e.scrollIntoView({ block: "center", inline: "center" });
  const r = e.getBoundingClientRect();
  return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2), url: location.href,
           label: (e.getAttribute("aria-label") || e.innerText || e.value || "").trim().slice(0, 80) };
}

function focusControl(ref) {
  const e = document.querySelector(`[data-jarvis-ref="${Number(ref)}"]`);
  if (!e) return { error: "That field is no longer on the page. List the controls again." };
  const type = (e.getAttribute("type") || "").toLowerCase();
  if (type === "password" || (e.getAttribute("autocomplete") || "").includes("password")) return { error: "Jarvis does not type into password fields." };
  if (e instanceof HTMLSelectElement) return { kind: "select", url: location.href };
  if (!(e.isContentEditable || e instanceof HTMLInputElement || e instanceof HTMLTextAreaElement)) return { error: "That control is not something text can be typed into." };
  e.scrollIntoView({ block: "center" });
  e.focus();
  if (e.isContentEditable) document.execCommand("selectAll", false); else e.select();      // what is typed replaces what was there
  return { kind: "text", url: location.href };
}

function cursorIsSafe() {
  let e = document.activeElement;
  while (e && e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement;
  const type = e ? (e.getAttribute("type") || "").toLowerCase() : "";
  if (type === "password" || (e && (e.getAttribute("autocomplete") || "").includes("password"))) return { error: "The cursor is in a password field. Jarvis does not type there." };
  return { ok: true };
}

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
    // Pages such as Google Classroom fill themselves in after loading, so wait until the text stops growing.
    const [frame] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: async (limit) => {
        const text = () => { const b = document.body; return b ? (b.innerText || b.textContent || "").trim() : ""; };
        let last = text();
        for (let i = 0; i < 16; i++) {
          await new Promise((r) => setTimeout(r, 500));
          const now = text();
          if (now.length >= 200 && now.length === last.length && document.readyState === "complete") { last = now; break; }
          last = now;
        }
        return { title: document.title, url: location.href, text: last.slice(0, limit), length: last.length };
      },
      args: [args.limit || 8000],
    });
    return { tab_id: tab.id, ...frame.result };
  }
  if (command === "open_tab") {
    if (!/^https?:\/\//.test(args.url || "")) throw new Error("Only http and https addresses can be opened.");
    const tab = await chrome.tabs.create({ url: args.url, active: false, ...(args.window_id ? { windowId: args.window_id } : {}) });      // in the background: nothing is interrupted
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
  if (command === "open_window") {
    const urls = (args.urls || []).filter((u) => /^https?:\/\//.test(u));
    if (!urls.length) throw new Error("Give at least one http or https address.");
    const win = await chrome.windows.create({ url: urls, focused: true });
    await Promise.all(win.tabs.map((t) => loaded(t.id)));
    return { window: win.id, tabs: (await chrome.tabs.query({ windowId: win.id })).map(describe) };
  }
  if (command === "group_tabs") {
    const ids = args.tab_ids || [];
    if (!ids.length) throw new Error("Give the tabs to group.");
    const first = await chrome.tabs.get(ids[0]);
    const existing = (await chrome.tabGroups.query({ windowId: first.windowId })).find((g) => (g.title || "").toLowerCase() === String(args.name || "").toLowerCase());
    const groupId = await chrome.tabs.group(existing ? { tabIds: ids, groupId: existing.id } : { tabIds: ids, createProperties: { windowId: first.windowId } });
    const colors = ["grey", "blue", "red", "yellow", "green", "pink", "purple", "cyan", "orange"];
    await chrome.tabGroups.update(groupId, { title: args.name || "", ...(colors.includes(args.color) ? { color: args.color } : {}) });
    return { group: args.name || "", window: first.windowId, tabs: (await chrome.tabs.query({ groupId })).map(describe) };
  }
  if (command === "list_controls") return inPage(await pageTab(args.tab_id), listControls, [args.limit || 80]);
  if (command === "click") {
    const tab = await pageTab(args.tab_id);
    const spot = await inPage(tab, locateControl, [args.ref]);
    try {
      await realInput(tab.id, async (send) => {
        const at = { x: spot.x, y: spot.y, button: "left", clickCount: 1 };
        await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: spot.x, y: spot.y });
        await send("Input.dispatchMouseEvent", { type: "mousePressed", ...at });
        await send("Input.dispatchMouseEvent", { type: "mouseReleased", ...at });
      });
      return { tab_id: tab.id, clicked: spot.label, url: spot.url, how: "real mouse click" };
    } catch (error) {       // the debugger is unavailable (e.g. DevTools is open on that tab): fall back to a scripted click
      return { ...(await inPage(tab, clickControl, [args.ref])), how: "scripted click" };
    }
  }
  if (command === "type") {
    const tab = await pageTab(args.tab_id);
    const text = args.text || "";
    const field = await inPage(tab, focusControl, [args.ref]);
    if (field.kind === "select") return inPage(tab, typeIntoControl, [args.ref, text, false]);
    try {
      await realInput(tab.id, async (send) => {
        await send("Input.insertText", { text });
        if (args.submit) await pressKey(send, "Enter", 0);
      });
      return { tab_id: tab.id, typed: text.length, submitted: Boolean(args.submit), url: field.url, how: "real keyboard" };
    } catch (error) {
      return { ...(await inPage(tab, typeIntoControl, [args.ref, text, Boolean(args.submit)])), how: "scripted typing" };
    }
  }
  if (command === "keys") {
    const tab = await pageTab(args.tab_id);
    await inPage(tab, cursorIsSafe, []);
    await realInput(tab.id, async (send) => {
      for (const combo of args.keys || []) {
        const parts = String(combo).split("+").map((p) => p.trim()).filter(Boolean);
        const key = parts.pop();
        const modifiers = parts.reduce((m, p) => m | ({ alt: 1, control: 2, ctrl: 2, meta: 4, command: 4, shift: 8 }[p.toLowerCase()] || 0), 0);
        await pressKey(send, key, modifiers);
      }
      if (args.text) await send("Input.insertText", { text: args.text });
    });
    return { tab_id: tab.id, pressed: (args.keys || []).length, typed: (args.text || "").length };
  }
  throw new Error(`Unknown request: ${command}`);
}

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.alarms.create("jarvis-reconnect", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(connect);
chrome.storage.onChanged.addListener((changes) => { if (changes.jarvisKey) { if (socket) socket.close(); connect(); } });
connect();
