"""Opening and closing applications, files and web pages on a Linux desktop.

Applications are discovered from the standard `.desktop` launcher files, the
same source the desktop's own application menu uses, so "open Firefox" finds
whatever the user actually has installed (including Flatpak and Snap apps).
"""

from __future__ import annotations

import asyncio
import difflib
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import psutil
from pydantic import Field

from jarvis.safety import Access, Assessment, Risk
from jarvis.tools.base import ImagePart, Tool, ToolContext, ToolError, ToolInput, ToolResult, Trimmed
from jarvis.tools.shell import command_env

_FIELD_CODES = {"%f", "%F", "%u", "%U", "%i", "%c", "%k", "%d", "%D", "%n", "%N", "%v", "%m"}
_TERMINALS = ("x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal", "kitty", "alacritty", "foot", "xterm")
# Processes that hold the desktop session together; closing them would log the user out or worse.
_NEVER_CLOSE = {
    "systemd", "init", "gnome-shell", "plasmashell", "kwin_x11", "kwin_wayland", "xorg", "xwayland", "sway", "hyprland",
    "gdm", "gdm3", "sddm", "lightdm", "dbus-daemon", "dbus-broker", "pipewire", "wireplumber", "pulseaudio", "login",
    "sshd", "bash", "zsh", "fish", "sh", "python", "python3", "jarvis", "mutter", "gnome-session-binary", "ksmserver",
}


@dataclass(frozen=True)
class DesktopApp:
    id: str                 # e.g. "firefox.desktop"
    name: str
    generic_name: str
    exec_line: str
    keywords: str
    terminal: bool
    path: str

    @property
    def binary(self) -> str:
        try:
            parts = [p for p in shlex.split(self.exec_line) if p not in _FIELD_CODES]
        except ValueError:
            return ""
        # skip `env VAR=value` style prefixes to find the real program
        while parts and (parts[0] == "env" or "=" in parts[0]):
            parts.pop(0)
        return Path(parts[0]).name if parts else ""

    def argv(self, target: str = "") -> list[str]:
        out: list[str] = []
        used_target = False
        for part in shlex.split(self.exec_line):
            if part in {"%f", "%F", "%u", "%U"}:
                if target:
                    out.append(target)
                    used_target = True
            elif part in _FIELD_CODES:
                continue
            else:
                out.append(part.replace("%%", "%"))
        if target and not used_target:
            out.append(target)
        return out

    def public(self) -> dict:
        return {"name": self.name, "description": self.generic_name, "id": self.id}


def _application_dirs() -> list[Path]:
    home = Path.home()
    data_home = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    data_dirs = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    dirs = [data_home / "applications", *(Path(d) / "applications" for d in data_dirs if d)]
    dirs += [
        home / ".local/share/flatpak/exports/share/applications",
        Path("/var/lib/flatpak/exports/share/applications"),
        Path("/var/lib/snapd/desktop/applications"),
    ]
    seen: list[Path] = []
    for d in dirs:
        if d not in seen:
            seen.append(d)
    return seen


def _parse_desktop_file(path: Path) -> DesktopApp | None:
    fields: dict[str, str] = {}
    section = ""
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("["):
                section = line
            elif section == "[Desktop Entry]" and "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                fields.setdefault(key.strip(), value.strip())
    except OSError:
        return None
    if fields.get("Type", "Application") != "Application" or not fields.get("Exec") or not fields.get("Name"):
        return None
    if fields.get("NoDisplay", "").lower() == "true" or fields.get("Hidden", "").lower() == "true":
        return None
    return DesktopApp(
        id=path.name, name=fields["Name"], generic_name=fields.get("GenericName", ""), exec_line=fields["Exec"],
        keywords=fields.get("Keywords", ""), terminal=fields.get("Terminal", "").lower() == "true", path=str(path),
    )


_cache: tuple[float, list[DesktopApp]] = (0.0, [])


def installed_apps(max_age: float = 30.0) -> list[DesktopApp]:
    global _cache
    if time.monotonic() - _cache[0] < max_age and _cache[1]:
        return _cache[1]
    apps: dict[str, DesktopApp] = {}
    for directory in _application_dirs():
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("**/*.desktop")):
            if path.name not in apps:           # earlier folders (the user's own) take precedence
                app = _parse_desktop_file(path)
                if app:
                    apps[path.name] = app
    _cache = (time.monotonic(), sorted(apps.values(), key=lambda a: a.name.lower()))
    return _cache[1]


def find_app(query: str, apps: list[DesktopApp] | None = None) -> list[DesktopApp]:
    """Best matches for a spoken or typed application name, best first."""
    q = " ".join(query.lower().split())
    if not q:
        return []
    scored: list[tuple[float, DesktopApp]] = []
    for app in apps if apps is not None else installed_apps():
        name, binary, stem = app.name.lower(), app.binary.lower(), app.id.lower().removesuffix(".desktop")
        if q in (name, binary, stem, stem.rsplit(".", 1)[-1]):
            score = 100.0
        elif name.startswith(q) or binary.startswith(q):
            score = 80.0
        elif q in name:
            score = 70.0
        elif q in app.generic_name.lower() or q in app.keywords.lower() or q in stem:
            score = 50.0
        else:
            ratio = max(difflib.SequenceMatcher(None, q, name).ratio(), difflib.SequenceMatcher(None, q, binary).ratio())
            score = ratio * 60 if ratio >= 0.72 else 0.0     # tolerate small transcription slips
        if score:
            scored.append((score - len(name) * 0.01, app))    # prefer the shorter, more exact name
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [app for _, app in scored[:5]]


def _detached(argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603 - argv comes from an installed launcher file, never from a shell string
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env=command_env(),
    )


def _processes_for(binary: str) -> list[psutil.Process]:
    me = os.getuid()
    found = []
    for proc in psutil.process_iter(["name", "exe", "uids"]):
        try:
            if proc.info["uids"] and proc.info["uids"].real != me:
                continue
            name = (proc.info["name"] or "").lower()
            exe = Path(proc.info["exe"] or "").name.lower()
            if binary.lower() in (name, exe) or name == binary.lower()[:15]:   # the kernel truncates names to 15 chars
                found.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


# --------------------------------------------------------------- applications


class ListApplicationsInput(ToolInput):
    query: str = Field(default="", description="Filter by name. Empty lists everything installed.")
    running_only: bool = False


async def _run_list_apps(a: ListApplicationsInput, ctx: ToolContext) -> ToolResult:
    apps = find_app(a.query) if a.query else installed_apps()
    rows = []
    for app in apps:
        running = bool(app.binary and _processes_for(app.binary))
        if a.running_only and not running:
            continue
        rows.append(app.public() | {"running": running})
    return ToolResult({"applications": rows[:80], "count": len(rows)}, f"{len(rows)} application(s)")


class OpenApplicationInput(ToolInput):
    name: str = Field(description="Application name as the user would say it, e.g. 'Firefox' or 'files'.")
    target: str = Field(default="", description="Optional file, folder or http(s) address for the application to open.")


def _check_target(ctx: ToolContext, target: str, base: Assessment) -> tuple[str, Assessment]:
    """Validate an optional file or web address handed to an application."""
    if not target:
        return "", base
    if target.startswith(("http://", "https://")):
        base.sends_external = True
        return target, base
    check = ctx.sandbox.check(target, Access.READ)
    if not check.allowed:
        base.blocked = check.reason
    elif check.sensitive:
        base.risk = Risk.HIGH
        base.reasons.append(f"{check.path} looks like a credentials or other sensitive file.")
    return str(check.path), base


def _assess_open_app(a: OpenApplicationInput, ctx: ToolContext) -> Assessment:
    base = Assessment(risk=Risk.LOW, title=f"Open {a.name}" + (f" with {a.target}" if a.target else ""))
    return _check_target(ctx, a.target, base)[1]


async def _run_open_app(a: OpenApplicationInput, ctx: ToolContext) -> ToolResult:
    matches = find_app(a.name)
    if not matches:
        raise ToolError(f"No installed application matches '{a.name}'. Use list_applications to see what is installed.")
    app = matches[0]
    target, _ = _check_target(ctx, a.target, Assessment())
    argv = app.argv(target)
    if app.terminal:
        terminal = next((t for t in _TERMINALS if shutil.which(t)), None)
        if terminal is None:
            raise ToolError(f"{app.name} needs a terminal window, and no terminal program was found.")
        argv = [terminal, "--" if terminal == "gnome-terminal" else "-e", *argv]
    if not shutil.which(argv[0]):
        raise ToolError(f"{app.name} is listed in the menu, but its program '{argv[0]}' is not installed or not on PATH.")
    try:
        proc = _detached(argv)
    except OSError as exc:
        raise ToolError(f"Could not start {app.name}: {exc}") from exc
    await asyncio.sleep(1.0)
    code = proc.poll()
    running = bool(_processes_for(app.binary)) if app.binary else code is None
    if code not in (None, 0) and not running:
        raise ToolError(f"{app.name} exited immediately with code {code}; it did not open.")
    result = {"application": app.name, "started": True, "verified_running": running}
    if len(matches) > 1:
        result["other_matches"] = [m.name for m in matches[1:]]
    return ToolResult(result, f"opened {app.name}" + ("" if running else " (could not confirm it is running)"))


class CloseApplicationInput(ToolInput):
    name: str
    force: bool = Field(default=False, description="Kill it if it does not close when asked. Unsaved work is lost.")


def _closing_target(name: str) -> tuple[str, str]:
    """(display label, process name) for an application the user wants closed."""
    matches = find_app(name)
    if matches:
        return matches[0].name, matches[0].binary
    return name, (name.strip().split() or [""])[0]


def _assess_close_app(a: CloseApplicationInput, ctx: ToolContext) -> Assessment:
    _, binary = _closing_target(a.name)
    if not binary or not _processes_for(binary):
        return Assessment(title=f"Close {a.name}")      # nothing is running, so there is nothing to confirm
    if a.force:
        return Assessment(risk=Risk.HIGH, title=f"Force-close {a.name}", reasons=["Force-closing discards unsaved work."])
    return Assessment(risk=Risk.MODIFY, title=f"Close {a.name}", detail="The application is asked to quit; unsaved work may be lost.", grant_key="apps:close")


async def _run_close_app(a: CloseApplicationInput, ctx: ToolContext) -> ToolResult:
    label, binary = _closing_target(a.name)
    if binary.lower() in _NEVER_CLOSE:
        raise ToolError(f"{label} is part of the desktop session itself and will not be closed.")
    own = {os.getpid(), os.getppid()}
    procs = [p for p in _processes_for(binary) if p.pid not in own]
    if not procs:
        return ToolResult({"application": label, "was_running": False}, f"{label} was not running")
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = await asyncio.to_thread(psutil.wait_procs, procs, 4)
    if alive and a.force:
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass
        _, alive = await asyncio.to_thread(psutil.wait_procs, alive, 3)
    closed = not alive
    result = {"application": label, "was_running": True, "closed": closed, "processes": len(procs)}
    if not closed:
        result["note"] = "It did not quit - it may be asking to save work. Use force=true only if the user agrees to lose unsaved changes."
    return ToolResult(result, f"{label} {'closed' if closed else 'did not close'}", is_error=not closed)


# ------------------------------------------------------------- paths and URLs


class OpenPathInput(ToolInput):
    path: str = Field(description="File or folder to open with the user's default application.")


def _assess_open_path(a: OpenPathInput, ctx: ToolContext) -> Assessment:
    return _check_target(ctx, a.path, Assessment(risk=Risk.LOW, title=f"Open {a.path}"))[1]


def _xdg_open(target: str) -> None:
    opener = shutil.which("xdg-open") or shutil.which("gio")
    if opener is None:
        raise ToolError("No desktop opener (xdg-open) is available on this system.")
    argv = [opener, target] if opener.endswith("xdg-open") else [opener, "open", target]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=20, env=command_env(), check=False)  # noqa: S603
    if result.returncode != 0:
        raise ToolError(f"Could not open {target}: {result.stderr.strip() or f'exit code {result.returncode}'}")


async def _run_open_path(a: OpenPathInput, ctx: ToolContext) -> ToolResult:
    path = ctx.sandbox.resolve(a.path, Access.READ)
    if not path.exists():
        raise ToolError(f"{path} does not exist.")
    await asyncio.to_thread(_xdg_open, str(path))
    return ToolResult({"opened": str(path)}, f"opened {path.name}")


class OpenUrlInput(ToolInput):
    url: Trimmed = Field(description="An http or https address. To search in the browser, build a search-engine address with the query.")


def _assess_open_url(a: OpenUrlInput, ctx: ToolContext) -> Assessment:
    parts = urlsplit(a.url)
    base = Assessment(risk=Risk.LOW, title=f"Open {a.url[:120]} in the browser", sends_external=True, grant_key="apps:open_url")
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        base.blocked = "Only http and https addresses can be opened."
    return base


async def _run_open_url(a: OpenUrlInput, ctx: ToolContext) -> ToolResult:
    await asyncio.to_thread(_xdg_open, a.url)
    return ToolResult({"opened": a.url}, f"opened {urlsplit(a.url).netloc}")


# ----------------------------------------------------------------- screenshot

_SCREENSHOT_COMMANDS = (
    ("grim", ["grim", "{path}"]),
    ("gnome-screenshot", ["gnome-screenshot", "-f", "{path}"]),
    ("spectacle", ["spectacle", "-b", "-n", "-o", "{path}"]),
    ("scrot", ["scrot", "-o", "{path}"]),
    ("xfce4-screenshooter", ["xfce4-screenshooter", "-f", "-s", "{path}"]),
    ("import", ["import", "-window", "root", "{path}"]),
)


class ScreenshotInput(ToolInput):
    pass


def _assess_screenshot(a: ScreenshotInput, ctx: ToolContext) -> Assessment:
    return Assessment(title="Take a screenshot of the screen", reads_private=True)


def _capture(path: Path, custom: str) -> None:
    if custom:
        argv = [part.replace("{path}", str(path)) for part in shlex.split(custom)]
        candidates = [argv]
    else:
        candidates = [[p.replace("{path}", str(path)) for p in argv] for prog, argv in _SCREENSHOT_COMMANDS if shutil.which(prog)]
    if not candidates:
        raise ToolError("No screenshot program found. Install one of: grim (Wayland), gnome-screenshot, spectacle, scrot.")
    errors = []
    for argv in candidates:
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=20, env=command_env(), check=False)  # noqa: S603
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{argv[0]}: {exc}")
            continue
        if done.returncode == 0 and path.exists() and path.stat().st_size > 0:
            return
        errors.append(f"{argv[0]}: {done.stderr.strip() or f'exit code {done.returncode}'}")
    raise ToolError("Could not take a screenshot. " + " | ".join(errors))


def _shrink(path: Path, max_edge: int = 1568) -> tuple[bytes, str, tuple[int, int]]:
    """Scale the capture down so it is quick to send and cheap for the model to read."""
    from PIL import Image

    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((max_edge, max_edge))
        out = path.with_suffix(".jpg")
        img.save(out, "JPEG", quality=80)
        return out.read_bytes(), "image/jpeg", img.size


async def _run_screenshot(a: ScreenshotInput, ctx: ToolContext) -> ToolResult:
    shots = ctx.services.paths.screenshots_dir
    shots.mkdir(parents=True, exist_ok=True)
    path = shots / f"screen-{time.strftime('%Y%m%d-%H%M%S')}.png"
    await asyncio.to_thread(_capture, path, ctx.config.desktop.screenshot_command)
    data, mime, size = await asyncio.to_thread(_shrink, path)
    return ToolResult(
        {"saved_to": str(path), "width": size[0], "height": size[1]},
        "screenshot taken", images=[ImagePart(mime, data)],
    )


APP_TOOLS: list[Tool] = [
    Tool("list_applications", "List installed applications, optionally filtered by name, and whether each is running.", ListApplicationsInput, _run_list_apps, group="desktop"),
    Tool(
        "open_application",
        "Start an application on the user's desktop by name, optionally handing it a file, folder or web address.",
        OpenApplicationInput, _run_open_app, _assess_open_app, timeout=30, group="desktop",
    ),
    Tool("close_application", "Ask a running application to quit.", CloseApplicationInput, _run_close_app, _assess_close_app, timeout=30, group="desktop"),
    Tool("open_path", "Open a file or folder with the user's default application (as if double-clicked).", OpenPathInput, _run_open_path, _assess_open_path, timeout=30, group="desktop"),
    Tool("open_url", "Open a web address in the user's default browser.", OpenUrlInput, _run_open_url, _assess_open_url, timeout=30, group="desktop"),
    Tool(
        "take_screenshot",
        "Capture the user's screen and look at it. Use it to see what an application is showing or to check that an action worked.",
        ScreenshotInput, _run_screenshot, _assess_screenshot, timeout=45, group="desktop",
    ),
]
