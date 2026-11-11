"""Microphone input and speaker output.

Preferred route: the `sounddevice` library (its Windows and macOS packages
include the audio driver layer). On Linux, where that needs an extra system
package, Jarvis falls back to the desktop's own recording and playback
commands, which every PulseAudio or PipeWire system has.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import threading
from collections.abc import Callable
from typing import Any, Protocol

import numpy as np

from jarvis.config import VoiceConfig

SAMPLE_RATE = 16_000
CHUNK = 1_600                     # 100 ms of audio
ChunkSink = Callable[[np.ndarray], None]

_RECORDERS = (
    ("parec", "parec --raw --rate=16000 --channels=1 --format=s16le --latency-msec=40"),
    ("pw-record", "pw-record --rate 16000 --channels 1 --format s16 -"),
    ("arecord", "arecord -q -t raw -f S16_LE -r 16000 -c 1"),
)
_PLAYERS = (
    ("paplay", "paplay --raw --rate={rate} --channels=1 --format=s16le"),
    ("pw-play", "pw-play --rate {rate} --channels 1 --format s16 -"),
    ("aplay", "aplay -q -t raw -f S16_LE -r {rate} -c 1"),
)


class AudioUnavailable(Exception):
    """No way to record or play sound was found; the message says what to do."""


class Microphone(Protocol):
    def start(self, sink: ChunkSink) -> None: ...
    def stop(self) -> None: ...


class Speaker(Protocol):
    def play(self, pcm: bytes, rate: int) -> None: ...     # returns when playback finished or was stopped
    def stop(self) -> None: ...


def _sounddevice() -> Any:
    try:
        import sounddevice
    except (ImportError, OSError) as exc:      # OSError: the audio driver layer (PortAudio) is missing
        raise AudioUnavailable(str(exc)) from exc
    return sounddevice


class SoundDeviceMicrophone:
    def __init__(self, device: str = "") -> None:
        self._sd = _sounddevice()
        self._device = _device(device)
        self._stream: Any = None

    def start(self, sink: ChunkSink) -> None:
        def callback(indata, frames, time, status) -> None:
            sink(indata[:, 0].copy())

        try:
            self._stream = self._sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=CHUNK, device=self._device, callback=callback,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001 - PortAudio reports many device problems as generic errors
            raise AudioUnavailable(f"The microphone could not be opened: {exc}") from exc

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001, S110 - closing a dead stream is not worth reporting
                pass


class SoundDeviceSpeaker:
    def __init__(self, device: str = "") -> None:
        self._sd = _sounddevice()
        self._device = _device(device)

    def play(self, pcm: bytes, rate: int) -> None:
        samples = np.frombuffer(pcm, dtype=np.int16)
        if samples.size:
            self._sd.play(samples, rate, device=self._device)
            self._sd.wait()

    def stop(self) -> None:
        self._sd.stop()


def _device(name: str) -> int | str | None:
    if not name:
        return None
    return int(name) if name.isdigit() else name


class CommandMicrophone:
    """Reads raw 16 kHz mono 16-bit audio from a command's standard output."""

    def __init__(self, command: str) -> None:
        self._argv = shlex.split(command)
        self._proc: subprocess.Popen | None = None

    def start(self, sink: ChunkSink) -> None:
        try:
            self._proc = subprocess.Popen(self._argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)  # noqa: S603
        except OSError as exc:
            raise AudioUnavailable(f"The recording command could not be started: {exc}") from exc
        proc = self._proc

        def pump() -> None:
            assert proc.stdout is not None
            while data := proc.stdout.read(CHUNK * 2):
                usable = len(data) - len(data) % 2
                sink(np.frombuffer(data[:usable], dtype=np.int16).astype(np.float32) / 32768.0)

        threading.Thread(target=pump, daemon=True, name="jarvis-mic").start()

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()


class CommandSpeaker:
    """Plays raw mono 16-bit audio by piping it into a command."""

    def __init__(self, command: str) -> None:
        self._command = command
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def play(self, pcm: bytes, rate: int) -> None:
        argv = shlex.split(self._command.replace("{rate}", str(rate)))
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
        except OSError as exc:
            raise AudioUnavailable(f"The playback command could not be started: {exc}") from exc
        with self._lock:
            self._proc = proc
        try:
            proc.communicate(pcm)
        except (BrokenPipeError, OSError):
            pass
        finally:
            with self._lock:
                self._proc = None

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.kill()


def _first_available(options: tuple[tuple[str, str], ...]) -> str | None:
    return next((command for program, command in options if shutil.which(program)), None)


def open_microphone(cfg: VoiceConfig) -> Microphone:
    if cfg.input_backend == "command" or (cfg.input_backend == "auto" and cfg.input_command):
        if not cfg.input_command:
            raise AudioUnavailable("voice.input_command is empty.")
        return CommandMicrophone(cfg.input_command)
    try:
        return SoundDeviceMicrophone(cfg.input_device)
    except AudioUnavailable as exc:
        command = _first_available(_RECORDERS) if cfg.input_backend == "auto" else None
        if command:
            return CommandMicrophone(command)
        raise AudioUnavailable(
            "No way to record audio was found. Install the voice extra (pip install \"jarvis-assistant[voice]\"; "
            f"on Linux also the system package libportaudio2). Details: {exc}"
        ) from exc


def open_speaker(cfg: VoiceConfig) -> Speaker:
    if cfg.output_backend == "command" or (cfg.output_backend == "auto" and cfg.output_command):
        if not cfg.output_command:
            raise AudioUnavailable("voice.output_command is empty.")
        return CommandSpeaker(cfg.output_command)
    try:
        return SoundDeviceSpeaker(cfg.output_device)
    except AudioUnavailable as exc:
        command = _first_available(_PLAYERS) if cfg.output_backend == "auto" else None
        if command:
            return CommandSpeaker(command)
        raise AudioUnavailable(f"No way to play audio was found. Details: {exc}") from exc
