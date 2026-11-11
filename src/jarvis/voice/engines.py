"""The speech engines: wake-word detection, speech detection, recognition and synthesis.

Detection and recognition use sherpa-onnx (Apache-2.0); synthesis uses Piper
(GPL-3.0, an optional dependency loaded only when voice is switched on).
Everything runs locally: no audio leaves the computer.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import numpy as np

SAMPLE_RATE = 16_000
# (boost, trigger threshold) for the keyword spotter: higher boost / lower threshold triggers more easily.
SENSITIVITY = {"low": (1.0, 0.25), "medium": (2.0, 0.12), "high": (3.0, 0.06)}
_KWS_STEM = "epoch-12-avg-2-chunk-16-left-64.onnx"
_KNOWN_TOKENS = {"jarvis": "▁JA R VI S", "hey jarvis": "▁HE Y ▁JA R VI S"}


class EngineError(Exception):
    pass


def keyword_tokens(phrase: str, bpe_model: Path) -> str:
    """Spell a wake phrase in the sub-word pieces the keyword model listens for."""
    phrase = " ".join(phrase.lower().split())
    try:
        import sentencepiece
    except ImportError:
        if phrase in _KNOWN_TOKENS:
            return _KNOWN_TOKENS[phrase]
        raise EngineError("A custom wake word needs the 'sentencepiece' package.") from None
    processor = sentencepiece.SentencePieceProcessor(model_file=str(bpe_model))
    return " ".join(processor.encode(phrase.upper(), out_type=str))


class WakeDetector:
    def __init__(self, model_dir: Path, phrase: str, sensitivity: str = "medium") -> None:
        import sherpa_onnx

        boost, threshold = SENSITIVITY[sensitivity]
        tokens = keyword_tokens(phrase, model_dir / "bpe.model")
        label = phrase.upper().replace(" ", "_")
        # the keyword file is tiny and only read at start-up
        self._keywords = Path(tempfile.mkdtemp(prefix="jarvis-kws-")) / "keywords.txt"
        self._keywords.write_text(f"{tokens} @{label}\n", encoding="utf-8")
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(model_dir / "tokens.txt"),
            encoder=str(model_dir / f"encoder-{_KWS_STEM}"),
            decoder=str(model_dir / f"decoder-{_KWS_STEM}"),
            joiner=str(model_dir / f"joiner-{_KWS_STEM}"),
            keywords_file=str(self._keywords),
            num_threads=1, keywords_score=boost, keywords_threshold=threshold, provider="cpu",
        )
        self._stream = self._spotter.create_stream()

    def feed(self, samples: np.ndarray) -> bool:
        """Feed live audio; True when the wake word has just been heard."""
        self._stream.accept_waveform(SAMPLE_RATE, samples)
        heard = False
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
            if self._spotter.get_result(self._stream):
                heard = True
                self._spotter.reset_stream(self._stream)
        return heard

    def reset(self) -> None:
        self._stream = self._spotter.create_stream()


class VoiceActivity:
    """Tells speech from silence, with a short hold so brief pauses do not end an utterance."""

    def __init__(self, model_file: Path, end_silence: float = 0.8) -> None:
        import sherpa_onnx

        self._sherpa = sherpa_onnx
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(model_file)
        cfg.silero_vad.threshold = 0.5
        cfg.silero_vad.min_silence_duration = end_silence
        cfg.silero_vad.min_speech_duration = 0.15
        cfg.sample_rate = SAMPLE_RATE
        self._cfg = cfg
        self._window = cfg.silero_vad.window_size
        self._vad: Any = None
        self._rest = np.zeros(0, dtype=np.float32)
        self.reset()

    def reset(self) -> None:
        self._vad = self._sherpa.VoiceActivityDetector(self._cfg, buffer_size_in_seconds=60)
        self._rest = np.zeros(0, dtype=np.float32)

    def feed(self, samples: np.ndarray) -> bool:
        """Feed audio; returns whether someone is currently speaking."""
        data = np.concatenate([self._rest, samples.astype(np.float32, copy=False)])
        usable = len(data) - len(data) % self._window
        for start in range(0, usable, self._window):
            self._vad.accept_waveform(data[start:start + self._window])
        self._rest = data[usable:]
        while not self._vad.empty():       # finished segments are not needed; keep the queue from growing
            self._vad.pop()
        return bool(self._vad.is_speech_detected())


class Transcriber:
    def __init__(self, model_dir: Path, threads: int = 2) -> None:
        import sherpa_onnx

        self._recognizer = sherpa_onnx.OfflineRecognizer.from_moonshine(
            preprocessor=str(model_dir / "preprocess.onnx"),
            encoder=str(model_dir / "encode.int8.onnx"),
            uncached_decoder=str(model_dir / "uncached_decode.int8.onnx"),
            cached_decoder=str(model_dir / "cached_decode.int8.onnx"),
            tokens=str(model_dir / "tokens.txt"),
            num_threads=threads,
        )

    def transcribe(self, samples: np.ndarray) -> str:
        if len(samples) < SAMPLE_RATE // 5:        # under 0.2 s there is nothing to recognise
            return ""
        stream = self._recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples.astype(np.float32, copy=False))
        self._recognizer.decode_stream(stream)
        return stream.result.text.strip()


class Synthesizer:
    def __init__(self, model_file: Path, speed: float = 1.0, volume: float = 1.0) -> None:
        try:
            from piper import PiperVoice, SynthesisConfig
        except ImportError as exc:
            raise EngineError("Speech output needs the 'piper-tts' package.") from exc
        config = model_file.with_name(model_file.name + ".json")
        self._voice = PiperVoice.load(str(model_file), config_path=str(config))
        self._make_config = SynthesisConfig
        self.speed = speed
        self.volume = volume

    def synthesize(self, text: str) -> tuple[bytes, int]:
        """Return (16-bit mono audio, sample rate) for a piece of text."""
        settings = self._make_config(length_scale=1.0 / max(0.25, self.speed), volume=self.volume)
        audio, rate = bytearray(), 22_050
        for chunk in self._voice.synthesize(text, syn_config=settings):
            audio += chunk.audio_int16_bytes
            rate = chunk.sample_rate
        return bytes(audio), rate
