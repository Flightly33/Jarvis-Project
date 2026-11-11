// Jarvis Bridge: connects this browser to the Jarvis program on the same computer (127.0.0.1 only)
// and answers three requests from it: list the tabs, read a tab, open a tab.

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
  throw new Error(`Unknown request: ${command}`);
}

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.alarms.create("jarvis-reconnect", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(connect);
chrome.storage.onChanged.addListener((changes) => { if (changes.jarvisKey) { if (socket) socket.close(); connect(); } });
connect();
