"""The speech models Jarvis uses, and fetching them on first use.

All models run on this computer. They are downloaded once from the public
release pages of the open-source projects that publish them.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
_KWS = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
_KWS_STEM = "epoch-12-avg-2-chunk-16-left-64.onnx"
_MOONSHINE = ("preprocess.onnx", "encode.int8.onnx", "uncached_decode.int8.onnx", "cached_decode.int8.onnx", "tokens.txt")

Progress = Callable[[str], None]


@dataclass(frozen=True)
class ModelSpec:
    key: str
    kind: str                  # kws | vad | stt | tts
    label: str
    url: str
    megabytes: int
    folder: str                # where it ends up inside the models directory
    files: tuple[str, ...]     # what must be present for the model to count as installed

    @property
    def is_archive(self) -> bool:
        return self.url.endswith(".tar.bz2")


def _piper(voice: str) -> ModelSpec:
    return ModelSpec(voice, "tts", f"voice {voice}", f"{_BASE}/tts-models/vits-piper-{voice}.tar.bz2", 67,
                     f"vits-piper-{voice}", (f"{voice}.onnx", f"{voice}.onnx.json"))


CATALOG: dict[str, ModelSpec] = {spec.key: spec for spec in [
    ModelSpec("kws", "kws", "wake-word detector", f"{_BASE}/kws-models/{_KWS}.tar.bz2", 18, _KWS,
              ("tokens.txt", "bpe.model", f"encoder-{_KWS_STEM}", f"decoder-{_KWS_STEM}", f"joiner-{_KWS_STEM}")),
    ModelSpec("vad", "vad", "speech detector", f"{_BASE}/asr-models/silero_vad.onnx", 1, "silero-vad", ("silero_vad.onnx",)),
    ModelSpec("moonshine-tiny-en", "stt", "speech recognition (compact)", f"{_BASE}/asr-models/sherpa-onnx-moonshine-tiny-en-int8.tar.bz2",
              108, "sherpa-onnx-moonshine-tiny-en-int8", _MOONSHINE),
    ModelSpec("moonshine-base-en", "stt", "speech recognition (more accurate)", f"{_BASE}/asr-models/sherpa-onnx-moonshine-base-en-int8.tar.bz2",
              251, "sherpa-onnx-moonshine-base-en-int8", _MOONSHINE),
    _piper("en_GB-alan-medium"),
    _piper("en_GB-northern_english_male-medium"),
]}


class ModelError(Exception):
    pass


class ModelStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(os.environ.get("JARVIS_MODELS_DIR") or root)

    def spec(self, key: str) -> ModelSpec:
        try:
            return CATALOG[key]
        except KeyError:
            raise ModelError(f"Unknown speech model '{key}'. Available: {', '.join(CATALOG)}") from None

    def path(self, key: str) -> Path:
        return self.root / self.spec(key).folder

    def ready(self, key: str) -> bool:
        folder = self.path(key)
        return all((folder / name).is_file() for name in self.spec(key).files)

    async def ensure(self, key: str, progress: Progress = lambda message: None) -> Path:
        """Return the model's folder, downloading and unpacking it first if needed."""
        spec = self.spec(key)
        folder = self.path(key)
        if self.ready(key):
            return folder
        self.root.mkdir(parents=True, exist_ok=True)
        part = self.root / f"{spec.folder}.download"
        try:
            await self._download(spec, part, progress)
            if spec.is_archive:
                progress(f"Unpacking the {spec.label}…")
                await asyncio.to_thread(_extract, part, self.root)
            else:
                folder.mkdir(parents=True, exist_ok=True)
                shutil.move(str(part), str(folder / spec.files[0]))
        except (httpx.HTTPError, OSError, tarfile.TarError) as exc:
            raise ModelError(f"Could not get the {spec.label}: {exc}") from exc
        finally:
            part.unlink(missing_ok=True)
        if not self.ready(key):
            raise ModelError(f"The {spec.label} download was incomplete. It will be retried next time.")
        return folder

    async def _download(self, spec: ModelSpec, target: Path, progress: Progress) -> None:
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0)) as client:
            async with client.stream("GET", spec.url) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length") or 0) or spec.megabytes * 1_000_000
                done, shown = 0, -1
                with target.open("wb") as fh:
                    async for chunk in response.aiter_bytes(1 << 16):
                        fh.write(chunk)
                        done += len(chunk)
                        percent = min(99, int(done * 100 / total))
                        if percent != shown:
                            shown = percent
                            progress(f"Downloading the {spec.label}: {percent}% of {total // 1_000_000} MB")


def _extract(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(destination, filter="data")     # "data" refuses paths that escape the destination  # noqa: S202
