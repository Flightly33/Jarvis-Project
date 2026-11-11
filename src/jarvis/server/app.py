"""HTTP and WebSocket endpoints behind the interface.

This server can make the computer do things, so it is locked down:

* it only ever listens on the loopback address (enforced in the settings);
* every request must carry a secret that is generated at start-up and handed
  to the interface window, so other programs and web pages cannot use it;
* the Host and Origin headers are checked, which defeats a malicious web page
  trying to reach it through the user's browser (cross-site requests and DNS
  rebinding).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from jarvis import __version__
from jarvis.brain.base import BackendUnavailable
from jarvis.brain.openai_compat import PRESETS, OpenAICompatBackend, _ApiError
from jarvis.config import expand_dirs, get_secret, set_secret
from jarvis.core import JarvisCore

log = logging.getLogger(__name__)

COOKIE = "jarvis_session"
STATIC = Path(__file__).parent / "static"
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageBody(_Body):
    text: str = Field(min_length=1, max_length=20_000)


class ApprovalBody(_Body):
    approved: bool
    scope: Literal["once", "request", "always"] = "once"


class FolderBody(_Body):
    path: str
    mode: Literal["write", "read"] = "write"


class EventBody(_Body):
    title: str = Field(min_length=1, max_length=200)
    start: str = Field(max_length=40)
    end: str | None = Field(default=None, max_length=40)
    all_day: bool = False
    notes: str = Field(default="", max_length=1000)


class SettingsBody(_Body):
    """The settings the interface may change. Everything else stays in the settings file."""

    name: str | None = Field(default=None, max_length=60)
    honorific: str | None = Field(default=None, max_length=30)
    autonomy: Literal["cautious", "balanced", "trusting"] | None = None
    model: str | None = Field(default=None, max_length=60)
    effort: Literal["low", "medium", "high"] | None = None
    voice_enabled: bool | None = None
    wake_enabled: bool | None = None
    wake_word: str | None = Field(default=None, max_length=40)
    wake_sensitivity: Literal["low", "medium", "high"] | None = None
    followup_seconds: float | None = Field(default=None, ge=0, le=30)
    tts_speed: float | None = Field(default=None, ge=0.5, le=2.0)
    tts_volume: float | None = Field(default=None, ge=0.0, le=2.0)
    backend: Literal["claude", "openai", "demo"] | None = None
    ai_provider: str | None = Field(default=None, max_length=30)      # a preset name, or "custom"
    ai_base_url: str | None = Field(default=None, max_length=300)
    ai_model: str | None = Field(default=None, max_length=120)
    ai_key: str | None = Field(default=None, max_length=400)          # write-only: never sent back
    ai_vision: bool | None = None


_SETTING_MAP = {
    "name": ("user", "name"), "honorific": ("user", "honorific"), "autonomy": ("safety", "autonomy"),
    "model": ("claude", "model"), "effort": ("claude", "effort"), "voice_enabled": ("voice", "enabled"),
    "wake_enabled": ("voice", "wake_enabled"), "wake_word": ("voice", "wake_word"),
    "wake_sensitivity": ("voice", "wake_sensitivity"), "followup_seconds": ("voice", "followup_seconds"),
    "tts_speed": ("voice", "tts_speed"), "tts_volume": ("voice", "tts_volume"),
}


def create_app(core: JarvisCore, voice: Any = None, *, token: str, port: int, extra_hosts: tuple[str, ...] = ()) -> FastAPI:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}", *extra_hosts}
    allowed_origins = {f"http://{host}" for host in allowed_hosts}
    background: set[asyncio.Task] = set()

    def authorised(supplied: str | None) -> bool:
        return bool(supplied) and hmac.compare_digest(supplied.encode(), token.encode())

    def spawn(coro) -> None:
        task = asyncio.create_task(coro)
        background.add(task)
        task.add_done_callback(background.discard)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await core.start()
        if voice is not None:
            await voice.start()
        try:
            yield
        finally:
            for task in list(background):
                task.cancel()
            if voice is not None:
                await voice.stop()
            await core.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts:
            return PlainTextResponse("Forbidden", status_code=403)
        path = request.url.path
        if path == "/" and "token" in request.query_params:
            if not authorised(request.query_params["token"]):
                return PlainTextResponse("Forbidden", status_code=403)
            # Move the secret out of the address bar and into a cookie no other site can send or read.
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/")
            return response
        if not authorised(request.cookies.get(COOKIE)):
            return PlainTextResponse("Open Jarvis from its own window or shortcut.", status_code=401)
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin and origin not in allowed_origins:
                return PlainTextResponse("Forbidden", status_code=403)
        response = await call_next(request)
        response.headers.update(_SECURITY_HEADERS)
        return response

    # ------------------------------------------------------------------ state

    def settings_view() -> dict[str, Any]:
        cfg = core.config
        return {
            "name": cfg.user.name, "honorific": cfg.user.honorific, "autonomy": cfg.safety.autonomy,
            "model": cfg.claude.model, "effort": cfg.claude.effort, "backend": cfg.brain.backend,
            "voice_enabled": cfg.voice.enabled, "wake_enabled": cfg.voice.wake_enabled, "wake_word": cfg.voice.wake_word,
            "wake_sensitivity": cfg.voice.wake_sensitivity, "followup_seconds": cfg.voice.followup_seconds,
            "tts_speed": cfg.voice.tts_speed, "tts_volume": cfg.voice.tts_volume,
            "approved_dirs": [str(p) for p in core.services.sandbox.approved],
            "read_only_dirs": [str(p) for p in core.services.sandbox.read_only],
            "command_allowlist": list(cfg.safety.command_allowlist),
            "settings_file": str(core.paths.config_file),
            "ai_provider": next((name for name, p in PRESETS.items() if p["base_url"] == cfg.openai.base_url), "custom"),
            "ai_base_url": cfg.openai.base_url, "ai_model": cfg.openai.model, "ai_vision": cfg.openai.supports_vision,
            "ai_key_saved": bool(cfg.openai.api_key_env and get_secret(core.paths, cfg.openai.api_key_env)),
            "ai_presets": {name: {"label": p["label"], "keys_page": p["keys_page"], "needs_key": bool(p["api_key_env"])} for name, p in PRESETS.items()},
        }

    def snapshot() -> dict[str, Any]:
        return {
            "version": __version__,
            "processing": core.assistant.busy,
            "backend": core.backend.status(),
            "voice": voice.snapshot() if voice is not None else {"state": "off", "available": False},
            "approvals": [r.public() for r in core.services.approvals.pending()],
            "history": core.store.transcript(limit=60),
            "reminders": [r.public() for r in core.store.reminders()],
            "jobs": [j.public(300) for j in core.services.jobs.jobs()[:10]],
            "settings": settings_view(),
            "now": time.time(),
        }

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return snapshot()

    # ----------------------------------------------------------- conversation

    @app.post("/api/message", status_code=202)
    async def message(body: MessageBody) -> dict[str, bool]:
        spawn(core.assistant.ask(body.text, "text"))
        return {"accepted": True}

    @app.post("/api/stop")
    async def stop() -> dict[str, bool]:
        await core.emergency_stop("interface")
        if voice is not None:
            await voice.silence()
        return {"stopped": True}

    @app.post("/api/login")
    async def login() -> dict[str, bool]:
        """Open the Claude sign-in in the browser and wait for it to finish."""
        from jarvis.cli import _claude_cli

        cli = _claude_cli()
        if cli is None:
            raise HTTPException(409, "The Claude program was not found in this installation.")
        try:
            proc = await asyncio.create_subprocess_exec(cli, "auth", "login")     # shares the console window, in case it asks something
            code = await asyncio.wait_for(proc.wait(), 600)
        except TimeoutError:
            proc.kill()
            raise HTTPException(504, "The sign-in was not completed in time. Please try again.") from None
        except OSError as exc:
            raise HTTPException(500, f"Could not start the sign-in: {exc}") from exc
        if code != 0:
            raise HTTPException(502, "The sign-in did not complete. Look at the Jarvis console window for details, then try again.")
        await core.backend.reset()
        return {"signed_in": True}

    @app.post("/api/conversation/new")
    async def new_conversation() -> dict[str, str]:
        await core.assistant.interrupt()
        await core.assistant.new_conversation()
        return {"conversation": core.assistant.conversation_id}

    @app.post("/api/approvals/{request_id}")
    async def approval(request_id: str, body: ApprovalBody) -> dict[str, bool]:
        if not core.services.approvals.resolve(request_id, body.approved, body.scope, by="interface"):
            raise HTTPException(404, "That request has already been answered or has expired.")
        return {"resolved": True}

    # --------------------------------------------------------------- settings

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return settings_view()

    @app.patch("/api/settings")
    async def patch_settings(body: SettingsBody) -> dict[str, Any]:
        changes: dict[str, dict[str, Any]] = {}
        for field, value in body.model_dump(exclude_none=True).items():
            if field in _SETTING_MAP:
                section, key = _SETTING_MAP[field]
                changes.setdefault(section, {})[key] = value
        service: dict[str, Any] = {}
        if body.ai_provider is not None:
            if body.ai_provider in PRESETS:
                preset = PRESETS[body.ai_provider]
                service |= {"base_url": preset["base_url"], "api_key_env": preset["api_key_env"]}
            elif body.ai_provider == "custom":
                url = (body.ai_base_url or core.config.openai.base_url).strip()
                parts = urlsplit(url)
                local = parts.hostname in {"localhost", "127.0.0.1", "::1"}
                if parts.scheme not in {"http", "https"} or not parts.hostname or (parts.scheme == "http" and not local):
                    raise HTTPException(422, "The service address must start with https:// (http:// is only accepted for this computer).")
                service |= {"base_url": url, "api_key_env": "JARVIS_AI_KEY"}
            else:
                raise HTTPException(422, "Unknown AI service.")
        if body.ai_model is not None:
            service["model"] = body.ai_model.strip()
        if body.ai_vision is not None:
            service["supports_vision"] = body.ai_vision
        if service:
            changes["openai"] = service
        try:
            await core.apply_settings(changes)
            if body.ai_key:
                name = core.config.openai.api_key_env
                if not name:
                    raise HTTPException(422, "This service does not use an API key.")
                set_secret(core.paths, name, body.ai_key.strip())
            if body.backend is not None:
                await core.switch_backend(body.backend)
        except (ValueError, BackendUnavailable) as exc:
            raise HTTPException(422, str(exc)) from exc
        if voice is not None and "voice" in changes:
            await voice.reconfigure()
        return settings_view()

    @app.get("/api/models")
    async def models() -> dict[str, Any]:
        """What the configured AI service offers, so a model can be picked from a list."""
        probe = OpenAICompatBackend(core.services, core.executor, lambda: "")
        if not core.config.openai.base_url:
            raise HTTPException(409, "Choose an AI service first.")
        try:
            return {"models": await probe.list_models()}
        except _ApiError as exc:
            raise HTTPException(502, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - network failures of every kind
            raise HTTPException(502, f"Could not reach the AI service: {type(exc).__name__}") from exc

    @app.post("/api/folders")
    async def add_folder(body: FolderBody) -> dict[str, Any]:
        path = expand_dirs([body.path])[0].resolve(strict=False)
        if not path.is_dir():
            raise HTTPException(422, f"{path} is not an existing folder.")
        if len(path.parts) <= 2 and path != Path.home():
            raise HTTPException(422, "The top of a drive or a system folder cannot be approved.")
        if any(path == p or p in path.parents or path in p.parents for p in core.paths.protected()):
            raise HTTPException(422, "That would expose the assistant's own settings and data.")
        safety = core.config.safety
        entry = str(path)
        approved = [d for d in safety.approved_dirs if expand_dirs([d])[0].resolve(strict=False) != path]
        read_only = [d for d in safety.read_only_dirs if expand_dirs([d])[0].resolve(strict=False) != path]
        (approved if body.mode == "write" else read_only).append(entry)
        await core.apply_settings({"safety": {"approved_dirs": approved, "read_only_dirs": read_only}})
        return settings_view()

    @app.delete("/api/folders")
    async def remove_folder(body: FolderBody) -> dict[str, Any]:
        path = expand_dirs([body.path])[0].resolve(strict=False)
        safety = core.config.safety
        approved = [d for d in safety.approved_dirs if expand_dirs([d])[0].resolve(strict=False) != path]
        read_only = [d for d in safety.read_only_dirs if expand_dirs([d])[0].resolve(strict=False) != path]
        if not approved:
            raise HTTPException(422, "At least one folder must stay approved for reading and writing.")
        await core.apply_settings({"safety": {"approved_dirs": approved, "read_only_dirs": read_only}})
        return settings_view()

    @app.get("/api/events")
    async def list_events(start: float, end: float) -> dict[str, Any]:
        return {"events": core.store.events(start, end)}

    @app.post("/api/events", status_code=201)
    async def add_event(body: EventBody) -> dict[str, Any]:
        from jarvis.tools.base import ToolError
        from jarvis.tools.calendar import parse_moment

        try:
            event = core.store.add_event(body.title, parse_moment(body.start), parse_moment(body.end) if body.end else None, body.all_day, body.notes)
        except (ValueError, ToolError) as exc:
            raise HTTPException(422, str(exc)) from exc
        core.bus.emit("calendar_changed")
        return event

    @app.delete("/api/events/{event_id}")
    async def delete_event(event_id: int) -> dict[str, bool]:
        if not core.store.delete_event(event_id):
            raise HTTPException(404, "That event no longer exists.")
        core.bus.emit("calendar_changed")
        return {"deleted": True}

    @app.get("/api/audit")
    async def audit(limit: int = 100) -> dict[str, Any]:
        return {"entries": core.store.audit(max(1, min(limit, 500)))}

    # ------------------------------------------------------------------ voice

    @app.post("/api/voice/{action}")
    async def voice_action(action: Literal["listen", "silence", "pause", "resume"]) -> dict[str, Any]:
        if voice is None:
            raise HTTPException(409, "Voice is not available in this session.")
        await getattr(voice, {"listen": "push_to_talk", "silence": "silence", "pause": "pause", "resume": "resume"}[action])()
        return voice.snapshot()

    # ----------------------------------------------------------------- events

    @app.websocket("/ws")
    async def events(ws: WebSocket) -> None:
        origin = ws.headers.get("origin")
        if (
            ws.headers.get("host", "") not in allowed_hosts
            or not authorised(ws.cookies.get(COOKIE))
            or (origin and origin not in allowed_origins)
        ):
            await ws.close(code=4403)
            return
        await ws.accept()
        queue = core.bus.subscribe()
        try:
            await ws.send_json({"type": "hello", **snapshot()})
            while True:
                await ws.send_json(await queue.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            core.bus.unsubscribe(queue)
            with contextlib.suppress(Exception):
                await ws.close()

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error in %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "Something went wrong inside Jarvis."}, status_code=500)

    return app
