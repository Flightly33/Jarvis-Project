"""Command-line entry point: `jarvis chat`, `jarvis doctor`, `jarvis config`."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

from jarvis import __version__
from jarvis.brain.base import BackendUnavailable
from jarvis.config import ConfigError, load_config
from jarvis.paths import default_paths

_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def _dim(t: str) -> str:
    return _c("2", t)


def _bold(t: str) -> str:
    return _c("1", t)


def _warn(t: str) -> str:
    return _c("33", t)


def _bad(t: str) -> str:
    return _c("31", t)


def _good(t: str) -> str:
    return _c("32", t)


class _Lines:
    """Reads lines from the terminal on a helper thread, so replies and Ctrl+C stay live on every platform."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._loop = asyncio.get_running_loop()
        threading.Thread(target=self._pump, daemon=True, name="jarvis-stdin").start()

    def _pump(self) -> None:
        try:
            for line in sys.stdin:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, line.rstrip("\r\n"))
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
        except (RuntimeError, ValueError, OSError):
            pass        # the program is shutting down

    def end(self) -> None:
        self._queue.put_nowait(None)

    async def read(self, prompt: str) -> str | None:
        print(prompt, end="", flush=True)
        return await self._queue.get()


def _ensure_signed_in() -> None:
    """On first run, walk the user through signing in to their Claude account."""
    cli = _claude_cli()
    if cli is None:
        return
    try:
        status = subprocess.run([cli, "auth", "status", "--json"], capture_output=True, text=True, timeout=30, check=False)  # noqa: S603
        if json.loads(status.stdout or "{}").get("loggedIn"):
            return
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return
    print("First, sign in with your Claude account (the one with your plan - not an API key).\n")
    subprocess.call([cli, "auth", "login"])  # noqa: S603
    print()


async def _chat(args: argparse.Namespace) -> int:
    from jarvis.core import JarvisCore

    try:
        core = JarvisCore()
        if core.config.brain.backend == "claude":
            await asyncio.to_thread(_ensure_signed_in)
        await core.start()
    except (ConfigError, BackendUnavailable) as exc:
        print(_bad(str(exc)))
        return 1

    lines = _Lines()
    events = core.bus.subscribe()
    approval_waiting = asyncio.Event()

    async def ask_approval(request: dict) -> None:
        print(f"\n{_warn('  Approval needed')} {_dim('(risk: ' + request['risk'] + ')')}")
        print(f"  {_bold(request['title'])}")
        for line in (request["detail"] or "").splitlines()[:12]:
            print(_dim(f"    {line}"))
        for reason in request["reasons"]:
            print(f"  - {reason}")
        choices = "[y] yes, once"
        if request["allow_request_scope"]:
            choices += " / [r] yes, for the rest of this request"
        if request["allow_always"]:
            choices += " / [a] always allow this"
        approval_waiting.set()
        answer = (await lines.read(f"  {choices} / [N] no > ") or "").strip().lower()
        approval_waiting.clear()
        approved = answer in {"y", "yes", "r", "a"}
        scope = {"r": "request", "a": "always"}.get(answer, "once")
        core.services.approvals.resolve(request["id"], approved, scope, by="terminal")

    async def show_events() -> None:
        while True:
            event = await events.get()
            kind = event["type"]
            if kind == "tool_start":
                print(_dim(f"\n  > {event['title']}"), flush=True)
            elif kind == "tool_end":
                mark = _good("ok") if event["ok"] else _bad("x")
                print(_dim(f"    {mark} {event['summary']}"), flush=True)
            elif kind == "approval_request":
                await ask_approval(event["request"])
            elif kind == "notice":
                print(_warn(f"\n  [{event['level']}] {event['text']}"), flush=True)
            elif kind == "reminder":
                print(_bold(f"\n  Reminder: {event['text']}"), flush=True)

    def on_sigint() -> None:
        if core.assistant.busy or core.services.approvals.pending():
            print(_warn("\n  Stopping..."), flush=True)
            asyncio.ensure_future(core.emergency_stop("ctrl-c"))  # noqa: RUF006
        else:
            lines.end()         # nothing is running: treat Ctrl+C as "quit"

    loop = asyncio.get_running_loop()
    previous_handler = signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(on_sigint))
    shower = asyncio.create_task(show_events())

    print(f"{_bold('J.A.R.V.I.S.')} {__version__}  -  {core.backend.label}")
    print(_dim(f"Approved folders:\n{_indent(core.services.sandbox.describe())}"))
    print(_dim("Type a request. /new starts a fresh conversation, /quit exits, Ctrl+C stops the current request."))
    code = 0
    try:
        while True:
            text = await lines.read(f"\n{_bold('you>')} ")
            if text is None or text.strip() in {"/quit", "/exit"}:
                break
            text = text.strip()
            if not text:
                continue
            if text == "/new":
                await core.assistant.new_conversation()
                print(_dim("  (new conversation)"))
                continue
            print(f"\n{_bold('jarvis>')} ", end="", flush=True)
            result = await core.assistant.ask(text, "text", on_text=lambda d: print(d, end="", flush=True))
            print()
            if result.error:
                print(_bad(f"  {result.error}"))
            elif result.interrupted:
                print(_warn("  (stopped)"))
    except KeyboardInterrupt:
        pass
    finally:
        shower.cancel()
        signal.signal(signal.SIGINT, previous_handler)
        await core.close()
    return code


def _indent(text: str, by: str = "  ") -> str:
    return "\n".join(by + line for line in text.splitlines())


def _doctor(args: argparse.Namespace) -> int:
    paths = default_paths()
    ok = True

    def line(good: bool | None, label: str, detail: str = "") -> None:
        nonlocal ok
        mark = _good(" ok ") if good else _warn("note") if good is None else _bad("FAIL")
        if good is False:
            ok = False
        print(f"[{mark}] {label}" + (f"  {_dim(detail)}" if detail else ""))

    line(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}", "3.11 or newer is required")
    try:
        cfg = load_config(paths)
        line(True, "Settings", str(paths.config_file))
    except ConfigError as exc:
        line(False, "Settings", str(exc))
        return 1
    from jarvis.config import expand_dirs

    for folder in expand_dirs(cfg.safety.approved_dirs):
        line(True if folder.is_dir() else None, f"Approved folder {folder}", "" if folder.is_dir() else "will be created on first run")
    line(True, f"Reasoning backend: {cfg.brain.backend}", f"autonomy level: {cfg.safety.autonomy}")
    if cfg.brain.backend == "claude":
        have_sdk = importlib.util.find_spec("claude_agent_sdk") is not None
        line(have_sdk, "Claude Agent SDK", "" if have_sdk else 'pip install "jarvis-assistant[claude]"')
        cli = _claude_cli()
        signed_in, detail = None, "could not check sign-in status"
        if cli:
            try:
                status = subprocess.run([cli, "auth", "status", "--json"], capture_output=True, text=True, timeout=30, check=False)  # noqa: S603
                info = json.loads(status.stdout or "{}")
                signed_in = bool(info.get("loggedIn"))
                detail = (info.get("subscriptionType") or info.get("authMethod") or "") if signed_in else "run `jarvis login` to sign in with your Claude account"
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        line(signed_in, f"Claude sign-in (model '{cfg.claude.model}', {'subscription' if cfg.claude.auth == 'subscription' else 'API key'})", str(detail))
    for module, label in (("sherpa_onnx", "speech recognition (sherpa-onnx)"), ("piper", "speech synthesis (piper-tts)")):
        present = importlib.util.find_spec(module) is not None
        line(True if present else None, f"Voice: {label}", "" if present else 'optional: pip install "jarvis-assistant[voice]"')
    if sys.platform == "win32":
        line(bool(shutil.which("powershell.exe")), "PowerShell", "used to run commands")
    else:
        for prog, why in (("xdg-open", "opening files and web pages"), ("notify-send", "desktop notifications")):
            line(True if shutil.which(prog) else None, f"Desktop helper: {prog}", "" if shutil.which(prog) else f"not found - needed for {why}")
        shots = [p for p in ("grim", "gnome-screenshot", "spectacle", "scrot", "import") if shutil.which(p)]
        line(True if shots else None, "Screenshot program", ", ".join(shots) or "none found (install grim, gnome-screenshot, spectacle or scrot)")
    print()
    print(_good("Ready.") if ok else _bad("Some checks failed."))
    return 0 if ok else 1


def _claude_cli() -> str | None:
    """The Claude Code program: the copy bundled with the Agent SDK, else one on PATH."""
    try:
        import claude_agent_sdk

        folder = Path(claude_agent_sdk.__file__).parent / "_bundled"
        for name in ("claude.exe", "claude"):
            if (folder / name).exists():
                return str(folder / name)
    except ImportError:
        pass
    return shutil.which("claude")


def _login(args: argparse.Namespace) -> int:
    cli = _claude_cli()
    if cli is None:
        print(_bad('Claude Code was not found. Install it with: pip install "jarvis-assistant[claude]"'))
        return 1
    print("Opening Claude sign-in. Choose your Claude account (the one with your Pro plan), not an API key.\n")
    return subprocess.call([cli, "auth", "login"])  # noqa: S603 - fixed arguments, program shipped with the SDK


def _config(args: argparse.Namespace) -> int:
    paths = default_paths()
    paths.ensure()
    try:
        load_config(paths)
    except ConfigError as exc:
        print(_bad(str(exc)))
        return 1
    if args.action == "path":
        print(paths.config_file)
    else:
        print(paths.config_file.read_text(encoding="utf-8"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="J.A.R.V.I.S. - your personal assistant.")
    parser.add_argument("--version", action="version", version=f"jarvis {__version__}")
    parser.add_argument("--debug", action="store_true", help="print diagnostic logging")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("chat", help="talk to Jarvis in this terminal")
    sub.add_parser("doctor", help="check that everything Jarvis needs is in place")
    sub.add_parser("login", help="sign in to your Claude account (needed once)")
    cfg = sub.add_parser("config", help="show the settings file or where it lives")
    cfg.add_argument("action", choices=["path", "show"], nargs="?", default="path")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if args.command == "doctor":
        return _doctor(args)
    if args.command == "login":
        return _login(args)
    if args.command == "config":
        return _config(args)
    if args.command in (None, "chat"):
        try:
            code = asyncio.run(_chat(args))
        except KeyboardInterrupt:
            code = 130
        if code and getattr(sys, "frozen", False):
            input("\nPress Enter to close this window.")     # keep the error readable when started by double-click
        return code
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
