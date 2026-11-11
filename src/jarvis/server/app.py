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

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from jarvis import __version__
from jarvis.config import expand_dirs
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
            section, key = _SETTING_MAP[field]
            changes.setdefault(section, {})[key] = value
        try:
            await core.apply_settings(changes)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if voice is not None and "voice" in changes:
            await voice.reconfigure()
        return settings_view()

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
