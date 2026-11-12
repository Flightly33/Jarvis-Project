"""Talking to Jarvis from a phone, through a private Telegram bot.

The user creates their own bot (free) and gives Jarvis its token. Jarvis then
asks Telegram for new messages from this computer (no ports are opened and
nothing needs to be reachable from the internet). Only one chat - the one
paired with a code shown on this computer - is ever obeyed.

Messages travel through Telegram's servers, so this is an external channel:
what Jarvis replies there leaves the computer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

import httpx

from jarvis.config import get_secret
from jarvis.intents import local_intent

if TYPE_CHECKING:
    from jarvis.core import JarvisCore

log = logging.getLogger(__name__)
SECRET = "JARVIS_TELEGRAM_TOKEN"  # noqa: S105 - the name of a stored secret
_LIMIT = 3900
_MAX_NOTE_SECONDS = 120
_MAX_NOTE_BYTES = 5_000_000
_MAX_FILE_BYTES = 20_000_000


def intent_is_stop(text: str) -> bool:
    return local_intent(text) == "stop"


class PhoneLink:
    def __init__(self, core: JarvisCore, transport: httpx.AsyncBaseTransport | None = None, speech: Any = None) -> None:
        self.core = core
        self._transport = transport
        self._speech = speech          # turns voice messages into text and replies into voice messages
        self.pairing_code = f"{secrets.randbelow(900000) + 100000}"
        self.problem = ""
        self._tasks: list[asyncio.Task] = []
        self._turns: set[asyncio.Task] = set()
        self._offset = 0
        self._phone_turns = 0

    @property
    def token(self) -> str | None:
        return get_secret(self.core.paths, SECRET)

    @property
    def owner(self) -> int:
        return self.core.config.phone.telegram_chat_id

    def snapshot(self) -> dict[str, Any]:
        return {"phone_connected": bool(self.token) and not self.problem, "phone_paired": bool(self.owner),
                "phone_code": self.pairing_code, "phone_problem": self.problem}

    async def start(self) -> None:
        if self.token and not self._tasks:
            self._tasks = [asyncio.create_task(self._poll()), asyncio.create_task(self._watch())]

    async def stop(self) -> None:
        for task in [*self._tasks, *self._turns]:
            task.cancel()
        for task in [*self._tasks, *self._turns]:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    async def restart(self) -> None:
        await self.stop()
        self.problem = ""
        await self.start()

    async def _api(self, method: str, wait: float = 20.0, **params: Any) -> Any:
        async with httpx.AsyncClient(timeout=wait, transport=self._transport) as client:
            response = await client.post(f"https://api.telegram.org/bot{self.token}/{method}", json=params)
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description") or f"Telegram error {response.status_code}")
        return data.get("result")

    async def send(self, text: str, chat_id: int | None = None) -> bool:
        """Send a message to the paired phone. Returns False if there is nowhere to send it."""
        target = chat_id or self.owner
        if not self.token or not target or not text.strip():
            return False
        try:
            for start in range(0, len(text), _LIMIT):
                await self._api("sendMessage", chat_id=target, text=text[start:start + _LIMIT])
            return True
        except Exception as exc:  # noqa: BLE001 - the phone being unreachable must not disturb anything else
            log.warning("could not message the phone: %s", exc)
            return False

    async def _speech_engine(self) -> Any:
        if self._speech is None:
            try:
                from jarvis.voice.notes import NoteSpeech
            except ImportError as exc:
                raise RuntimeError("Voice messages need Jarvis's voice support, which is not installed.") from exc
            self._speech = NoteSpeech(self.core)
        return self._speech

    async def _download(self, file_id: str) -> bytes:
        info = await self._api("getFile", file_id=file_id)
        async with httpx.AsyncClient(timeout=60.0, transport=self._transport) as client:
            response = await client.get(f"https://api.telegram.org/file/bot{self.token}/{info['file_path']}")
        response.raise_for_status()
        return response.content

    async def send_voice(self, text: str) -> bool:
        """Say `text` to the paired phone as a voice message. Returns False if that was not possible."""
        try:
            speech = await self._speech_engine()
            note = await speech.speak(text)
            if not note or not self.owner:
                return False
            async with httpx.AsyncClient(timeout=60.0, transport=self._transport) as client:
                response = await client.post(f"https://api.telegram.org/bot{self.token}/sendVoice", data={"chat_id": str(self.owner)},
                                             files={"voice": ("jarvis.ogg", note, "audio/ogg")})
            return bool(response.json().get("ok"))
        except Exception as exc:  # noqa: BLE001 - the written reply still goes out
            log.warning("could not send a voice message: %s", exc)
            return False

    async def _hear(self, chat: int, recording: dict[str, Any]) -> None:
        """A voice message from the phone: write down what was said, then treat it like a typed message."""
        if chat != self.owner:
            await self._handle(chat, "(voice message)")
            return
        if int(recording.get("duration") or 0) > _MAX_NOTE_SECONDS or int(recording.get("file_size") or 0) > _MAX_NOTE_BYTES:
            await self.send("That recording is too long for me. Keep voice messages under two minutes.")
            return
        try:
            speech = await self._speech_engine()
            text = (await speech.transcribe(await self._download(recording["file_id"]))).strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read a voice message: %s", exc)
            await self.send(f"I could not listen to that voice message. {exc}")
            return
        if not text:
            await self.send("I could not make out any words in that voice message.")
            return
        await self.send(f"I heard: {text}")
        await self._handle(chat, text, spoken=True)

    async def _receive(self, chat: int, attachment: dict[str, Any], caption: str) -> None:
        """A photo or file from the phone: keep it in an approved folder and tell Jarvis where it is."""
        if chat != self.owner:
            await self._handle(chat, "(file)")
            return
        if int(attachment.get("file_size") or 0) > _MAX_FILE_BYTES:
            await self.send("That file is too large for me to receive here (the limit is 20 MB).")
            return
        roots = self.core.services.sandbox.approved
        if not roots:
            await self.send("I have no approved folder to keep that in. Add one under Settings > Safety.")
            return
        try:
            data = await self._download(attachment["file_id"])
        except Exception as exc:  # noqa: BLE001
            await self.send(f"I could not fetch that file from Telegram ({type(exc).__name__}).")
            return
        given = "".join(ch for ch in str(attachment.get("file_name") or "") if ch.isalnum() or ch in " ._-").strip(" .")
        name = given or f"photo-{time.strftime('%Y%m%d-%H%M%S')}.jpg"
        folder = roots[0] / "From phone"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        if path.exists():
            path = folder / f"{path.stem}-{time.strftime('%H%M%S')}{path.suffix}"
        path.write_bytes(data)
        request = caption or "I have sent you this file. Tell me briefly what it is and ask what I want done with it."
        await self._handle(chat, f"{request}\n\n[Sent from the phone with this message: a file now saved at {path}. Use view_image to look at it if it is a picture.]")

    async def _poll(self) -> None:
        while True:
            try:
                updates = await self._api("getUpdates", 40.0, offset=self._offset, timeout=25, allowed_updates=["message"])
                self.problem = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.problem = "Telegram rejected the bot token." if "Unauthorized" in str(exc) else f"Cannot reach Telegram ({type(exc).__name__})."
                await asyncio.sleep(15)
                continue
            for update in updates or []:
                self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                message = update.get("message") or {}
                text = (message.get("text") or "").strip()
                chat = (message.get("chat") or {}).get("id")
                recording = message.get("voice") or message.get("audio")
                attachment = (message.get("photo") or [None])[-1] or message.get("document")      # the last photo size is the largest
                if chat and (text or recording or attachment):
                    try:
                        if attachment:
                            await self._receive(int(chat), attachment, (message.get("caption") or "").strip())
                        elif recording and not text:
                            await self._hear(int(chat), recording)
                        else:
                            await self._handle(int(chat), text)
                    except Exception:  # noqa: BLE001
                        log.exception("failed to handle a phone message")

    async def _handle(self, chat: int, text: str, spoken: bool = False) -> None:
        if text.lower().startswith("/pair"):
            supplied = text[5:].strip()
            if secrets.compare_digest(supplied, self.pairing_code):
                self.core.config.phone.telegram_chat_id = chat
                self.core.services.save_config()
                self.pairing_code = f"{secrets.randbelow(900000) + 100000}"      # a code works once
                self.core.bus.emit("settings_changed")
                await self.send("Paired. You can talk to Jarvis here now, and reminders will arrive in this chat.", chat)
            else:
                await self.send("That pairing code is not right. Use the one shown in Jarvis's Settings on your computer.", chat)
            return
        if chat != self.owner:
            await self.send("This Jarvis is private. To pair, send /pair followed by the code shown in its Settings.", chat)
            return
        if text.lower() in {"/start", "/help"}:
            await self.send("Send a message or a voice message and Jarvis will act on it on your computer. /stop halts whatever it is doing.")
            return
        if text.lower() == "/stop" or (spoken and intent_is_stop(text)):
            await self.core.emergency_stop("phone")
            await self.send("Stopped.")
            return
        approvals = self.core.services.approvals
        intent = local_intent(text)
        if approvals.pending() and intent in {"yes", "no", "confirm"}:
            answered = approvals.resolve_latest(intent != "no", by="phone")
            if answered is None:
                await self.send("That action is high risk, so it can only be approved at the computer. Reply no to decline it.")
            return
        task = asyncio.create_task(self._turn(text, spoken))
        self._turns.add(task)
        task.add_done_callback(self._turns.discard)

    async def _turn(self, text: str, spoken: bool = False) -> None:
        self._phone_turns += 1
        try:
            result = await self.core.assistant.ask(text, "phone")
        finally:
            self._phone_turns -= 1
        await self.send(result.text or result.error or ("Stopped." if result.interrupted else "Done."))
        if spoken and result.text:
            await self.send_voice(result.text)      # a spoken question gets a spoken answer as well
        if result.error and result.text:
            await self.send(result.error)

    async def _watch(self) -> None:
        """Forward what should reach the phone: reminders, and approvals needed by a request sent from it."""
        queue = self.core.bus.subscribe()
        try:
            while True:
                event = await queue.get()
                if event["type"] == "reminder" and self.core.config.phone.notify:
                    await self.send(f"Reminder: {event['text']}")
                elif event["type"] == "approval_request" and self._phone_turns:
                    request = event["request"]
                    high = request["risk"] == "high"
                    await self.send(f"Approval needed: {request['title']}\n" + ("This is high risk and can only be approved at the computer. Reply no to decline." if high else "Reply yes or no."))
        finally:
            self.core.bus.unsubscribe(queue)
