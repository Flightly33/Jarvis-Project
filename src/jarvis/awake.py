"""Keeping the computer from going to sleep on its own while Jarvis is running.

Windows only. The request belongs to the thread that makes it and lapses when that thread
ends, so a small thread holds it. It does not override closing the lid or choosing Sleep.
"""

from __future__ import annotations

import sys
import threading

_CONTINUOUS = 0x80000000
_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    def __init__(self) -> None:
        self._release = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def set(self, on: bool) -> None:
        if not self.supported or on == self.active:
            return
        if on:
            self._release.clear()
            self._thread = threading.Thread(target=self._hold, name="keep-awake", daemon=True)
            self._thread.start()
        else:
            self._release.set()
            self._thread.join(timeout=2)
            self._thread = None

    def _hold(self) -> None:
        import ctypes

        kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel.SetThreadExecutionState(_CONTINUOUS | _SYSTEM_REQUIRED)
        self._release.wait()
        kernel.SetThreadExecutionState(_CONTINUOUS)
