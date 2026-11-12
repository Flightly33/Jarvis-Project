from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from jarvis.config import Config, set_secret
from jarvis.core import JarvisCore
from jarvis.phone import PhoneLink


class Telegram:
    """A stand-in for Telegram: hands out queued messages and records what Jarvis sends."""

    def __init__(self):
        self.incoming: list[dict] = []
        self.sent: list[tuple[int, str]] = []
        self.next_id = 1

    def say(self, chat: int, text: str):
        self.incoming.append({"update_id": self.next_id, "message": {"chat": {"id": chat}, "text": text}})
        self.next_id += 1

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "/botTOKEN123/" in str(request.url)
        if request.url.path.endswith("/getUpdates"):
            batch = [u for u in self.incoming if u["update_id"] >= body["offset"]]
            if not batch:
                await asyncio.sleep(0.02)
            return httpx.Response(200, json={"ok": True, "result": batch})
        self.sent.append((body["chat_id"], body["text"]))
        return httpx.Response(200, json={"ok": True, "result": {}})


@pytest.fixture
async def rig(paths, home, workspace):
    cfg = Config()
    cfg.brain.backend = "demo"
    cfg.safety.approved_dirs = [str(workspace)]
    cfg.voice.enabled = False
    core = JarvisCore(paths, cfg)
    set_secret(paths, "JARVIS_TELEGRAM_TOKEN", "TOKEN123")
    telegram = Telegram()
    core.phone = PhoneLink(core, transport=httpx.MockTransport(telegram))
    await core.start()
    yield core, telegram, workspace
    await core.close()


async def until(condition, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < end, "condition not reached"
        await asyncio.sleep(0.02)


async def test_strangers_are_refused_and_pairing_needs_the_code_from_the_computer(rig):
    core, telegram, workspace = rig
    telegram.say(111, "create file hacked.txt saying owned")
    telegram.say(111, "/pair 000000")
    await until(lambda: len(telegram.sent) == 2)
    assert "private" in telegram.sent[0][1] and "not right" in telegram.sent[1][1]
    assert not (workspace / "hacked.txt").exists() and core.config.phone.telegram_chat_id == 0
    code = core.phone.pairing_code
    telegram.say(222, f"/pair {code}")
    await until(lambda: core.config.phone.telegram_chat_id == 222)
    assert core.phone.pairing_code != code                       # a code works only once
    telegram.say(111, "list files")
    await until(lambda: any(c == 111 and "private" in t for c, t in telegram.sent[2:]))


async def test_owner_can_work_from_the_phone_and_gets_reminders(rig):
    core, telegram, workspace = rig
    core.config.phone.telegram_chat_id = 222
    telegram.say(222, "create file note.txt saying from my phone")
    await until(lambda: (workspace / "note.txt").exists() and telegram.sent)
    assert telegram.sent[-1] == (222, "I've created note.txt and confirmed it is there.")
    core.store.add_reminder("submit the essay", 1.0)
    core.scheduler.tick()
    await until(lambda: (222, "Reminder: submit the essay") in telegram.sent)


async def test_approvals_from_the_phone(rig):
    core, telegram, workspace = rig
    core.config.phone.telegram_chat_id = 222
    (workspace / "old.txt").write_text("x")
    telegram.say(222, "delete old.txt")
    await until(lambda: any("Approval needed" in t for _, t in telegram.sent))
    telegram.say(222, "no")
    await until(lambda: any("declined" in t for _, t in telegram.sent))
    assert (workspace / "old.txt").exists()
    # high-risk actions cannot be approved remotely
    request = asyncio.create_task(core.services.approvals.request(tool="t", title="erase", detail="", risk="high", reasons=[]))
    await asyncio.sleep(0.05)
    telegram.say(222, "yes")
    await until(lambda: any("only be approved at the computer" in t for _, t in telegram.sent))
    assert not request.done()
    core.services.approvals.decline_all()
    await request


async def test_upcoming_calendar_events_are_announced_once(rig):
    core, telegram, _ = rig
    core.config.phone.telegram_chat_id = 222
    core.store.add_event("Maths exam", 10_000 + 600)
    core.store.add_event("Holiday", 10_000 + 300, all_day=True)
    core.scheduler.tick(now=10_000)
    core.scheduler.tick(now=10_010)
    await until(lambda: any("Maths exam starts at" in t for _, t in telegram.sent))
    await asyncio.sleep(0.1)
    assert sum("Maths exam" in t for _, t in telegram.sent) == 1 and not any("Holiday" in t for _, t in telegram.sent)
