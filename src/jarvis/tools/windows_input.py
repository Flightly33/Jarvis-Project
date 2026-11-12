"""Putting text or a file into another application's window on Windows.

Jarvis brings the named window to the front, pastes, and optionally presses
Enter - what the user would do with Ctrl+V. It always asks first, never
touches command-line windows (that would sidestep the checks on shell
commands), and does nothing unless the intended window really is in front.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess

from pydantic import Field

from jarvis.safety import Access, Assessment, Risk
from jarvis.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult, Trimmed

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TERMINALS = {"powershell", "pwsh", "cmd", "windowsterminal", "conhost", "openconsole", "wt", "bash", "wsl", "mintty", "putty", "alacritty", "wezterm-gui"}

_LIST = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    "@(Get-Process | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle } | "
    "Select-Object @{n='title';e={$_.MainWindowTitle}}, @{n='program';e={$_.ProcessName}}) | ConvertTo-Json -Compress"
)

_PASTE = r"""
[Console]::OutputEncoding=[Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
Add-Type -Namespace J -Name W -MemberDefinition '[DllImport("user32.dll")] public static extern System.IntPtr GetForegroundWindow(); [DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr h); [DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr h, int c); [DllImport("user32.dll")] public static extern bool IsIconic(System.IntPtr h);'
$want = $env:JARVIS_WINDOW.ToLower()
$all = @(Get-Process | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle })
$p = $all | Where-Object { $_.MainWindowTitle.ToLower() -eq $want } | Select-Object -First 1
if (-not $p) { $p = $all | Where-Object { $_.MainWindowTitle.ToLower().Contains($want) -or $_.ProcessName.ToLower() -eq $want } | Select-Object -First 1 }
if (-not $p) { Write-Output 'NOWINDOW'; exit 0 }
if ($env:JARVIS_TERMINALS.Split(',') -contains $p.ProcessName.ToLower()) { Write-Output ('TERMINAL ' + $p.ProcessName); exit 0 }
$h = $p.MainWindowHandle
if ([J.W]::IsIconic($h)) { [void][J.W]::ShowWindow($h, 9) }
$shell = New-Object -ComObject WScript.Shell
foreach ($try in 1..4) {
  [void]$shell.AppActivate($p.Id); [void][J.W]::SetForegroundWindow($h)
  Start-Sleep -Milliseconds 350
  if ([J.W]::GetForegroundWindow() -eq $h) { break }
  $shell.SendKeys('%')
}
if ([J.W]::GetForegroundWindow() -ne $h) { Write-Output 'NOFOCUS'; exit 0 }
$old = $null; try { $old = Get-Clipboard -Raw } catch {}
if ($env:JARVIS_FILE) { Set-Clipboard -Path $env:JARVIS_FILE; [System.Windows.Forms.SendKeys]::SendWait('^v'); Start-Sleep -Milliseconds 900 }
if ($env:JARVIS_TEXT) { Set-Clipboard -Value $env:JARVIS_TEXT; [System.Windows.Forms.SendKeys]::SendWait('^v'); Start-Sleep -Milliseconds 400 }
if ($env:JARVIS_ENTER -eq '1') { [System.Windows.Forms.SendKeys]::SendWait('{ENTER}'); Start-Sleep -Milliseconds 200 }
if ($old) { try { Set-Clipboard -Value $old } catch {} }
Write-Output ('OK ' + $p.ProcessName + ' | ' + $p.MainWindowTitle)
"""


def _powershell(script: str, extra_env: dict[str, str] | None = None, timeout: float = 40.0) -> str:
    try:
        done = subprocess.run(  # noqa: S603 - fixed script; the variable parts travel as environment variables, never as code
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
            capture_output=True, timeout=timeout, check=False, creationflags=_NO_WINDOW, env={**os.environ, **(extra_env or {})},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError(f"Windows did not respond: {exc}") from exc
    out = done.stdout.decode("utf-8", errors="replace").strip()
    if done.returncode != 0 and not out:
        raise ToolError(f"Windows refused: {done.stderr.decode('utf-8', errors='replace').strip()[:300]}")
    return out


def list_windows() -> list[dict]:
    rows = json.loads(_powershell(_LIST) or "[]")
    return [rows] if isinstance(rows, dict) else rows


def paste_into_window(window: str, text: str = "", file: str = "", press_enter: bool = False) -> str:
    """Returns the title of the window that received it; raises ToolError if nothing was sent."""
    out = _powershell(_PASTE, {"JARVIS_WINDOW": window, "JARVIS_TEXT": text, "JARVIS_FILE": file, "JARVIS_ENTER": "1" if press_enter else "0",
                               "JARVIS_TERMINALS": ",".join(sorted(TERMINALS))})
    last = out.splitlines()[-1] if out else ""
    if last.startswith("OK "):
        return last[3:]
    if last == "NOWINDOW":
        raise ToolError(f"No open window matches '{window}'. Use list_windows to see what is open; the application may need opening first.")
    if last.startswith("TERMINAL"):
        raise ToolError("That is a command-line window. Jarvis does not type into those; use run_command instead, which checks the command first.")
    if last == "NOFOCUS":
        raise ToolError("Windows would not bring that window to the front (the screen may be locked or a full-screen program is in the way), so nothing was sent.")
    raise ToolError(f"Nothing was sent: {out[-300:] or 'no answer from Windows'}")


class ListWindowsInput(ToolInput):
    pass


async def _run_list(a: ListWindowsInput, ctx: ToolContext) -> ToolResult:
    windows = await asyncio.to_thread(list_windows)
    return ToolResult({"windows": windows}, f"{len(windows)} window(s) open", untrusted_source="window titles")


class SendToWindowInput(ToolInput):
    window: Trimmed = Field(min_length=2, max_length=120, description="Part of the window's title, or its program name, from list_windows.")
    text: str = Field(default="", max_length=8000, description="Text to paste into whatever field is active in that window.")
    file_path: Trimmed = Field(default="", description="A file or picture inside the approved folders to paste in (for applications that accept pasted files).")
    press_enter: bool = Field(default=False, description="Press Enter afterwards, e.g. to send a chat message.")


def _assess_send(a: SendToWindowInput, ctx: ToolContext) -> Assessment:
    what = " and ".join(part for part in ("a file" if a.file_path else "", "text" if a.text else "") if part) or "nothing"
    base = Assessment(
        risk=Risk.MODIFY, title=f"Paste {what} into the '{a.window[:60]}' window" + (" and press Enter" if a.press_enter else ""),
        detail=(a.file_path + "\n" if a.file_path else "") + a.text[:600], must_ask=True, sends_external=True,
        reasons=["Whatever that application does with it happens as you."],
    )
    if not a.text and not a.file_path:
        base.blocked = "Give text, a file, or both."
    return base


async def _run_send(a: SendToWindowInput, ctx: ToolContext) -> ToolResult:
    file = ""
    if a.file_path:
        path = ctx.sandbox.resolve(a.file_path, Access.READ)
        if not path.is_file():
            raise ToolError(f"{path} is not a file.")
        file = str(path)
    target = await asyncio.to_thread(paste_into_window, a.window, a.text, file, a.press_enter)
    return ToolResult({"sent_to": target, "note": "Take a screenshot to check it arrived where intended."}, f"pasted into {target[:50]}",
                      untrusted_source="window titles")


WINDOW_TOOLS: list[Tool] = [
    Tool("list_windows", "List the application windows open on the user's desktop (title and program).", ListWindowsInput, _run_list, timeout=45, group="desktop"),
    Tool(
        "send_to_window",
        "Bring another application's window to the front and paste text and/or a file into it, optionally pressing Enter - for example "
        "to put a message into a chat application. It pastes wherever that window's cursor is, so say so if the result should be checked. "
        "It will not type into command-line windows.",
        SendToWindowInput, _run_send, _assess_send, timeout=60, group="desktop",
    ),
]
