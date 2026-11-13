"""Opening and closing applications, files and web pages on Windows.

Applications come from the Start menu's own list (`Get-StartApps`), which
covers classic programs and Microsoft Store apps alike, and are started the
way the Start menu starts them.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import psutil

from jarvis.safety import Access, Assessment, Risk
from jarvis.tools.apps import (
    CloseApplicationInput,
    ListApplicationsInput,
    OpenApplicationInput,
    OpenPathInput,
    OpenUrlInput,
    ScreenshotInput,
    _assess_open_app,
    _assess_open_path,
    _assess_open_url,
    _assess_screenshot,
    _check_target,
    _shrink,
)
from jarvis.tools.base import ImagePart, Tool, ToolContext, ToolError, ToolResult

_NEVER_CLOSE = {
    "explorer", "csrss", "winlogon", "services", "lsass", "svchost", "dwm", "system", "smss", "wininit", "python",
    "pythonw", "jarvis", "powershell", "pwsh", "cmd", "conhost", "windowsterminal", "openconsole", "claude", "sihost",
    "fontdrvhost", "registry", "taskhostw", "ctfmon", "searchhost", "startmenuexperiencehost", "shellexperiencehost",
}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_cache: tuple[float, list[dict]] = (0.0, [])


def start_apps(max_age: float = 60.0) -> list[dict]:
    """[{name, app_id}] for everything in the Start menu."""
    global _cache
    if time.monotonic() - _cache[0] < max_age and _cache[1]:
        return _cache[1]
    script = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"
    try:
        done = subprocess.run(  # noqa: S603 - fixed command
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=30, check=False, creationflags=_NO_WINDOW,
        )
        rows = json.loads(done.stdout.decode("utf-8", errors="replace") or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise ToolError(f"Could not read the Start menu's application list: {exc}") from exc
    if isinstance(rows, dict):
        rows = [rows]
    apps = sorted(
        ({"name": str(r.get("Name", "")), "app_id": str(r.get("AppID", ""))} for r in rows if r.get("Name") and r.get("AppID")),
        key=lambda a: a["name"].lower(),
    )
    _cache = (time.monotonic(), apps)
    return apps


def match_apps(query: str, apps: list[dict]) -> list[dict]:
    q = " ".join(query.lower().split())
    if not q:
        return []
    scored = []
    for app in apps:
        name = app["name"].lower()
        if q == name:
            score = 100.0
        elif name.startswith(q):
            score = 80.0
        elif q in name:
            score = 70.0
        else:
            ratio = difflib.SequenceMatcher(None, q, name).ratio()
            score = ratio * 60 if ratio >= 0.72 else 0.0
        if score:
            scored.append((score - len(name) * 0.01, app))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [a for _, a in scored[:5]]


def match_processes(query: str) -> list[psutil.Process]:
    """Running programs whose name fits what the user called the application."""
    squeezed = "".join(query.lower().split())
    own = {os.getpid(), os.getppid()}
    found = []
    for proc in psutil.process_iter(["name"]):
        stem = (proc.info["name"] or "").lower().removesuffix(".exe")
        if not stem or proc.pid in own:
            continue
        if squeezed == stem or (len(squeezed) >= 3 and squeezed in stem) or (len(stem) >= 4 and stem in squeezed):
            found.append(proc)
    return found


async def _run_list_apps(a: ListApplicationsInput, ctx: ToolContext) -> ToolResult:
    apps = await asyncio.to_thread(start_apps)
    if a.query:
        apps = match_apps(a.query, apps)
    rows = []
    for app in apps:
        running = bool(match_processes(app["name"])) if (a.running_only or a.query) else None
        if a.running_only and not running:
            continue
        rows.append({"name": app["name"]} | ({"running": running} if running is not None else {}))
    return ToolResult({"applications": rows[:120], "count": len(rows)}, f"{len(rows)} application(s)")


async def _run_open_app(a: OpenApplicationInput, ctx: ToolContext) -> ToolResult:
    matches = match_apps(a.name, await asyncio.to_thread(start_apps))
    if not matches:
        raise ToolError(f"No installed application matches '{a.name}'. Use list_applications to see what is installed.")
    app = matches[0]
    target, _ = _check_target(ctx, a.target, Assessment())
    try:
        if target:
            # hand the file or address to the application the way "Open with" would
            os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
        else:
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app['app_id']}"])  # noqa: S603, S607
    except OSError as exc:
        raise ToolError(f"Could not start {app['name']}: {exc}") from exc
    await asyncio.sleep(1.5)
    running = bool(match_processes(app["name"]))
    result = {"application": app["name"], "started": True, "verified_running": running}
    if target:
        result["note"] = "The target was opened with its default application."
    if len(matches) > 1:
        result["other_matches"] = [m["name"] for m in matches[1:]]
    return ToolResult(result, f"opened {app['name']}" + ("" if running else " (could not confirm it is running)"))


def _assess_close_app(a: CloseApplicationInput, ctx: ToolContext) -> Assessment:
    if not match_processes(a.name):
        return Assessment(title=f"Close {a.name}")
    if a.force:
        return Assessment(risk=Risk.HIGH, title=f"Force-close {a.name}", reasons=["Force-closing discards unsaved work."])
    return Assessment(risk=Risk.MODIFY, title=f"Close {a.name}", detail="The application is asked to quit; unsaved work may be lost.", grant_key="apps:close")


async def _run_close_app(a: CloseApplicationInput, ctx: ToolContext) -> ToolResult:
    procs = match_processes(a.name)
    if not procs:
        return ToolResult({"application": a.name, "was_running": False}, f"{a.name} was not running")
    names = {(p.info["name"] or "").lower().removesuffix(".exe") for p in procs}
    if names & _NEVER_CLOSE:
        raise ToolError(f"{a.name} matches a program Windows itself depends on ({', '.join(sorted(names & _NEVER_CLOSE))}); it will not be closed.")
    for name in names:
        # without /F, taskkill asks the application to close, as clicking its X would
        argv = ["taskkill", "/IM", f"{name}.exe", "/T"] + (["/F"] if a.force else [])
        await asyncio.to_thread(subprocess.run, argv, capture_output=True, timeout=15, check=False, creationflags=_NO_WINDOW)
    _, alive = await asyncio.to_thread(psutil.wait_procs, procs, 5)
    closed = not alive
    result = {"application": a.name, "was_running": True, "closed": closed, "processes": len(procs)}
    if not closed:
        result["note"] = "It did not quit - it may be asking to save work. Use force=true only if the user agrees to lose unsaved changes."
    return ToolResult(result, f"{a.name} {'closed' if closed else 'did not close'}", is_error=not closed)


async def _run_open_path(a: OpenPathInput, ctx: ToolContext) -> ToolResult:
    path = ctx.sandbox.resolve(a.path, Access.READ)
    if not path.exists():
        raise ToolError(f"{path} does not exist.")
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
    except OSError as exc:
        raise ToolError(f"Could not open {path}: {exc}") from exc
    return ToolResult({"opened": str(path)}, f"opened {path.name}")


async def _run_open_url(a: OpenUrlInput, ctx: ToolContext) -> ToolResult:
    try:
        os.startfile(a.url)  # type: ignore[attr-defined]  # noqa: S606
    except OSError as exc:
        raise ToolError(f"Could not open the address: {exc}") from exc
    return ToolResult({"opened": a.url}, f"opened {urlsplit(a.url).netloc}")


def _grab(path: Path) -> None:
    from PIL import ImageGrab

    ImageGrab.grab(all_screens=True).save(path)


async def _run_screenshot(a: ScreenshotInput, ctx: ToolContext) -> ToolResult:
    shots = ctx.services.paths.screenshots_dir
    shots.mkdir(parents=True, exist_ok=True)
    path = shots / f"screen-{time.strftime('%Y%m%d-%H%M%S')}.png"
    try:
        await asyncio.to_thread(_grab, path)
    except Exception as exc:  # noqa: BLE001 - no desktop session, locked screen...
        raise ToolError(f"Could not take a screenshot: {exc}") from exc
    data, mime, size = await asyncio.to_thread(_shrink, path)
    from jarvis.tools import desktop_control

    desktop_control.view = desktop_control.View(*desktop_control.screen_geometry(), size[0], size[1])     # so clicks can be placed from this picture
    return ToolResult({"saved_to": str(path), "width": size[0], "height": size[1], "note": "Positions for screen_click are pixels in this picture."},
                      "screenshot taken", images=[ImagePart(mime, data)])


APP_TOOLS: list[Tool] = [
    Tool("list_applications", "List applications in the Start menu, optionally filtered by name.", ListApplicationsInput, _run_list_apps, timeout=45, group="desktop"),
    Tool(
        "open_application",
        "Start an application on the user's desktop by name. If a file, folder or web address is given as target, it is opened with its default application instead.",
        OpenApplicationInput, _run_open_app, _assess_open_app, timeout=45, group="desktop",
    ),
    Tool("close_application", "Ask a running application to quit.", CloseApplicationInput, _run_close_app, _assess_close_app, timeout=45, group="desktop"),
    Tool("open_path", "Open a file or folder with the user's default application (as if double-clicked).", OpenPathInput, _run_open_path, _assess_open_path, timeout=30, group="desktop"),
    Tool("open_url", "Open a web address in the user's default browser.", OpenUrlInput, _run_open_url, _assess_open_url, timeout=30, group="desktop"),
    Tool(
        "take_screenshot",
        "Capture the user's screen and look at it. Use it to see what an application is showing or to check that an action worked.",
        ScreenshotInput, _run_screenshot, _assess_screenshot, timeout=45, group="desktop",
    ),
]
