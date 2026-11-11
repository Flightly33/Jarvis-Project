"""A browser Jarvis can drive: open pages, read them, click and type.

It is a separate browser window with its own profile kept in Jarvis's data
folder. The user signs in to sites there themselves (Jarvis never types into
password fields), and Jarvis can then work with those sites, webmail included.

Safety: everything read from a page is treated as outside content, pages the
user is signed in to count as private data, and clicking or typing counts as
changing something, so it follows the approval rules like any other change.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field

from jarvis.safety import Assessment, Risk
from jarvis.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult, Trimmed

_SNAPSHOT = """
(limit) => {
  const picks = 'a[href],button,input,textarea,select,[role=button],[role=link],[role=textbox],[role=menuitem],[role=tab],[contenteditable=true]';
  const out = [];
  let n = 0;
  document.querySelectorAll('[data-jarvis-ref]').forEach((e) => e.removeAttribute('data-jarvis-ref'));
  for (const e of document.querySelectorAll(picks)) {
    const box = e.getBoundingClientRect();
    const style = getComputedStyle(e);
    if (box.width < 2 || box.height < 2 || style.visibility === 'hidden' || style.display === 'none') continue;
    n += 1;
    e.setAttribute('data-jarvis-ref', String(n));
    const secret = e.type === 'password';
    const label = (e.innerText || e.getAttribute('aria-label') || e.getAttribute('placeholder') || e.getAttribute('title') || e.name || '')
      .trim().replace(/\\s+/g, ' ').slice(0, 80);
    out.push({ ref: n, kind: e.tagName.toLowerCase() + (e.type ? ':' + e.type : ''), label: secret ? '(password field)' : label });
    if (n >= 150) break;
  }
  return { title: document.title, url: location.href, text: (document.body ? document.body.innerText : '').slice(0, limit), elements: out };
}
"""


class BrowserSession:
    """One long-lived browser window with a persistent profile."""

    def __init__(self) -> None:
        self._playwright: Any = None
        self._context: Any = None
        self._lock = asyncio.Lock()
        self.elements: dict[int, dict[str, Any]] = {}
        self.url = ""

    async def page(self, ctx: ToolContext) -> Any:
        async with self._lock:
            if self._context is None:
                await self._start(ctx)
            pages = [p for p in self._context.pages if not p.is_closed()]
            return pages[-1] if pages else await self._context.new_page()

    async def _start(self, ctx: ToolContext) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ToolError("Browser control is not installed in this copy of Jarvis.") from exc
        profile = ctx.services.paths.data_dir / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        headless = os.environ.get("JARVIS_BROWSER_HEADLESS") == "1"
        custom = ctx.config.desktop.browser_path
        attempts: list[dict[str, Any]] = [{"executable_path": custom}] if custom else [{"channel": "msedge"}, {"channel": "chrome"}, {}]
        self._playwright = await async_playwright().start()
        errors = []
        for extra in attempts:
            try:
                self._context = await asyncio.wait_for(
                    self._playwright.chromium.launch_persistent_context(
                        str(profile), headless=headless, no_viewport=not headless, timeout=25_000, **extra,
                    ),
                    30,
                )
                self._context.on("close", lambda *_: setattr(self, "_context", None))
                return
            except Exception as exc:  # noqa: BLE001 - try the next installed browser
                name = extra.get("channel") or extra.get("executable_path") or "built-in browser"
                detail = str(exc).splitlines()[0][:160] if str(exc) else "did not start within 30 seconds"
                errors.append(f"{name}: {detail}")
        await self._playwright.stop()
        self._playwright = None
        raise ToolError("No browser could be started (Microsoft Edge or Google Chrome is needed). " + " | ".join(errors))

    async def close(self) -> None:
        context, playwright = self._context, self._playwright
        self._context = self._playwright = None
        for closer in (getattr(context, "close", None), getattr(playwright, "stop", None)):
            if closer:
                try:
                    await closer()
                except Exception:  # noqa: BLE001, S110 - shutting down
                    pass


_session = BrowserSession()


async def close_browser() -> None:
    await _session.close()


async def _snapshot(page: Any, limit: int) -> ToolResult:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:  # noqa: BLE001, S110 - a slow page is still readable
        pass
    await asyncio.sleep(0.6)       # let scripts fill the page in
    try:
        data = await asyncio.wait_for(page.evaluate(_SNAPSHOT, limit), 20)
    except TimeoutError:
        raise ToolError("The page did not respond when Jarvis tried to read it.") from None
    _session.elements = {e["ref"]: e for e in data["elements"]}
    _session.url = data["url"]
    lines = [f"Page: {data['title']}", f"Address: {data['url']}", "", data["text"].strip(), "", "Things that can be clicked or typed into (use the number as ref):"]
    lines += [f"  [{e['ref']}] {e['kind']}: {e['label'] or '(no label)'}" for e in data["elements"]]
    return ToolResult("\n".join(lines), f"read {urlsplit(data['url']).netloc or 'page'}", untrusted_source=data["url"])


def _where() -> str:
    return urlsplit(_session.url).netloc or "the page"


class BrowserOpenInput(ToolInput):
    url: Trimmed = Field(description="An http(s) address to open in Jarvis's browser window.")
    max_chars: int = Field(default=6000, ge=500, le=30_000)


def _assess_open(a: BrowserOpenInput, ctx: ToolContext) -> Assessment:
    parts = urlsplit(a.url)
    base = Assessment(risk=Risk.LOW, title=f"Open {a.url[:100]} in Jarvis's browser", ingests_untrusted=True, reads_private=True, sends_external=True, grant_key="browser")
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        base.blocked = "Only http and https addresses can be opened."
    return base


async def _run_open(a: BrowserOpenInput, ctx: ToolContext) -> ToolResult:
    page = await _session.page(ctx)
    try:
        await asyncio.wait_for(page.goto(a.url, wait_until="domcontentloaded", timeout=40_000), 45)
    except TimeoutError:
        raise ToolError("The page took longer than 45 seconds to load.") from None
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"The page could not be opened: {str(exc).splitlines()[0][:200]}") from exc
    if os.environ.get("JARVIS_BROWSER_HEADLESS") != "1":
        try:
            await asyncio.wait_for(page.bring_to_front(), 5)
        except Exception:  # noqa: BLE001, S110 - showing the window is a nicety
            pass
    return await _snapshot(page, a.max_chars)


class BrowserReadInput(ToolInput):
    max_chars: int = Field(default=6000, ge=500, le=30_000)


def _assess_read(a: BrowserReadInput, ctx: ToolContext) -> Assessment:
    return Assessment(title="Read the page in Jarvis's browser", ingests_untrusted=True, reads_private=True)


async def _run_read(a: BrowserReadInput, ctx: ToolContext) -> ToolResult:
    return await _snapshot(await _session.page(ctx), a.max_chars)


class BrowserClickInput(ToolInput):
    ref: int = Field(description="The number of the element, from the latest page reading.")


def _label(ref: int) -> str:
    element = _session.elements.get(ref)
    return f"'{element['label'] or element['kind']}'" if element else f"element {ref}"


def _assess_click(a: BrowserClickInput, ctx: ToolContext) -> Assessment:
    return Assessment(risk=Risk.MODIFY, title=f"Click {_label(a.ref)} on {_where()}", sends_external=True, grant_key="browser",
                      detail="Clicking can submit forms, send messages or change settings on the site.")


async def _locate(ctx: ToolContext, ref: int) -> tuple[Any, Any]:
    page = await _session.page(ctx)
    target = page.locator(f'[data-jarvis-ref="{ref}"]')
    if await target.count() == 0:
        raise ToolError("That element is no longer on the page. Read the page again with browser_read and use a current number.")
    return page, target.first


async def _run_click(a: BrowserClickInput, ctx: ToolContext) -> ToolResult:
    page, target = await _locate(ctx, a.ref)
    try:
        await target.click(timeout=10_000)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"The click did not work: {str(exc).splitlines()[0][:200]}") from exc
    return await _snapshot(page, 6000)


class BrowserTypeInput(ToolInput):
    ref: int = Field(description="The number of the text field, from the latest page reading.")
    text: str
    press_enter: bool = False


def _assess_type(a: BrowserTypeInput, ctx: ToolContext) -> Assessment:
    base = Assessment(risk=Risk.MODIFY, title=f"Type into {_label(a.ref)} on {_where()}", detail=a.text[:400], sends_external=True, grant_key="browser")
    element = _session.elements.get(a.ref)
    if element and "password" in element["kind"]:
        base.blocked = "Jarvis does not type into password fields. Ask the user to sign in themselves in the browser window."
    return base


async def _run_type(a: BrowserTypeInput, ctx: ToolContext) -> ToolResult:
    page, target = await _locate(ctx, a.ref)
    if (await target.get_attribute("type") or "").lower() == "password":
        raise ToolError("Jarvis does not type into password fields. Ask the user to sign in themselves in the browser window.")
    try:
        await target.fill(a.text, timeout=10_000)
        if a.press_enter:
            await target.press("Enter")
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"Typing did not work: {str(exc).splitlines()[0][:200]}") from exc
    return await _snapshot(page, 6000)


BROWSER_TOOLS: list[Tool] = [
    Tool(
        "browser_open",
        "Open a web address in Jarvis's own browser window and read the page. Use this (not open_url) when you need to "
        "read or operate a site yourself, including sites the user is signed in to such as webmail. If a site asks "
        "for a sign-in, tell the user to sign in themselves in that window; never ask for or type a password.",
        BrowserOpenInput, _run_open, _assess_open, timeout=200, group="browser",
    ),
    Tool("browser_read", "Read the page currently shown in Jarvis's browser: its text and the numbered things that can be clicked or typed into.", BrowserReadInput, _run_read, _assess_read, timeout=60, group="browser"),
    Tool("browser_click", "Click a numbered element on the page in Jarvis's browser, then read the resulting page.", BrowserClickInput, _run_click, _assess_click, timeout=60, group="browser"),
    Tool("browser_type", "Type text into a numbered field on the page in Jarvis's browser (never passwords).", BrowserTypeInput, _run_type, _assess_type, timeout=60, group="browser"),
]
