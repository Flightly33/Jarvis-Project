"""Using the desktop the way the user does: look at the screen, then click, type, press keys and scroll.

Windows only. Positions are given in the pixels of the latest screenshot Jarvis took and are
mapped back to the real screen. Typing is refused while a command-line window is in front,
because that would sidestep the checks on shell commands.
"""

from __future__ import annotations

import asyncio
import ctypes
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass

from pydantic import Field

from jarvis.safety import Assessment, Risk
from jarvis.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult, Trimmed
from jarvis.tools.windows_input import TERMINALS

_DOWN_UP = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
_WHEEL, _KEYUP, _UNICODE, _EXTENDED = 0x0800, 0x0002, 0x0004, 0x0001
_MODIFIERS = {"control": 0x11, "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B}
_KEYS = {
    "enter": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B, "backspace": 0x08, "delete": 0x2E, "space": 0x20, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "arrowleft": 0x25, "arrowup": 0x26, "arrowright": 0x27, "arrowdown": 0x28, "left": 0x25, "up": 0x26,
    "right": 0x27, "down": 0x28, "period": 0xBE, "comma": 0xBC, "minus": 0xBD, "equal": 0xBB, "slash": 0xBF, "printscreen": 0x2C, "insert": 0x2D,
}
_EXTENDED_KEYS = {0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x5B}


@dataclass
class View:
    """How the latest screenshot maps onto the real screen."""

    left: int
    top: int
    width: int
    height: int
    shot_width: int
    shot_height: int

    def to_screen(self, x: int, y: int) -> tuple[int, int]:
        if not (0 <= x < self.shot_width and 0 <= y < self.shot_height):
            raise ToolError(f"({x}, {y}) is outside the screenshot, which is {self.shot_width} by {self.shot_height}.")
        return (self.left + round((x + 0.5) * self.width / self.shot_width), self.top + round((y + 0.5) * self.height / self.shot_height))


view: View | None = None


def parse_combo(combo: str) -> list[int]:
    """'Control+Shift+S' -> virtual key codes, modifiers first."""
    codes: list[int] = []
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ToolError("An empty key was given.")
    for part in parts[:-1]:
        if part not in _MODIFIERS:
            raise ToolError(f"'{part}' is not a modifier key (use Control, Shift, Alt or Win).")
        codes.append(_MODIFIERS[part])
    key = parts[-1]
    if key in _MODIFIERS:
        codes.append(_MODIFIERS[key])
    elif key in _KEYS:
        codes.append(_KEYS[key])
    elif len(key) == 1 and (key.isalpha() or key.isdigit()):
        codes.append(ord(key.upper()))
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 12:
        codes.append(0x6F + int(key[1:]))
    else:
        raise ToolError(f"Unknown key '{parts[-1]}'.")
    return codes


# ----------------------------------------------------------------- Windows plumbing

if sys.platform == "win32":
    class _Mouse(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("data", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]

    class _Keyboard(ctypes.Structure):
        _fields_ = [("vk", wintypes.WORD), ("scan", wintypes.WORD), ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]

    class _Union(ctypes.Union):
        _fields_ = [("mouse", _Mouse), ("keyboard", _Keyboard)]

    class _Input(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _Union)]

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int]
    _user32.SendInput.restype = wintypes.UINT
    try:        # so that screenshots, cursor positions and clicks all use the same real pixels
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001 - already set, or an older Windows
        pass

    def _send(*events: _Input) -> None:
        array = (_Input * len(events))(*events)
        if _user32.SendInput(len(events), array, ctypes.sizeof(_Input)) != len(events):
            raise ToolError("Windows blocked the input (the screen may be locked, or an administrator window is in front).")

    def _key(vk: int = 0, up: bool = False, char: int = 0) -> _Input:
        flags = (_KEYUP if up else 0) | (_UNICODE if char else 0) | (_EXTENDED if vk in _EXTENDED_KEYS else 0)
        return _Input(type=1, u=_Union(keyboard=_Keyboard(vk=vk, scan=char, flags=flags, time=0, extra=0)))

    def _button(flags: int, data: int = 0) -> _Input:
        return _Input(type=0, u=_Union(mouse=_Mouse(dx=0, dy=0, data=data, flags=flags, time=0, extra=0)))

    def screen_geometry() -> tuple[int, int, int, int]:
        metric = _user32.GetSystemMetrics
        return metric(76), metric(77), metric(78), metric(79)       # the whole desktop across all monitors

    def cursor() -> tuple[int, int]:
        point = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    def foreground_program() -> str:
        import psutil

        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(_user32.GetForegroundWindow(), ctypes.byref(pid))
        try:
            return psutil.Process(pid.value).name().lower().removesuffix(".exe")
        except Exception:  # noqa: BLE001
            return ""

    def click(x: int, y: int, button: str = "left", double: bool = False) -> None:
        _user32.SetCursorPos(x, y)
        time.sleep(0.08)
        down, up = _DOWN_UP[button]
        for _ in range(2 if double else 1):
            _send(_button(down), _button(up))
            time.sleep(0.06)

    def scroll(x: int, y: int, notches: int) -> None:
        _user32.SetCursorPos(x, y)
        time.sleep(0.05)
        _send(_button(_WHEEL, (notches * 120) & 0xFFFFFFFF))

    def type_text(text: str) -> None:
        for ch in text.replace("\r\n", "\n"):
            if ch == "\n":
                _send(_key(0x0D), _key(0x0D, up=True))
            else:
                units = ch.encode("utf-16-le")
                for i in range(0, len(units), 2):
                    unit = int.from_bytes(units[i:i + 2], "little")
                    _send(_key(char=unit), _key(char=unit, up=True))
            time.sleep(0.004)

    def press(codes: list[int]) -> None:
        _send(*[_key(c) for c in codes], *[_key(c, up=True) for c in reversed(codes)])
        time.sleep(0.05)


def _no_terminal() -> None:
    program = foreground_program()
    if program in TERMINALS:
        raise ToolError("A command-line window is in front. Jarvis does not type into those; use run_command instead, which checks the command first.")


def _need_view() -> View:
    if view is None:
        raise ToolError("Take a screenshot first (take_screenshot), then give positions as they appear in it.")
    return view


_WHY = ["This uses your mouse and keyboard, so it acts as you in whatever is on screen."]


class ClickInput(ToolInput):
    x: int = Field(ge=0, description="Horizontal position in the latest screenshot, in its pixels.")
    y: int = Field(ge=0, description="Vertical position in the latest screenshot, in its pixels.")
    button: Trimmed = Field(default="left", pattern="^(left|right|middle)$")
    double: bool = False
    purpose: str = Field(default="", max_length=200, description="What this click is for, in a few words, for the user to see.")


def _assess_click(a: ClickInput, ctx: ToolContext) -> Assessment:
    return Assessment(risk=Risk.MODIFY, title=f"{'Double-click' if a.double else 'Click'} on the screen at ({a.x}, {a.y})", detail=a.purpose,
                      reasons=_WHY, sends_external=True, grant_key="desktop-control")


async def _run_click(a: ClickInput, ctx: ToolContext) -> ToolResult:
    x, y = _need_view().to_screen(a.x, a.y)
    await asyncio.to_thread(click, x, y, a.button, a.double)
    return ToolResult({"clicked": [a.x, a.y], "next": "Take a screenshot to see the result."}, f"clicked at ({a.x}, {a.y})")


class TypeInput(ToolInput):
    text: str = Field(min_length=1, max_length=8000, description="Text to type wherever the cursor is in the window in front.")


def _assess_type(a: TypeInput, ctx: ToolContext) -> Assessment:
    return Assessment(risk=Risk.MODIFY, title="Type on the keyboard", detail=a.text[:600], reasons=_WHY, sends_external=True, grant_key="desktop-control")


async def _run_type(a: TypeInput, ctx: ToolContext) -> ToolResult:
    _no_terminal()
    await asyncio.to_thread(type_text, a.text)
    return ToolResult({"typed_characters": len(a.text)}, f"typed {len(a.text)} character(s)")


class KeysInput(ToolInput):
    keys: list[Trimmed] = Field(min_length=1, max_length=30, description="Keys or shortcuts to press in order, e.g. 'Control+S', 'Alt+Tab', 'Enter', 'Win'.")


def _assess_keys(a: KeysInput, ctx: ToolContext) -> Assessment:
    base = Assessment(risk=Risk.MODIFY, title="Press keys: " + ", ".join(a.keys)[:100], reasons=_WHY, sends_external=True, grant_key="desktop-control")
    try:
        for combo in a.keys:
            parse_combo(combo)
    except ToolError as exc:
        base.blocked = str(exc)
    return base


async def _run_keys(a: KeysInput, ctx: ToolContext) -> ToolResult:
    _no_terminal()
    for combo in a.keys:
        await asyncio.to_thread(press, parse_combo(combo))
    return ToolResult({"pressed": a.keys}, "pressed " + ", ".join(a.keys)[:60])


class ScrollInput(ToolInput):
    x: int = Field(ge=0, description="Where to scroll, in the latest screenshot's pixels.")
    y: int = Field(ge=0)
    notches: int = Field(ge=-30, le=30, description="Wheel steps: negative scrolls down, positive scrolls up.")


def _assess_scroll(a: ScrollInput, ctx: ToolContext) -> Assessment:
    return Assessment(risk=Risk.LOW, title="Scroll on the screen", grant_key="desktop-control")


async def _run_scroll(a: ScrollInput, ctx: ToolContext) -> ToolResult:
    x, y = _need_view().to_screen(a.x, a.y)
    await asyncio.to_thread(scroll, x, y, a.notches)
    return ToolResult({"scrolled": a.notches}, "scrolled")


CONTROL_TOOLS: list[Tool] = [
    Tool(
        "screen_click",
        "Click somewhere on the user's screen, in any application. Take a screenshot first and give the position as it appears in "
        "that screenshot; take another afterwards to see what happened. Prefer the tab_ tools for web pages and send_to_window for "
        "pasting - they are more reliable than clicking by position.",
        ClickInput, _run_click, _assess_click, group="desktop",
    ),
    Tool("screen_type", "Type text with the keyboard into whatever has the cursor in the window in front (click the field first). Never passwords.",
         TypeInput, _run_type, _assess_type, timeout=120, group="desktop"),
    Tool("screen_keys", "Press keys or keyboard shortcuts in the window in front, e.g. Control+S, Alt+Tab, Enter.", KeysInput, _run_keys, _assess_keys, group="desktop"),
    Tool("screen_scroll", "Turn the mouse wheel at a position on the screen.", ScrollInput, _run_scroll, _assess_scroll, group="desktop"),
]
