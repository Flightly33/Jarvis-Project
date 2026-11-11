"""The voice loop: wake word -> listen -> understand -> act -> speak -> keep listening.

One task reads the microphone. While idle it only runs the wake-word
detector. Once woken (or when the Talk button is pressed) it records until
the speaker stops, turns the recording into text and hands it to the
assistant, whose reply is spoken sentence by sentence as it is produced.
The wake word keeps working while Jarvis talks, so it can be interrupted.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from jarvis.voice.audio import SAMPLE_RATE, AudioUnavailable, Microphone, Speaker, open_microphone, open_speaker
from jarvis.voice.engines import EngineError, Synthesizer, Transcriber, VoiceActivity, WakeDetector
from jarvis.voice.models import ModelError, ModelStore
from jarvis.voice.text import SentenceChunker, local_intent, speakable, strip_wake_word

if TYPE_CHECKING:
    from jarvis.core import JarvisCore

log = logging.getLogger(__name__)

_PREROLL_SECONDS = 1.5
_APPROVAL_WINDOW = 12.0


@dataclass
class Engines:
    wake: Any          # .feed(samples) -> bool, .reset()
    vad: Any           # .feed(samples) -> bool, .reset()
    stt: Any           # .transcribe(samples) -> str
    tts: Any           # .synthesize(text) -> (pcm bytes, rate)


class UtteranceCapture:
    """Records one spoken request, deciding from speech activity when it has begun and ended."""

    def __init__(self, vad: Any, start_timeout: float, max_seconds: float, preroll: np.ndarray | None = None) -> None:
        self._vad = vad
        self._start_timeout = start_timeout
        self._max = max_seconds
        self._chunks: list[np.ndarray] = []
        self._heard = 0
        self.speech_started = False
        vad.reset()
        if preroll is not None and len(preroll):
            self._chunks.append(preroll)
            self.speech_started = bool(vad.feed(preroll))

    def feed(self, chunk: np.ndarray) -> str | None:
        """Add live audio. Returns "done" when the request is complete, "timeout" if nobody spoke."""
        self._chunks.append(chunk)
        self._heard += len(chunk)
        speaking = self._vad.feed(chunk)
        if speaking:
            self.speech_started = True
        elif self.speech_started:
            return "done"
        seconds = self._heard / SAMPLE_RATE
        if not self.speech_started and seconds >= self._start_timeout:
            return "timeout"
        if seconds >= self._max:
            return "done"
        return None

    def audio(self) -> np.ndarray:
        return np.concatenate(self._chunks) if self._chunks else np.zeros(0, dtype=np.float32)


class SpeechOutput:
    """Speaks queued sentences in order; can be cut off at any moment."""

    def __init__(self, tts: Any, speaker: Speaker, on_change) -> None:
        self._tts = tts
        self._speaker = speaker
        self._on_change = on_change
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._generation = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self.active = False
        self._worker: asyncio.Task | None = None

    def say(self, text: str) -> None:
        text = speakable(text)
        if not text:
            return
        self._idle.clear()
        self._queue.put_nowait(text)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    def stop(self) -> None:
        self._generation += 1
        while not self._queue.empty():
            self._queue.get_nowait()
        with contextlib.suppress(Exception):
            self._speaker.stop()

    async def drain(self) -> None:
        await self._idle.wait()

    async def close(self) -> None:
        self.stop()
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker

    async def _run(self) -> None:
        try:
            while not self._queue.empty():
                text = self._queue.get_nowait()
                generation = self._generation
                try:
                    pcm, rate = await asyncio.to_thread(self._tts.synthesize, text)
                    if generation != self._generation:
                        continue                       # interrupted while the audio was being prepared
                    self._set_active(True)
                    await asyncio.to_thread(self._speaker.play, pcm, rate)
                except Exception:  # noqa: BLE001 - a failed sentence must not silence the rest
                    log.exception("could not speak a sentence")
        finally:
            self._set_active(False)
            self._idle.set()

    def _set_active(self, value: bool) -> None:
        if self.active != value:
            self.active = value
            self._on_change()


class VoiceController:
    def __init__(self, core: JarvisCore, *, engines: Engines | None = None, microphone: Microphone | None = None, speaker: Speaker | None = None) -> None:
        self.core = core
        self._given = (engines, microphone, speaker)
        self._engines: Engines | None = None
        self._mic: Microphone | None = None
        self._speech: SpeechOutput | None = None
        self._chunks: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self._preroll: deque[np.ndarray] = deque(maxlen=int(_PREROLL_SECONDS * 10))
        self._capture: UtteranceCapture | None = None
        self._mode = ""
        self._state = "off"
        self._detail = ""
        self._paused = False
        self._voice_turn = False
        self._tasks: list[asyncio.Task] = []
        self._converse: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ----------------------------------------------------------------- status

    @property
    def cfg(self):
        return self.core.config.voice

    def snapshot(self) -> dict[str, Any]:
        state = self._state
        if self._engines is not None and not self._paused:
            if self._capture is not None:
                state = "listening"
            elif self._speech is not None and self._speech.active:
                state = "speaking"
        return {
            "state": state, "detail": self._detail, "available": self._engines is not None,
            "wake_enabled": self.cfg.wake_enabled, "wake_word": self.cfg.wake_word,
        }

    def _publish(self) -> None:
        self.core.bus.emit("voice", **self.snapshot())

    def _set(self, state: str, detail: str = "") -> None:
        self._state, self._detail = state, detail
        self._publish()

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if not self.cfg.enabled:
            self._set("off")
            return
        self._tasks.append(asyncio.create_task(self._boot()))

    async def _boot(self) -> None:
        engines, microphone, speaker = self._given
        try:
            self._set("loading", "Getting the speech models ready…")
            if engines is None:
                engines = await self._load_engines()
            speaker = speaker or await asyncio.to_thread(open_speaker, self.cfg)
            microphone = microphone or await asyncio.to_thread(open_microphone, self.cfg)
            microphone.start(self._on_chunk)
        except (ModelError, EngineError, AudioUnavailable, ImportError, OSError) as exc:
            log.warning("voice unavailable: %s", exc)
            self._set("error", f"Voice is unavailable: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - voice must never take the rest of the assistant down
            log.exception("voice failed to start")
            self._set("error", f"Voice failed to start: {type(exc).__name__}: {exc}")
            return
        self._engines, self._mic = engines, microphone
        self._speech = SpeechOutput(engines.tts, speaker, self._publish)
        self._tasks.append(asyncio.create_task(self._listen()))
        self._tasks.append(asyncio.create_task(self._watch_events()))
        self._set("idle")

    async def _load_engines(self) -> Engines:
        store = ModelStore(self.core.paths.models_dir)
        progress = lambda message: self._loop.call_soon_threadsafe(self._set, "loading", message)  # noqa: E731
        kws = await store.ensure("kws", progress)
        vad = await store.ensure("vad", progress)
        stt = await store.ensure(self.cfg.stt_model, progress)
        voice_file = Path(self.cfg.tts_voice).expanduser()
        if voice_file.suffix != ".onnx":       # a catalogue voice rather than a file the user supplied
            voice_file = (await store.ensure(self.cfg.tts_voice, progress)) / f"{self.cfg.tts_voice}.onnx"
        self._set("loading", "Starting the speech engines…")

        def build() -> Engines:
            return Engines(
                wake=WakeDetector(kws, self.cfg.wake_word, self.cfg.wake_sensitivity),
                vad=VoiceActivity(vad / "silero_vad.onnx", self.cfg.end_silence_seconds),
                stt=Transcriber(stt),
                tts=Synthesizer(voice_file, self.cfg.tts_speed, self.cfg.tts_volume),
            )

        return await asyncio.to_thread(build)

    async def stop(self) -> None:
        if self._mic is not None:
            with contextlib.suppress(Exception):
                self._mic.stop()
        if self._converse is not None:
            self._converse.cancel()
        for task in self._tasks:
            task.cancel()
        for task in [*self._tasks, self._converse]:
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._tasks.clear()
        if self._speech is not None:
            await self._speech.close()
        self._engines = self._mic = self._speech = self._capture = self._converse = None
        self._paused = False
        self._state, self._detail = "off", ""

    async def reconfigure(self) -> None:
        """Apply changed voice settings by restarting the voice loop."""
        await self.stop()
        await self.start()
        self._publish()

    # --------------------------------------------------------------- controls

    async def push_to_talk(self) -> None:
        if self._engines is None or self._paused:
            return
        if self._capture is not None:           # pressing again cancels
            self._capture = None
            self._publish()
            return
        self._speech.stop()
        self._begin("manual", self.cfg.start_timeout_seconds)

    async def silence(self) -> None:
        if self._speech is not None:
            self._speech.stop()
        self._capture = None
        self._publish()

    async def pause(self) -> None:
        if self._engines is None or self._paused:
            return
        self._paused = True
        await self.silence()
        self._mic.stop()                         # the microphone is really closed, not just ignored
        self._set("paused")

    async def resume(self) -> None:
        if self._engines is None or not self._paused:
            return
        try:
            self._mic.start(self._on_chunk)
        except AudioUnavailable as exc:
            self._set("error", str(exc))
            return
        self._paused = False
        self._engines.wake.reset()
        self._set("idle")

    async def say(self, text: str) -> None:
        if self._speech is not None and not self._paused:
            self._speech.say(text)

    # ------------------------------------------------------------- microphone

    def _on_chunk(self, chunk: np.ndarray) -> None:
        """Called from the audio thread."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._chunks.put_nowait, chunk)

    async def _listen(self) -> None:
        engines = self._engines
        while True:
            chunk = await self._chunks.get()
            if self._paused:
                continue
            self._preroll.append(chunk)
            try:
                if self._capture is not None:
                    self.core.bus.emit("voice_level", level=float(np.sqrt(np.mean(np.square(chunk)))) if len(chunk) else 0.0)
                    verdict = self._capture.feed(chunk)
                    if verdict:
                        await self._finish(verdict)
                elif self.cfg.wake_enabled and engines.wake.feed(chunk):
                    self._speech.stop()                    # speaking over Jarvis interrupts it
                    engines.wake.reset()
                    self.core.bus.emit("voice_wake")
                    self._begin("wake", self.cfg.start_timeout_seconds, np.concatenate(list(self._preroll)))
            except Exception:  # noqa: BLE001 - one bad chunk must not end listening for good
                log.exception("voice loop error")
                self._capture = None
                self._publish()

    def _begin(self, mode: str, start_timeout: float, preroll: np.ndarray | None = None) -> None:
        self._mode = mode
        self._capture = UtteranceCapture(self._engines.vad, start_timeout, self.cfg.max_utterance_seconds, preroll)
        self._detail = {"followup": "Still listening", "approval": "Say yes or no"}.get(mode, "Go ahead")
        self._publish()

    async def _finish(self, verdict: str) -> None:
        capture, mode = self._capture, self._mode
        self._capture = None
        if verdict == "timeout":
            self._set("idle")
            return
        self._set("transcribing")
        text = await asyncio.to_thread(self._engines.stt.transcribe, capture.audio())
        text, _ = strip_wake_word(text, self.cfg.wake_word)
        if not text:
            if mode == "wake":                  # only the name was said: wait for the request itself
                self._state = "idle"
                self._begin("command", self.cfg.start_timeout_seconds)
            else:
                self._set("idle")
            return
        self._set("idle")
        await self._dispatch(text, mode)

    async def _dispatch(self, text: str, mode: str) -> None:
        intent = local_intent(text)
        approvals = self.core.services.approvals
        if approvals.pending() and self.core.config.safety.voice_approval and intent in {"yes", "confirm", "no", "stop"}:
            approve = intent in {"yes", "confirm"}
            answered = approvals.resolve_latest(approve, by="voice", allow_high_risk=intent == "confirm")
            if answered is None and approve:
                self._speech.say("That one is high risk. Say confirm to approve it, or no to decline.")
                await self._speech.drain()
                self._begin("approval", _APPROVAL_WINDOW)
            return
        if mode == "approval":
            return                                # unclear answer: the request stays open on screen
        if intent == "stop":
            self._speech.stop()
            await self.core.emergency_stop("voice")
            return
        if intent == "nevermind":
            return
        if intent == "pause":
            await self.pause()
            return
        if self.core.assistant.busy:              # a new request replaces the one in progress
            await self.core.assistant.interrupt()
        if self._converse is not None and not self._converse.done():
            self._converse.cancel()
        self._converse = asyncio.create_task(self._talk(text, "voice-followup" if mode == "followup" else "voice"))

    async def _talk(self, text: str, source: str) -> None:
        speech = self._speech
        chunker = SentenceChunker()

        def on_text(delta: str) -> None:
            for sentence in chunker.feed(delta):
                speech.say(sentence)

        self._voice_turn = True
        try:
            result = await self.core.assistant.ask(text, source, on_text=on_text)
        finally:
            self._voice_turn = False
        for sentence in chunker.flush():
            speech.say(sentence)
        if result.error:
            speech.say("I ran into a problem. The details are on screen.")
        await speech.drain()
        replied = bool(result.text or result.error)
        if replied and not result.interrupted and self.cfg.followup_seconds > 0 and not self._paused and self._capture is None:
            self._begin("followup", self.cfg.followup_seconds)

    # ----------------------------------------------------------------- events

    async def _watch_events(self) -> None:
        """Speak the things that should be said aloud: reminders, approvals, finished background work."""
        queue = self.core.bus.subscribe()
        try:
            while True:
                event = await queue.get()
                kind = event["type"]
                if kind == "reminder":
                    await self.say(f"Reminder: {event['text']}")
                elif kind == "job" and event.get("finished") and event["job"]["status"] != "stopped":
                    ok = event["job"]["status"] == "finished"
                    await self.say("Your background command has finished." if ok else "Your background command has failed.")
                elif kind == "approval_request" and self._voice_turn and self.core.config.safety.voice_approval:
                    request = event["request"]
                    high = request["risk"] == "high"
                    await self.say(f"I need your approval to {request['title']}. " + ("This is high risk. Say confirm, or no." if high else "Yes or no?"))
                    await self._speech.drain()
                    if self._capture is None and not self._paused and self.core.services.approvals.pending():
                        self._begin("approval", _APPROVAL_WINDOW)
        finally:
            self.core.bus.unsubscribe(queue)
