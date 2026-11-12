"""Voice messages for the phone link: turn a recorded message into text, and a reply into a voice message.

Everything runs on this computer with the same speech models the microphone uses.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from jarvis.voice.audio import SAMPLE_RATE
from jarvis.voice.text import speakable

if TYPE_CHECKING:
    from jarvis.core import JarvisCore

MAX_SECONDS = 120
MAX_SPOKEN_CHARS = 1200
_OPUS_RATE = 48_000


class NoteError(Exception):
    pass


def _av() -> Any:
    try:
        import av
    except ImportError as exc:
        raise NoteError("Voice messages need the 'av' package, which is not installed.") from exc
    return av


def decode(data: bytes) -> np.ndarray:
    """Decode a recorded message (Telegram sends Ogg/Opus) to 16 kHz mono samples in -1..1."""
    av = _av()
    try:
        with av.open(io.BytesIO(data)) as container:
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            parts = [out.to_ndarray().reshape(-1) for frame in container.decode(audio=0) for out in resampler.resample(frame)]
            parts += [out.to_ndarray().reshape(-1) for out in resampler.resample(None)]
    except NoteError:
        raise
    except Exception as exc:  # noqa: BLE001 - whatever the decoder dislikes, the message is unreadable
        raise NoteError("That recording could not be read.") from exc
    if not parts:
        return np.zeros(0, dtype=np.float32)
    samples = np.concatenate(parts).astype(np.float32) / 32768.0
    return samples[: MAX_SECONDS * SAMPLE_RATE]


def encode(pcm: bytes, rate: int) -> bytes:
    """Pack 16-bit mono audio as Ogg/Opus, which Telegram shows as a playable voice message."""
    av = _av()
    out = io.BytesIO()
    samples = np.frombuffer(pcm, dtype=np.int16).reshape(1, -1)
    with av.open(out, "w", format="ogg") as container:
        stream = container.add_stream("libopus", rate=_OPUS_RATE, layout="mono")
        stream.bit_rate = 32_000
        resampler = av.AudioResampler(format="s16", layout="mono", rate=_OPUS_RATE, frame_size=960)
        frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(samples), format="s16", layout="mono")
        frame.sample_rate = rate
        for chunk in [*resampler.resample(frame), *resampler.resample(None)]:
            container.mux(stream.encode(chunk))
        container.mux(stream.encode(None))
    return out.getvalue()


class NoteSpeech:
    """Speech recognition and synthesis for voice messages, loaded the first time one arrives."""

    def __init__(self, core: JarvisCore) -> None:
        self.core = core
        self._own: tuple[Any, Any] | None = None
        self._lock = asyncio.Lock()

    async def _engines(self) -> tuple[Any, Any]:
        shared = self.core.shared_engines           # the microphone's engines, when voice is running
        if shared is not None:
            return shared.stt, shared.tts
        async with self._lock:
            if self._own is None:
                try:
                    from jarvis.voice.engines import Synthesizer, Transcriber
                    from jarvis.voice.models import ModelStore

                    cfg = self.core.config.voice
                    store = ModelStore(self.core.paths.models_dir)
                    stt = await store.ensure(cfg.stt_model)
                    voice_file = Path(cfg.tts_voice).expanduser()
                    if voice_file.suffix != ".onnx":
                        voice_file = (await store.ensure(cfg.tts_voice)) / f"{cfg.tts_voice}.onnx"
                    self._own = await asyncio.to_thread(
                        lambda: (Transcriber(stt), Synthesizer(voice_file, cfg.tts_speed, cfg.tts_volume, cfg.tts_expressiveness)))
                except NoteError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise NoteError(f"Voice messages are unavailable: {exc}") from exc
        return self._own

    async def transcribe(self, recording: bytes) -> str:
        stt, _ = await self._engines()
        samples = await asyncio.to_thread(decode, recording)
        return await asyncio.to_thread(stt.transcribe, samples)

    async def speak(self, text: str) -> bytes | None:
        """A voice message saying `text`, or None when there is nothing worth saying aloud."""
        text = speakable(text)
        if not text:
            return None
        if len(text) > MAX_SPOKEN_CHARS:
            text = text[:MAX_SPOKEN_CHARS].rsplit(" ", 1)[0] + ". The rest is in the written message."
        _, tts = await self._engines()
        pcm, rate = await asyncio.to_thread(tts.synthesize, text)
        return await asyncio.to_thread(encode, pcm, rate) if pcm else None
