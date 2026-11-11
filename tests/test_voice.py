from __future__ import annotations

import asyncio
import os
from collections import deque
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from jarvis.config import Config  # noqa: E402
from jarvis.core import JarvisCore  # noqa: E402
from jarvis.voice import Engines, UtteranceCapture, VoiceController  # noqa: E402
from jarvis.voice.text import SentenceChunker, local_intent, speakable, strip_wake_word  # noqa: E402

SILENCE = np.zeros(1600, dtype=np.float32)
SPEECH = np.full(1600, 0.3, dtype=np.float32)
WAKE = np.full(1600, 0.95, dtype=np.float32)       # the stand-in wake detector fires on this


# ------------------------------------------------------------------- text


def test_sentences_are_released_as_they_complete():
    chunker = SentenceChunker()
    out = []
    for piece in ["I found the issue in your game ", "project. The level transitions ", "are mishandled. I have a fix", " ready"]:
        out += chunker.feed(piece)
    assert out == ["I found the issue in your game project.", "The level transitions are mishandled."]
    assert chunker.flush() == ["I have a fix ready"]
    assert SentenceChunker().feed("Version 3.5 is out and it is e.g. faster. Next") == ["Version 3.5 is out and it is e.g. faster."]
    assert SentenceChunker().feed("First line\nSecond") == ["First line"]


def test_reply_text_is_cleaned_for_reading_aloud():
    text = "**Done.** See [the docs](https://example.com/a/b) or https://www.python.org/downloads/ - `ls -la` works.\n- item one\n```py\nprint(1)\n```"
    spoken = speakable(text)
    assert spoken == "Done. See the docs or python.org - ls -la works. item one"
    assert speakable("<ignore/>") == "" and speakable("Great 🎉 news") == "Great news"


@pytest.mark.parametrize(("heard", "rest"), [
    ("Jarvis, what time is it?", "what time is it?"),
    ("Hey Jarvis open my browser", "open my browser"),
    ("Okay, Jarvis. Check my assignments", "Check my assignments"),
    ("Jervis what time is it", "what time is it"),          # misheard name
    ("Jarvas, stop", "stop"),
    ("Jarvis.", ""),
    ("jarvis", ""),
])
def test_wake_word_is_removed_from_the_transcript(heard, rest):
    assert strip_wake_word(heard, "jarvis") == (rest, True)


def test_ordinary_sentences_are_left_alone():
    assert strip_wake_word("What time is it?", "jarvis") == ("What time is it?", False)
    assert strip_wake_word("Service was good", "jarvis")[1] is False
    assert strip_wake_word("Hey Friday, play music", "friday") == ("play music", True)


@pytest.mark.parametrize(("said", "intent"), [
    ("Stop.", "stop"), ("Jarvis, stop talking", "stop"), ("cancel that please", "stop"), ("Never mind.", "nevermind"),
    ("Yes, please.", "yes"), ("go ahead", "yes"), ("No.", "no"), ("I confirm", "confirm"), ("stop listening", "pause"),
    ("stop the music player", None), ("yes I think the essay is due friday", None), ("what's the weather", None),
])
def test_short_spoken_commands(said, intent):
    assert local_intent(said) == intent


# ---------------------------------------------------------------- capture


class FakeVad:
    def reset(self):
        pass

    def feed(self, chunk):
        return bool(np.abs(chunk).mean() > 0.1)


def test_capture_ends_when_the_speaker_stops():
    cap = UtteranceCapture(FakeVad(), start_timeout=1.0, max_seconds=10)
    assert cap.feed(SILENCE) is None and cap.feed(SPEECH) is None and cap.feed(SPEECH) is None
    assert cap.feed(SILENCE) == "done" and len(cap.audio()) == 4 * 1600


def test_capture_times_out_if_nobody_speaks_and_is_capped():
    cap = UtteranceCapture(FakeVad(), start_timeout=0.3, max_seconds=10)
    assert [cap.feed(SILENCE) for _ in range(3)] == [None, None, "timeout"]
    long = UtteranceCapture(FakeVad(), start_timeout=1, max_seconds=0.5)
    assert [long.feed(SPEECH) for _ in range(5)][-1] == "done"


def test_preroll_keeps_the_start_of_a_request_said_in_one_breath():
    cap = UtteranceCapture(FakeVad(), start_timeout=1.0, max_seconds=10, preroll=np.concatenate([WAKE, SPEECH]))
    assert cap.speech_started and cap.feed(SPEECH) is None and cap.feed(SILENCE) == "done"
    assert len(cap.audio()) == 4 * 1600


# ------------------------------------------------------------- controller


class FakeWake:
    def feed(self, chunk):
        return bool(chunk.max() > 0.9)

    def reset(self):
        pass


class FakeStt:
    def __init__(self):
        self.script: deque[str] = deque()

    def transcribe(self, samples):
        return self.script.popleft() if self.script else ""


class FakeTts:
    def synthesize(self, text):
        return text.encode(), 16000


class FakeSpeaker:
    def __init__(self):
        self.played: list[str] = []
        self.stops = 0

    def play(self, pcm, rate):
        self.played.append(pcm.decode())

    def stop(self):
        self.stops += 1


class FakeMic:
    def __init__(self):
        self.sink = None
        self.running = False

    def start(self, sink):
        self.sink, self.running = sink, True

    def stop(self):
        self.running = False

    def push(self, *chunks):
        for chunk in chunks:
            self.sink(chunk)


@pytest.fixture
async def rig(paths, home: Path, workspace: Path):
    cfg = Config()
    cfg.brain.backend = "demo"
    cfg.safety.approved_dirs = [str(workspace)]
    cfg.safety.approval_timeout_seconds = 5
    cfg.voice.followup_seconds = 0.3
    cfg.voice.start_timeout_seconds = 0.5
    core = JarvisCore(paths, cfg)
    await core.start()
    stt, speaker, mic = FakeStt(), FakeSpeaker(), FakeMic()
    voice = VoiceController(core, engines=Engines(FakeWake(), FakeVad(), stt, FakeTts()), microphone=mic, speaker=speaker)
    events: list[dict] = []
    q = core.bus.subscribe()

    async def collect():
        while True:
            events.append(await q.get())

    collector = asyncio.create_task(collect())
    await voice.start()
    await until(lambda: voice.snapshot()["state"] == "idle")
    yield type("Rig", (), {"core": core, "voice": voice, "stt": stt, "speaker": speaker, "mic": mic, "events": events, "workspace": workspace})
    collector.cancel()
    await voice.stop()
    await core.close()


async def until(condition, timeout: float = 5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not reached in time")
        await asyncio.sleep(0.01)


def kinds(rig, kind):
    return [e for e in rig.events if e["type"] == kind]


async def test_wake_word_request_is_answered_aloud_then_keeps_listening(rig):
    rig.stt.script.append("Jarvis, create file notes.txt saying hello")
    rig.mic.push(SILENCE, WAKE)
    await until(lambda: rig.voice.snapshot()["state"] == "listening")
    rig.mic.push(SPEECH, SPEECH, SILENCE)
    await until(lambda: kinds(rig, "assistant_message"))
    asked = kinds(rig, "user_message")[0]
    assert asked["text"] == "create file notes.txt saying hello" and asked["source"] == "voice"
    assert (rig.workspace / "notes.txt").read_text() == "hello\n"
    await until(lambda: rig.speaker.played)
    assert rig.speaker.played == ["I've created notes.txt and confirmed it is there."]
    # the microphone stays open for a follow-up without the wake word...
    await until(lambda: rig.voice.snapshot()["detail"] == "Still listening")
    # ...and goes back to waiting for the wake word when nothing is said
    rig.mic.push(SILENCE, SILENCE, SILENCE, SILENCE)
    await until(lambda: rig.voice.snapshot()["state"] == "idle")


async def test_follow_up_needs_no_wake_word(rig):
    rig.stt.script.extend(["Jarvis, create file a.txt saying one", "list files"])
    rig.mic.push(WAKE, SPEECH, SILENCE)
    await until(lambda: rig.voice.snapshot()["detail"] == "Still listening")
    rig.mic.push(SPEECH, SILENCE)
    await until(lambda: len(kinds(rig, "assistant_message")) == 2)
    assert kinds(rig, "user_message")[1]["source"] == "voice-followup"


async def test_saying_only_the_name_waits_for_the_request(rig):
    rig.stt.script.extend(["Jarvis.", "list files"])
    rig.mic.push(WAKE, SILENCE)
    await until(lambda: rig.voice.snapshot()["detail"] == "Go ahead" and not rig.stt.script.count("Jarvis."))
    rig.mic.push(SPEECH, SILENCE)
    await until(lambda: kinds(rig, "user_message"))
    assert kinds(rig, "user_message")[0]["text"] == "list files"


async def test_ordinary_talk_without_the_wake_word_is_ignored(rig):
    rig.stt.script.append("delete everything")
    rig.mic.push(SPEECH, SPEECH, SILENCE, SILENCE)
    await asyncio.sleep(0.2)
    assert kinds(rig, "user_message") == [] and rig.voice.snapshot()["state"] == "idle"


async def test_approval_can_be_declined_by_voice(rig):
    (rig.workspace / "old.txt").write_text("x")
    rig.stt.script.extend(["Jarvis, delete old.txt", "No."])
    rig.mic.push(WAKE, SPEECH, SILENCE)
    await until(lambda: kinds(rig, "approval_request"))
    await until(lambda: rig.voice.snapshot()["detail"] == "Say yes or no")
    assert "I need your approval to Move old.txt to the trash" in rig.speaker.played[0]
    rig.mic.push(SPEECH, SILENCE)
    await until(lambda: kinds(rig, "approval_resolved"))
    resolved = kinds(rig, "approval_resolved")[0]
    assert resolved["approved"] is False and resolved["by"] == "voice"
    assert (rig.workspace / "old.txt").exists()


async def test_a_casual_yes_cannot_approve_a_high_risk_action(rig):
    (rig.workspace / "old.txt").write_text("x")
    request = asyncio.create_task(rig.core.services.approvals.request(tool="t", title="erase a disk", detail="", risk="high", reasons=[]))
    await asyncio.sleep(0.05)
    rig.stt.script.extend(["yes", "confirm"])
    await rig.voice.push_to_talk()
    rig.mic.push(SPEECH, SILENCE)
    await until(lambda: any("high risk" in p for p in rig.speaker.played))
    assert not request.done()
    await until(lambda: rig.voice.snapshot()["detail"] == "Say yes or no")
    rig.mic.push(SPEECH, SILENCE)
    assert (await asyncio.wait_for(request, 3)).approved


async def test_saying_stop_halts_everything(rig):
    rig.stt.script.append("Jarvis, stop")
    rig.mic.push(WAKE, SPEECH, SILENCE)
    await until(lambda: kinds(rig, "estop"))
    assert kinds(rig, "user_message") == []


async def test_talk_button_pause_and_resume(rig):
    await rig.voice.push_to_talk()
    assert rig.voice.snapshot()["state"] == "listening"
    await rig.voice.push_to_talk()                       # pressing again cancels
    assert rig.voice.snapshot()["state"] == "idle"
    await rig.voice.pause()
    assert rig.voice.snapshot()["state"] == "paused" and not rig.mic.running      # the microphone is really closed
    await rig.voice.resume()
    assert rig.voice.snapshot()["state"] == "idle" and rig.mic.running


async def test_reminders_are_spoken(rig):
    rig.core.store.add_reminder("submit the essay", 1.0)
    rig.core.scheduler.tick()
    await until(lambda: rig.speaker.played)
    assert rig.speaker.played == ["Reminder: submit the essay"]


async def test_wake_word_interrupts_speech(rig):
    rig.mic.push(WAKE)
    await until(lambda: rig.voice.snapshot()["state"] == "listening")
    assert rig.speaker.stops >= 1


async def test_voice_failure_leaves_the_assistant_usable(paths, home, workspace):
    cfg = Config()
    cfg.brain.backend = "demo"
    cfg.safety.approved_dirs = [str(workspace)]

    class BrokenMic(FakeMic):
        def start(self, sink):
            from jarvis.voice.audio import AudioUnavailable
            raise AudioUnavailable("no microphone found")

    core = JarvisCore(paths, cfg)
    await core.start()
    voice = VoiceController(core, engines=Engines(FakeWake(), FakeVad(), FakeStt(), FakeTts()), microphone=BrokenMic(), speaker=FakeSpeaker())
    await voice.start()
    await until(lambda: voice.snapshot()["state"] == "error")
    assert "no microphone" in voice.snapshot()["detail"]
    assert (await core.assistant.ask("list files")).text
    await voice.stop()
    await core.close()


# ------------------------------------------------------------ real models

MODELS = os.environ.get("JARVIS_TEST_MODELS")


@pytest.mark.live
@pytest.mark.skipif(not MODELS, reason="set JARVIS_TEST_MODELS to a folder for the real speech models")
async def test_real_engines_round_trip(paths, home, workspace, monkeypatch):
    """Synthesise a spoken request with the real voice, then let the real wake word and recogniser hear it."""
    pytest.importorskip("sherpa_onnx")
    pytest.importorskip("piper")
    from jarvis.voice.engines import Synthesizer, Transcriber, VoiceActivity, WakeDetector
    from jarvis.voice.models import ModelStore

    store = ModelStore(Path(MODELS))
    stt_key = Config().voice.stt_model
    folders = {key: await store.ensure(key) for key in ("kws", "vad", stt_key, "en_GB-alan-medium")}
    tts = Synthesizer(folders["en_GB-alan-medium"] / "en_GB-alan-medium.onnx")
    engines = Engines(
        wake=WakeDetector(folders["kws"], "jarvis", "medium"),
        vad=VoiceActivity(folders["vad"] / "silero_vad.onnx", 0.8),
        stt=Transcriber(folders[stt_key]),
        tts=tts,
    )

    def spoken(text: str):
        pcm, rate = tts.synthesize(text)
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        count = int(len(samples) * 16000 / rate)
        return np.interp(np.linspace(0, len(samples) - 1, count), np.arange(len(samples)), samples).astype(np.float32)

    cfg = Config()
    cfg.brain.backend = "demo"
    cfg.safety.approved_dirs = [str(workspace)]
    cfg.voice.followup_seconds = 0
    cfg.voice.start_timeout_seconds = 1.0
    core = JarvisCore(paths, cfg)
    await core.start()
    speaker, mic = FakeSpeaker(), FakeMic()
    speaker.play = lambda pcm, rate: speaker.played.append(f"{len(pcm)} bytes at {rate} Hz")
    voice = VoiceController(core, engines=engines, microphone=mic, speaker=speaker)
    seen: list[dict] = []
    q = core.bus.subscribe()
    await voice.start()
    await until(lambda: voice.snapshot()["state"] == "idle")

    async def play_into_microphone(audio):
        for start in range(0, len(audio), 1600):
            mic.push(audio[start:start + 1600])
            await asyncio.sleep(0)
        await asyncio.sleep(1.5)                    # let recognition and the reply finish
        while not q.empty():
            seen.append(q.get_nowait())

    quiet = np.zeros(16000, dtype=np.float32)
    # words that do not contain the wake word must not wake it
    await play_into_microphone(np.concatenate([quiet, spoken("The service at the restaurant was quite good this evening."), quiet, quiet]))
    assert not [e for e in seen if e["type"] == "voice_wake"]

    # A spoken request does. The synthetic voice varies from one rendering to the next (as people do),
    # so allow a few attempts and require that one is heard correctly from wake word to spoken reply.
    import difflib
    heard: list[str] = []
    for _ in range(4):
        await play_into_microphone(np.concatenate([spoken("Jarvis, what time is it?"), quiet, quiet, quiet]))
        heard = [e["text"] for e in seen if e["type"] == "user_message"]
        if any(difflib.SequenceMatcher(None, h.lower().strip(" ?.!"), "what time is it").ratio() > 0.8 for h in heard):
            break
    print("recognised:", heard)
    assert [e for e in seen if e["type"] == "voice_wake"], "the wake word was never detected"
    assert any(difflib.SequenceMatcher(None, h.lower().strip(" ?.!"), "what time is it").ratio() > 0.8 for h in heard), heard
    assert not any(h.lower().startswith("jarvis") for h in heard)       # the wake word itself is not part of the request
    await until(lambda: speaker.played, timeout=20)
    assert "bytes at 22050 Hz" in speaker.played[0]
    await voice.stop()
    await core.close()
