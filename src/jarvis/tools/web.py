"""Web search and page fetching for reasoning backends that have no web tools of their own.

(The Claude backend uses Claude Code's built-in search and fetch instead.)
Everything these tools return is marked as outside content, and requests are
refused if they would reach this computer or the local network.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import Field

from jarvis.safety import Assessment
from jarvis.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult, Trimmed

_MAX_BYTES = 2_000_000
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml", "application/rss+xml", "application/atom+xml")
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head", "iframe"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "blockquote", "pre", "table", "ul", "ol"}
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) JarvisAssistant/0.1 (personal assistant; fetches pages at its user's request)"


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def ensure_public(url: str) -> None:
    """Refuse addresses that point at this machine or a private network."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ToolError("Only http and https addresses can be fetched.")
    host = parts.hostname
    try:
        addresses = [host] if _is_ip(host) else await _resolve(host, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError as exc:
        raise ToolError(f"Could not look up {host}: {exc}") from exc
    for addr in addresses:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ToolError(f"{host} is on this computer or a private network; fetching it is not allowed.")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS and tag != "head":
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and tag != "head":
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """Return (title, readable text) from an HTML page."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - malformed markup should still yield whatever text was found
        pass
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return " ".join(parser.title.split()), text


async def fetch_page(url: str, *, timeout: float = 20.0, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    current = url
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout, transport=transport, headers={"User-Agent": _USER_AGENT}) as client:
        for _ in range(6):
            await ensure_public(current)       # checked again at every redirect
            async with client.stream("GET", current) as response:
                if response.is_redirect and response.headers.get("location"):
                    current = urljoin(current, response.headers["location"])
                    continue
                if response.status_code >= 400:
                    raise ToolError(f"The page returned HTTP {response.status_code}.")
                ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype and not ctype.startswith(_TEXT_TYPES):
                    raise ToolError(f"That address serves '{ctype}', which cannot be read as text.")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_BYTES:
                        break
                text = bytes(body[:_MAX_BYTES]).decode(response.encoding or "utf-8", errors="replace")
                title = ""
                if "html" in ctype or (not ctype and "<html" in text[:2000].lower()):
                    title, text = html_to_text(text)
                return {"url": current, "title": title, "text": text, "truncated": len(body) > _MAX_BYTES}
    raise ToolError("Too many redirects.")


class WebFetchInput(ToolInput):
    url: Trimmed = Field(description="The http(s) address to read.")
    max_chars: int = Field(default=12_000, ge=500, le=40_000)


def _assess_fetch(a: WebFetchInput, ctx: ToolContext) -> Assessment:
    return Assessment(title=f"Read {a.url[:120]}", ingests_untrusted=True, sends_external=True, grant_key="web")


async def _run_fetch(a: WebFetchInput, ctx: ToolContext) -> ToolResult:
    try:
        page = await fetch_page(a.url)
    except httpx.HTTPError as exc:
        raise ToolError(f"Could not fetch the page: {exc}") from exc
    text = page["text"]
    shown = text[: a.max_chars]
    body = f"Title: {page['title'] or '(none)'}\nAddress: {page['url']}\n\n{shown}"
    if len(text) > a.max_chars or page["truncated"]:
        body += "\n\n[page truncated]"
    return ToolResult(body, f"read {urlsplit(page['url']).netloc} ({len(shown)} characters)", untrusted_source=page["url"])


class WebSearchInput(ToolInput):
    query: str
    max_results: int = Field(default=6, ge=1, le=15)


def _assess_search(a: WebSearchInput, ctx: ToolContext) -> Assessment:
    return Assessment(title=f"Search the web for '{a.query[:80]}'", ingests_untrusted=True, sends_external=True, grant_key="web")


def _ddgs_search(query: str, max_results: int) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise ToolError(
            "Web search needs the optional 'ddgs' package (pip install \"jarvis-assistant[search]\"), "
            "or switch to the Claude backend, which has web search built in."
        ) from exc
    with DDGS() as engine:
        return [{"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")} for r in engine.text(query, max_results=max_results)]


async def _run_search(a: WebSearchInput, ctx: ToolContext) -> ToolResult:
    try:
        results = await asyncio.to_thread(_ddgs_search, a.query, a.max_results)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - the search service is outside our control
        raise ToolError(f"The web search failed: {exc}") from exc
    return ToolResult({"query": a.query, "results": results}, f"{len(results)} result(s)", untrusted_source="web search")


WEB_TOOLS: list[Tool] = [
    Tool(
        "web_search",
        "Search the web. Returns titles, addresses and snippets; follow up with web_fetch to read a result. "
        "Use it for anything recent or that you are not sure of, and cite the addresses you relied on.",
        WebSearchInput, _run_search, _assess_search, timeout=40, group="web",
    ),
    Tool("web_fetch", "Read the text of a web page.", WebFetchInput, _run_fetch, _assess_fetch, timeout=40, group="web"),
]
