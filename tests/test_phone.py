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
        self.voices: list[bytes] = []
        self.next_id = 1

    def say(self, chat: int, text: str):
        self.incoming.append({"update_id": self.next_id, "message": {"chat": {"id": chat}, "text": text}})
        self.next_id += 1

    def photo(self, chat: int, caption: str = ""):
        sizes = [{"file_id": "small", "file_size": 100}, {"file_id": "big", "file_size": 5000}]
        self.incoming.append({"update_id": self.next_id, "message": {"chat": {"id": chat}, "photo": sizes, "caption": caption}})
        self.next_id += 1

    def speak(self, chat: int, file_id: str = "rec1", duration: int = 3):
        self.incoming.append({"update_id": self.next_id, "message": {"chat": {"id": chat}, "voice": {"file_id": file_id, "duration": duration, "file_size": 900}}})
        self.next_id += 1

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        assert "/botTOKEN123/" in str(request.url)
        if request.url.path.startswith("/file/"):
            return httpx.Response(200, content=b"RECORDING:" + request.url.path.rsplit("/", 1)[-1].encode())
        if request.url.path.endswith("/sendVoice"):
            assert b'name="chat_id"\r\n\r\n222' in request.content
            self.voices.append(request.content)
            return httpx.Response(200, json={"ok": True, "result": {}})
        body = json.loads(request.content)
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": f"voice/{body['file_id']}.oga"}})
        if request.url.path.endswith("/getUpdates"):
            batch = [u for u in self.incoming if u["update_id"] >= body["offset"]]
            if not batch:
                await asyncio.sleep(0.02)
            return httpx.Response(200, json={"ok": True, "result": batch})
        self.sent.append((body["chat_id"], body["text"]))
        return httpx.Response(200, json={"ok": True, "result": {}})


class Speech:
    """Stands in for the speech models: each recording 'says' a fixed sentence."""

    said = {b"RECORDING:rec1.oga": "create file spoken.txt saying hello from a voice message", b"RECORDING:rec2.oga": "Stop.",
            b"RECORDING:rec3.oga": ""}

    async def transcribe(self, recording: bytes) -> str:
        return self.said[recording]

    async def speak(self, text: str) -> bytes:
        return b"OGG:" + text.encode()


@pytest.fixture
async def rig(paths, home, workspace):
    cfg = Config()
    cfg.brain.backend = "demo"
    cfg.safety.approved_dirs = [str(workspace)]
    cfg.voice.enabled = False
    core = JarvisCore(paths, cfg)
    set_secret(paths, "JARVIS_TELEGRAM_TOKEN", "TOKEN123")
    telegram = Telegram()
    core.phone = core.services.phone = PhoneLink(core, transport=httpx.MockTransport(telegram), speech=Speech())
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


async def test_voice_messages_are_heard_and_answered_in_voice(rig):
    core, telegram, workspace = rig
    telegram.speak(111)                                   # a stranger's recording is not even downloaded
    await until(lambda: any(c == 111 and "private" in t for c, t in telegram.sent))
    core.config.phone.telegram_chat_id = 222
    telegram.speak(222)
    await until(lambda: (workspace / "spoken.txt").exists() and telegram.voices)
    assert (222, "I heard: create file spoken.txt saying hello from a voice message") in telegram.sent
    assert b"OGG:I've created spoken.txt" in telegram.voices[0]
    assert not (workspace / "hacked.txt").exists() and len(telegram.voices) == 1
    telegram.speak(222, "rec3")
    await until(lambda: any("could not make out" in t for _, t in telegram.sent))
    telegram.speak(222, "rec1", duration=900)
    await until(lambda: any("too long" in t for _, t in telegram.sent))
    telegram.speak(222, "rec2")                            # saying "stop" halts things, like /stop
    await until(lambda: (222, "Stopped.") in telegram.sent)


def test_voice_message_audio_survives_packing_and_unpacking():
    pytest.importorskip("av")
    import numpy as np

    from jarvis.voice.notes import decode, encode

    rate = 22_050
    tone = (np.sin(2 * np.pi * 440 * np.arange(rate * 2) / rate) * 12000).astype(np.int16)
    packed = encode(tone.tobytes(), rate)
    assert packed[:4] == b"OggS" and len(packed) < len(tone.tobytes()) / 4
    samples = decode(packed)
    assert abs(len(samples) / 16_000 - 2.0) < 0.1
    spectrum = np.abs(np.fft.rfft(samples[:16_000]))
    assert abs(int(spectrum.argmax()) - 440) <= 2            # still the same note
    with pytest.raises(Exception, match="could not be read"):
        decode(b"not audio at all")


async def test_real_speech_models_understand_a_voice_message(paths):
    import os

    models = os.environ.get("JARVIS_TEST_MODELS")
    if not models:
        pytest.skip("set JARVIS_TEST_MODELS to run against the real speech models")
    pytest.importorskip("av")
    from pathlib import Path

    from jarvis.voice.notes import NoteSpeech

    from types import SimpleNamespace

    from jarvis.voice.engines import Synthesizer, Transcriber

    cfg = Config()
    cfg.brain.backend = "demo"
    core = JarvisCore(paths, cfg)
    voice = cfg.voice.tts_voice
    from jarvis.voice.models import ModelStore

    store = ModelStore(Path(models))
    core.shared_engines = SimpleNamespace(stt=Transcriber(await store.ensure(cfg.voice.stt_model)),
                                          tts=Synthesizer(await store.ensure(voice) / f"{voice}.onnx"))
    speech = NoteSpeech(core)
    for _ in range(3):                                      # synthesis varies a little from run to run
        note = await speech.speak("Please remind me to call the dentist tomorrow morning.")
        heard = (await speech.transcribe(note)).lower()
        if "dentist" in heard and "tomorrow" in heard:
            break
    assert "dentist" in heard and "tomorrow" in heard


async def test_jarvis_can_message_and_voice_the_phone_when_asked(rig):
    core, telegram, _ = rig
    from jarvis.tools.base import TurnState

    turn = TurnState(source="voice")
    outcome = await core.executor.execute("phone_message", {"text": "Dinner is ready.", "spoken": True}, turn)
    assert outcome.is_error and "not paired" in outcome.text
    core.config.phone.telegram_chat_id = 222
    outcome = await core.executor.execute("phone_message", {"text": "Dinner is ready.", "spoken": True}, turn)
    assert not outcome.is_error and '"voice_message": true' in outcome.text
    assert (222, "Dinner is ready.") in telegram.sent and b"OGG:Dinner is ready." in telegram.voices[-1]


async def test_photos_from_the_phone_are_kept_for_jarvis_to_look_at(rig):
    core, telegram, workspace = rig
    telegram.photo(111, "look at this")                    # strangers' files are not fetched
    await until(lambda: any(c == 111 and "private" in t for c, t in telegram.sent))
    assert not (workspace / "From phone").exists()
    core.config.phone.telegram_chat_id = 222
    asked: list[str] = []
    original = core.assistant.ask

    async def ask(text, source):
        asked.append(text)
        return await original("what time is it", source)

    core.assistant.ask = ask
    telegram.photo(222, "what does this error say?")
    await until(lambda: asked and any(c == 222 for c, _ in telegram.sent))
    saved = list((workspace / "From phone").iterdir())
    assert len(saved) == 1 and saved[0].read_bytes() == b"RECORDING:big.oga" and saved[0].suffix == ".jpg"
    assert asked[0].startswith("what does this error say?") and str(saved[0]) in asked[0]
