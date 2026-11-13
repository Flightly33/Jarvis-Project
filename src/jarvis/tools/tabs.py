"""The user's own browser tabs, reached through the Jarvis Bridge extension.

The extension runs inside the user's everyday Chrome or Edge and connects to
Jarvis on this computer. Through it Jarvis can list the open tabs, read a
tab's text, open, switch to, redirect and close tabs, and click or type on a
page. The extension itself refuses password fields. Pages are outside content
and, being the user's signed-in sessions, private data - and acting on them
acts as the user, so clicking, typing and closing ask first.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field

from jarvis.safety import Assessment, Risk
from jarvis.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult, Trimmed

_NOT_CONNECTED = (
    "The Jarvis Bridge browser extension is not connected, so the user's own browser cannot be reached. "
    "They can set it up in Settings > Browser extension (and the browser must be open)."
)


class ExtensionBridge:
    def __init__(self) -> None:
        self._socket: Any = None
        self._pending: dict[str, asyncio.Future] = {}

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def attach(self, socket: Any) -> None:
        self._socket = socket

    def detach(self, socket: Any) -> None:
        if self._socket is socket:
            self._socket = None
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ToolError("The browser extension disconnected."))
            self._pending.clear()

    def receive(self, message: dict[str, Any]) -> None:
        future = self._pending.pop(str(message.get("id")), None)
        if future is None or future.done():
            return
        if message.get("ok"):
            future.set_result(message.get("result"))
        else:
            future.set_exception(ToolError(f"The browser could not do that: {str(message.get('error'))[:300]}"))

    async def call(self, command: str, timeout: float = 45.0, **args: Any) -> Any:
        if self._socket is None:
            raise ToolError(_NOT_CONNECTED)
        request_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._socket.send_json({"id": request_id, "cmd": command, "args": args})
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise ToolError("The browser did not answer in time.") from None
        finally:
            self._pending.pop(request_id, None)


bridge = ExtensionBridge()


class TabsListInput(ToolInput):
    pass


def _assess_list(a: TabsListInput, ctx: ToolContext) -> Assessment:
    return Assessment(title="List the tabs open in your browser", reads_private=True)


async def _run_list(a: TabsListInput, ctx: ToolContext) -> ToolResult:
    tabs = await bridge.call("list_tabs")
    return ToolResult({"tabs": tabs}, f"{len(tabs)} tab(s) open", untrusted_source="browser tab titles")


class TabReadInput(ToolInput):
    tab_id: int = Field(default=0, description="A tab id from tabs_list. 0 reads the tab the user is currently on.")
    max_chars: int = Field(default=8000, ge=500, le=40_000)


def _assess_read(a: TabReadInput, ctx: ToolContext) -> Assessment:
    return Assessment(title="Read a tab in your browser", reads_private=True, ingests_untrusted=True)


async def _run_read(a: TabReadInput, ctx: ToolContext) -> ToolResult:
    page = await bridge.call("read_tab", tab_id=a.tab_id, limit=a.max_chars)
    body = page.get("text", "") or "(The page showed no text. It may still be loading, need the user to sign in, or draw its content in a way that cannot be read; tab_controls may still list its links and buttons.)"
    text = f"Page: {page.get('title', '')}\nAddress: {page.get('url', '')}\nTab id: {page.get('tab_id')}\n\n{body}"
    return ToolResult(text, f"read {urlsplit(page.get('url', '')).netloc or 'a tab'}", untrusted_source=page.get("url") or "a browser tab")


class TabOpenInput(ToolInput):
    url: Trimmed = Field(description="An http(s) address to open as a new background tab in the user's own browser. For Google sites, a particular "
                         "signed-in account can be chosen by adding authuser=<email> to the address's query.")
    window_id: int = Field(default=0, description="Open it in this window (a 'window' value from tabs_list or window_open). 0 uses the current window.")


def _assess_open(a: TabOpenInput, ctx: ToolContext) -> Assessment:
    parts = urlsplit(a.url)
    base = Assessment(risk=Risk.LOW, title=f"Open {a.url[:100]} in your browser", sends_external=True, grant_key="browser")
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        base.blocked = "Only http and https addresses can be opened."
    return base


async def _run_open(a: TabOpenInput, ctx: ToolContext) -> ToolResult:
    tab = await bridge.call("open_tab", url=a.url, window_id=a.window_id)
    return ToolResult({"opened": tab, "next": "Use tab_read with this tab_id to read it."}, f"opened {urlsplit(a.url).netloc} in a background tab")


class WindowOpenInput(ToolInput):
    urls: list[Trimmed] = Field(min_length=1, max_length=12, description="The http(s) addresses to open, one tab each, in a new browser window.")


def _assess_window(a: WindowOpenInput, ctx: ToolContext) -> Assessment:
    base = Assessment(risk=Risk.LOW, title=f"Open a new browser window with {len(a.urls)} tab(s)", detail="\n".join(a.urls)[:600], sends_external=True, grant_key="browser")
    if any(urlsplit(u).scheme not in {"http", "https"} or not urlsplit(u).netloc for u in a.urls):
        base.blocked = "Only http and https addresses can be opened."
    return base


async def _run_window(a: WindowOpenInput, ctx: ToolContext) -> ToolResult:
    opened = await bridge.call("open_window", timeout=75.0, urls=a.urls)
    return ToolResult(opened, f"opened a new window with {len(opened.get('tabs', []))} tab(s)", untrusted_source="browser tab titles")


class TabGroupInput(ToolInput):
    tab_ids: list[int] = Field(min_length=1, max_length=50, description="Tab ids to put in the group (all should be in the same window).")
    name: Trimmed = Field(min_length=1, max_length=40, description="The group's name. If a group with this name exists in that window, the tabs join it.")
    color: Trimmed = Field(default="", description="Optional: grey, blue, red, yellow, green, pink, purple, cyan or orange.")


def _assess_group(a: TabGroupInput, ctx: ToolContext) -> Assessment:
    return Assessment(risk=Risk.LOW, title=f"Put {len(a.tab_ids)} tab(s) in the tab group '{a.name}'")


async def _run_group(a: TabGroupInput, ctx: ToolContext) -> ToolResult:
    grouped = await bridge.call("group_tabs", tab_ids=a.tab_ids, name=a.name, color=a.color.lower())
    return ToolResult(grouped, f"grouped {len(grouped.get('tabs', []))} tab(s) as '{a.name}'", untrusted_source="browser tab titles")


class TabKeysInput(ToolInput):
    tab_id: int = Field(default=0, description="A tab id from tabs_list. 0 is the tab the user is currently on.")
    keys: list[Trimmed] = Field(default_factory=list, max_length=30, description="Keys or shortcuts to press in order, e.g. 'Control+A', 'Control+Shift+Period', 'Enter', 'ArrowDown'.")
    text: str = Field(default="", max_length=20_000, description="Text to type afterwards at the page's cursor, as a real keyboard would.")


def _assess_keys(a: TabKeysInput, ctx: ToolContext) -> Assessment:
    base = Assessment(risk=Risk.MODIFY, title="Press keys on a page in your browser", detail=(", ".join(a.keys) + "\n" + a.text[:400]).strip(),
                      reasons=["You are signed in there, so this acts as you."], sends_external=True, grant_key="tab-act")
    if not a.keys and not a.text:
        base.blocked = "Give keys, text, or both."
    return base


async def _run_keys(a: TabKeysInput, ctx: ToolContext) -> ToolResult:
    done = await bridge.call("keys", tab_id=a.tab_id, keys=a.keys, text=a.text)
    return ToolResult({"keys_pressed": done.get("pressed", 0), "typed_characters": done.get("typed", 0)}, "pressed keys on the page")


class TabIdInput(ToolInput):
    tab_id: int = Field(ge=1, description="A tab id from tabs_list.")


def _assess_switch(a: TabIdInput, ctx: ToolContext) -> Assessment:
    return Assessment(risk=Risk.LOW, title="Switch to another tab in your browser")


async def _run_switch(a: TabIdInput, ctx: ToolContext) -> ToolResult:
    tab = await bridge.call("switch_tab", tab_id=a.tab_id)
    return ToolResult({"now_showing": tab}, f"switched to {urlsplit(tab.get('url', '')).netloc or 'the tab'}", untrusted_source="browser tab titles")


def _assess_close(a: TabIdInput, ctx: ToolContext) -> Assessment:
    return Assessment(risk=Risk.MODIFY, title="Close a tab in your browser", detail="Anything unsaved on that page would be lost.", grant_key="tab-close")


async def _run_close(a: TabIdInput, ctx: ToolContext) -> ToolResult:
    tab = await bridge.call("close_tab", tab_id=a.tab_id)
    return ToolResult({"closed": tab}, f"closed {urlsplit(tab.get('url', '')).netloc or 'a tab'}", untrusted_source="browser tab titles")


class TabNavigateInput(ToolInput):
    tab_id: int = Field(default=0, description="A tab id from tabs_list. 0 is the tab the user is currently on.")
    url: Trimmed = Field(description="The http(s) address this tab should go to instead of its current page.")


def _assess_navigate(a: TabNavigateInput, ctx: ToolContext) -> Assessment:
    parts = urlsplit(a.url)
    base = Assessment(risk=Risk.MODIFY, title=f"Send a tab in your browser to {a.url[:100]}", detail="The page it shows now is replaced.",
                      sends_external=True, grant_key="browser")
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        base.blocked = "Only http and https addresses can be opened."
    return base


async def _run_navigate(a: TabNavigateInput, ctx: ToolContext) -> ToolResult:
    tab = await bridge.call("navigate_tab", tab_id=a.tab_id, url=a.url)
    return ToolResult({"tab": tab}, f"tab now at {urlsplit(a.url).netloc}", untrusted_source="browser tab titles")


class TabControlsInput(ToolInput):
    tab_id: int = Field(default=0, description="A tab id from tabs_list. 0 is the tab the user is currently on.")
    max_controls: int = Field(default=80, ge=5, le=300)


def _assess_controls(a: TabControlsInput, ctx: ToolContext) -> Assessment:
    return Assessment(title="Look at the buttons and fields on a tab in your browser", reads_private=True, ingests_untrusted=True)


async def _run_controls(a: TabControlsInput, ctx: ToolContext) -> ToolResult:
    page = await bridge.call("list_controls", tab_id=a.tab_id, limit=a.max_controls)
    lines = [f"[{c['ref']}] {c['kind']} {c['label']}".rstrip() for c in page.get("controls", [])]
    text = (f"Page: {page.get('title', '')}\nAddress: {page.get('url', '')}\nTab id: {page.get('tab_id')}\n"
            "Controls (use the number with tab_click or tab_type; numbers change whenever the page changes):\n" + "\n".join(lines)
            + ("\n(more controls exist than are listed)" if page.get("more") else ""))
    return ToolResult(text, f"{len(lines)} control(s) on {urlsplit(page.get('url', '')).netloc or 'the tab'}", untrusted_source=page.get("url") or "a browser tab")


class TabClickInput(ToolInput):
    tab_id: int = Field(default=0, description="The tab the controls were listed for. 0 is the tab the user is currently on.")
    ref: int = Field(ge=1, description="The control's number from tab_controls.")
    purpose: str = Field(default="", max_length=200, description="What this click does, in a few words, for the user to see.")


def _assess_click(a: TabClickInput, ctx: ToolContext) -> Assessment:
    return Assessment(risk=Risk.MODIFY, title=f"Click control {a.ref} on a page in your browser", detail=a.purpose,
                      reasons=["You are signed in there, so a click acts as you."], sends_external=True, grant_key="tab-act")


async def _run_click(a: TabClickInput, ctx: ToolContext) -> ToolResult:
    done = await bridge.call("click", tab_id=a.tab_id, ref=a.ref)
    return ToolResult({"clicked": done.get("clicked", ""), "next": "List the controls or read the tab again to see what changed."},
                      f"clicked '{str(done.get('clicked', ''))[:40]}'", untrusted_source=done.get("url") or "a browser tab")


class TabTypeInput(ToolInput):
    tab_id: int = Field(default=0, description="The tab the controls were listed for. 0 is the tab the user is currently on.")
    ref: int = Field(ge=1, description="The field's number from tab_controls.")
    text: str = Field(max_length=20_000, description="The text to put in the field, replacing what is there. For a drop-down list, the choice to select.")
    submit: bool = Field(default=False, description="Press Enter afterwards (e.g. to run a search).")


def _assess_type(a: TabTypeInput, ctx: ToolContext) -> Assessment:
    return Assessment(risk=Risk.MODIFY, title=f"Type into field {a.ref} on a page in your browser" + (" and press Enter" if a.submit else ""),
                      detail=a.text[:400], reasons=["You are signed in there, so this acts as you."], sends_external=True, grant_key="tab-act")


async def _run_type(a: TabTypeInput, ctx: ToolContext) -> ToolResult:
    done = await bridge.call("type", tab_id=a.tab_id, ref=a.ref, text=a.text, submit=a.submit)
    return ToolResult({"typed_characters": done.get("typed", 0), "submitted": bool(done.get("submitted"))}, "typed into the page",
                      untrusted_source=done.get("url") or "a browser tab")


TAB_TOOLS: list[Tool] = [
    Tool(
        "tabs_list",
        "List the tabs open in the user's own everyday browser (title and address). Needs the Jarvis Bridge extension.",
        TabsListInput, _run_list, _assess_list, group="tabs",
    ),
    Tool(
        "tab_read",
        "Read the text of a tab in the user's own browser, where they are already signed in to their sites. Works in "
        "the background while they do something else.",
        TabReadInput, _run_read, _assess_read, group="tabs",
    ),
    Tool(
        "tab_open",
        "Open a web address as a new background tab in the user's own browser (it does not interrupt what they are "
        "doing), then read it with tab_read. Prefer this over open_url when you need to read the page.",
        TabOpenInput, _run_open, _assess_open, group="tabs",
    ),
    Tool("window_open", "Open a new window of the user's own browser with one tab per address given.", WindowOpenInput, _run_window, _assess_window, timeout=90, group="tabs"),
    Tool("tab_group", "Put tabs of the user's own browser into a named tab group (created if it does not exist).", TabGroupInput, _run_group, _assess_group, group="tabs"),
    Tool(
        "tab_keys",
        "Press keys or shortcuts and/or type text at the cursor of a tab in the user's own browser, as a real keyboard. Use it for editors "
        "that have no listable field (Google Docs, Sheets, Slides: open docs.new, then type) and for keyboard shortcuts. The tab should be "
        "in front (tab_switch) for editors to respond. Never passwords.",
        TabKeysInput, _run_keys, _assess_keys, timeout=60, group="tabs",
    ),
    Tool("tab_switch", "Bring a tab in the user's own browser to the front.", TabIdInput, _run_switch, _assess_switch, group="tabs"),
    Tool("tab_close", "Close a tab in the user's own browser.", TabIdInput, _run_close, _assess_close, group="tabs"),
    Tool("tab_navigate", "Send an existing tab in the user's own browser to a different address.", TabNavigateInput, _run_navigate, _assess_navigate, timeout=60, group="tabs"),
    Tool(
        "tab_controls",
        "List the links, buttons and fields on a tab in the user's own browser, each with a number for tab_click and tab_type.",
        TabControlsInput, _run_controls, _assess_controls, group="tabs",
    ),
    Tool(
        "tab_click",
        "Click a numbered control on a tab in the user's own browser. The user is signed in there, so this acts as them: "
        "never click anything that sends, buys, deletes or confirms unless the user asked for exactly that.",
        TabClickInput, _run_click, _assess_click, group="tabs",
    ),
    Tool(
        "tab_type",
        "Type text into a numbered field on a tab in the user's own browser (never passwords; the extension refuses them).",
        TabTypeInput, _run_type, _assess_type, group="tabs",
    ),
]
