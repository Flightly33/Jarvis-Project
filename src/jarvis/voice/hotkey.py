"""A keyboard shortcut that works from any application (Windows): Ctrl+Alt+J starts listening."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)
_started = False


def start_hotkey(loop: asyncio.AbstractEventLoop, action: Callable[[], Awaitable[None]]) -> bool:
    """Register Ctrl+Alt+J system-wide. Returns False where that is not supported."""
    global _started
    if sys.platform != "win32" or _started:
        return _started
    _started = True

    def listen() -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        mod_alt, mod_control, mod_norepeat, wm_hotkey = 0x0001, 0x0002, 0x4000, 0x0312
        if not user32.RegisterHotKey(None, 1, mod_alt | mod_control | mod_norepeat, ord("J")):
            log.warning("the Ctrl+Alt+J shortcut is already used by another program")
            return
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message == wm_hotkey and not loop.is_closed():
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(action()))

    threading.Thread(target=listen, daemon=True, name="jarvis-hotkey").start()
    return True
