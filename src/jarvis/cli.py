"""Command-line entry point: `jarvis chat`, `jarvis doctor`, `jarvis config`."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import webbrowser
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


def _expired(value: object) -> bool:
    """Whether a sign-in expiry (a timestamp in seconds or milliseconds, or an ISO date) has passed."""
    import datetime as _dt
    import time as _time

    try:
        if isinstance(value, (int, float)):
            return (value / 1000 if value > 1e11 else value) < _time.time()
        if isinstance(value, str) and value:
            if value.replace(".", "", 1).isdigit():
                return _expired(float(value))
            when = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=_dt.UTC)
            return when.timestamp() < _time.time()
    except (ValueError, OverflowError):
        pass
    return False


def _ensure_signed_in() -> None:
    """On first run, walk the user through signing in to their Claude account."""
    cli = _claude_cli()
    if cli is None:
        return
    try:
        status = subprocess.run([cli, "auth", "status", "--json"], capture_output=True, text=True, timeout=30, check=False)  # noqa: S603
        info = json.loads(status.stdout or "{}")
        if info.get("loggedIn") and not _expired(info.get("loginExpiresAt")):
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


def _free_port(host: str, preferred: int) -> int:
    """The preferred port if it is free, otherwise the next free one."""
    for candidate in [*range(preferred, preferred + 20), 0]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host if host != "localhost" else "127.0.0.1", candidate))
                return probe.getsockname()[1]
            except OSError:
                continue
    raise OSError("no free port found")


def _app_browser() -> str | None:
    """A Chromium-family browser that can show the interface in its own window, if one is installed."""
    if sys.platform == "win32":
        roots = [os.environ.get(v) for v in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        tails = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"]
        for tail in tails:
            for root in roots:
                if root and (Path(root) / tail).exists():
                    return str(Path(root) / tail)
        return None
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge", "brave-browser"):
        if shutil.which(name):
            return shutil.which(name)
    return None


def open_interface(url: str, app_window: bool = True) -> None:
    browser = _app_browser() if app_window else None
    try:
        if browser:
            subprocess.Popen(  # noqa: S603 - a browser found in its standard install location
                [browser, f"--app={url}", "--window-size=1320,860"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return
    except OSError:
        pass
    webbrowser.open(url)


async def _run(args: argparse.Namespace) -> int:
    import uvicorn

    from jarvis.core import JarvisCore
    from jarvis.server import create_app

    try:
        core = JarvisCore()
        if core.config.brain.backend == "claude":
            await asyncio.to_thread(_ensure_signed_in)
    except (ConfigError, BackendUnavailable) as exc:
        print(_bad(str(exc)))
        return 1
    voice = None
    if core.config.voice.enabled and not args.no_voice:
        try:
            from jarvis.voice import VoiceController

            voice = VoiceController(core)
        except ImportError as exc:
            print(_dim(f"Voice is off: {exc}"))

    host = core.config.server.host
    port = _free_port(host, args.port or core.config.server.port)
    token = secrets.token_urlsafe(24)
    app = create_app(core, voice, token=token, port=port)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False))
    url = f"http://127.0.0.1:{port}/?token={token}"
    print(f"{_bold('J.A.R.V.I.S.')} {__version__}  -  {core.backend.label}")
    print(f"Interface: {url}")
    print(_dim("Keep this window open while you use Jarvis. Close it (or press Ctrl+C) to quit."), flush=True)
    if core.config.server.open_browser and not args.no_window:
        asyncio.get_running_loop().call_later(1.0, open_interface, url, core.config.server.app_window)
    try:
        await server.serve()
    except BackendUnavailable as exc:
        print(_bad(str(exc)))
        return 1
    return 0


async def _selftest(args: argparse.Namespace) -> int:
    """Check that this installation is complete, without using any AI model. Used by the build pipeline."""
    import tempfile

    import httpx
    import uvicorn

    failures = 0

    def check(ok: bool, label: str, detail: str = "", required: bool = True) -> None:
        nonlocal failures
        if not ok and required:
            failures += 1
        mark = _good(" ok ") if ok else _bad("FAIL") if required else _warn("miss")
        print(f"[{mark}] {label}" + (f"  {detail}" if detail else ""), flush=True)

    from jarvis.server.app import STATIC

    for name in ("index.html", "app.js", "styles.css"):
        check((STATIC / name).is_file(), f"interface file {name}")
    check(_claude_cli() is not None, "Claude Code program", _claude_cli() or "not found")
    voice_ok = True
    for module in ("numpy", "sherpa_onnx", "sentencepiece", "piper", "sounddevice"):
        try:
            __import__(module)
            check(True, f"voice component {module}")
        except Exception as exc:  # noqa: BLE001 - any import failure is what we are looking for
            voice_ok = False
            check(False, f"voice component {module}", f"{type(exc).__name__}: {exc}", args.require_voice)
    if voice_ok and os.environ.get("JARVIS_MODELS_DIR"):
        # With speech models at hand, prove the engines really run in this installation:
        # speak a sentence, then detect the wake word in it and recognise it.
        try:
            import numpy as np

            from jarvis.config import Config as _Config
            from jarvis.voice.engines import Synthesizer, Transcriber, VoiceActivity, WakeDetector
            from jarvis.voice.models import ModelStore

            store = ModelStore(Path(os.environ["JARVIS_MODELS_DIR"]))
            stt_key = _Config().voice.stt_model
            folders = {key: await store.ensure(key) for key in ("kws", "vad", stt_key, "en_GB-alan-medium")}
            tts = Synthesizer(folders["en_GB-alan-medium"] / "en_GB-alan-medium.onnx")
            wake = WakeDetector(folders["kws"], "jarvis", "medium")
            VoiceActivity(folders["vad"] / "silero_vad.onnx")
            stt = Transcriber(folders[stt_key])
            woke, text = False, ""
            for _ in range(4):                       # the synthetic voice varies a little each time
                pcm, rate = tts.synthesize("Jarvis, what time is it?")
                samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                count = int(len(samples) * 16000 / rate)
                audio = np.interp(np.linspace(0, len(samples) - 1, count), np.arange(len(samples)), samples).astype(np.float32)
                audio = np.concatenate([audio, np.zeros(16000, dtype=np.float32)])
                wake.reset()
                for start in range(0, len(audio), 1600):
                    woke = wake.feed(audio[start:start + 1600]) or woke
                text = stt.transcribe(audio)
                if woke and "time" in text.lower():
                    break
            check(woke, "wake word is detected in synthesised speech")
            check("time" in text.lower(), "speech is recognised", repr(text))
        except Exception as exc:  # noqa: BLE001
            check(False, "speech engines", f"{type(exc).__name__}: {exc}")

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["JARVIS_HOME"] = tmp
        from jarvis.config import Config
        from jarvis.core import JarvisCore
        from jarvis.server import create_app

        cfg = Config()
        cfg.brain.backend = "demo"
        cfg.voice.enabled = False
        cfg.safety.approved_dirs = [str(Path(tmp) / "workspace")]
        core = JarvisCore(config=cfg)
        port = _free_port("127.0.0.1", 8765)
        server = uvicorn.Server(uvicorn.Config(create_app(core, None, token="selftest", port=port), host="127.0.0.1", port=port, log_level="error"))
        task = asyncio.create_task(server.serve())
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.05)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                check((await client.get("/")).status_code == 401, "interface refuses strangers")
                check((await client.get("/?token=selftest")).status_code == 303, "interface accepts its own window")
                page = await client.get("/")
                check(page.status_code == 200 and "J.A.R.V.I.S." in page.text, "interface page loads")
                check((await client.get("/static/app.js")).status_code == 200, "interface script loads")
                await client.post("/api/message", json={"text": "create file selftest.txt saying ok"})
                for _ in range(100):
                    if (Path(tmp) / "workspace" / "selftest.txt").exists():
                        break
                    await asyncio.sleep(0.05)
                check((Path(tmp) / "workspace" / "selftest.txt").exists(), "a request runs a tool end to end")
        except Exception as exc:  # noqa: BLE001
            check(False, "interface server", f"{type(exc).__name__}: {exc}")
        finally:
            server.should_exit = True
            await task
    print(_good("Self-test passed.") if not failures else _bad(f"Self-test failed ({failures} problem(s))."))
    return 1 if failures else 0


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
    run = sub.add_parser("run", help="start Jarvis with its interface window (the default)")
    run.add_argument("--port", type=int, default=0, help="port for the local interface")
    run.add_argument("--no-window", action="store_true", help="do not open the interface window")
    run.add_argument("--no-voice", action="store_true", help="start without the microphone and speech")
    sub.add_parser("chat", help="talk to Jarvis in this terminal")
    sub.add_parser("doctor", help="check that everything Jarvis needs is in place")
    sub.add_parser("login", help="sign in to your Claude account (needed once)")
    selftest = sub.add_parser("selftest", help="check that this installation is complete")
    selftest.add_argument("--require-voice", action="store_true", help="fail if the voice components are missing")
    cfg = sub.add_parser("config", help="show the settings file or where it lives")
    cfg.add_argument("action", choices=["path", "show"], nargs="?", default="path")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if args.command == "doctor":
        return _doctor(args)
    if args.command == "login":
        return _login(args)
    if args.command == "selftest":
        return asyncio.run(_selftest(args))
    if args.command == "config":
        return _config(args)
    if args.command in (None, "run", "chat"):
        if args.command is None:
            args = parser.parse_args([*(argv if argv is not None else sys.argv[1:]), "run"])
        try:
            code = asyncio.run(_chat(args) if args.command == "chat" else _run(args))
        except KeyboardInterrupt:
            code = 130
        if code and getattr(sys, "frozen", False):
            input("\nPress Enter to close this window.")     # keep the error readable when started by double-click
        return code
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
