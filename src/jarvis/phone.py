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
from typing import TYPE_CHECKING, Any

import httpx

from jarvis.config import get_secret
from jarvis.intents import local_intent

if TYPE_CHECKING:
    from jarvis.core import JarvisCore

log = logging.getLogger(__name__)
SECRET = "JARVIS_TELEGRAM_TOKEN"  # noqa: S105 - the name of a stored secret
_LIMIT = 3900


class PhoneLink:
    def __init__(self, core: JarvisCore, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.core = core
        self._transport = transport
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
                if text and chat:
                    try:
                        await self._handle(int(chat), text)
                    except Exception:  # noqa: BLE001
                        log.exception("failed to handle a phone message")

    async def _handle(self, chat: int, text: str) -> None:
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
            await self.send("Send a message and Jarvis will act on it on your computer. /stop halts whatever it is doing.")
            return
        if text.lower() == "/stop":
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
        task = asyncio.create_task(self._turn(text))
        self._turns.add(task)
        task.add_done_callback(self._turns.discard)

    async def _turn(self, text: str) -> None:
        self._phone_turns += 1
        try:
            result = await self.core.assistant.ask(text, "phone")
        finally:
            self._phone_turns -= 1
        await self.send(result.text or result.error or ("Stopped." if result.interrupted else "Done."))
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
