"""Running shell commands, in the foreground or as background jobs.

Commands run through bash in their own process group, so a timeout, a
cancelled request or the emergency stop can end the whole process tree. The
environment handed to them has credentials stripped out.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import Field

from jarvis.events import EventBus
from jarvis.safety import Access, Assessment, Risk, classify_command
from jarvis.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult, Trimmed

_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE)", re.IGNORECASE)
_HEAD_CHARS = 6_000
_TAIL_CHARS = 14_000
_MAX_TIMEOUT = 3600


def command_env() -> dict[str, str]:
    """The environment for child commands: the user's own, minus anything that looks like a credential."""
    env = {
        k: v for k, v in os.environ.items()
        if not _SECRET_ENV.search(k) and not k.startswith(("ANTHROPIC_", "CLAUDE_", "CLAUDECODE"))
    }
    # Never let a command sit waiting for input nobody can give.
    env.update({"GIT_TERMINAL_PROMPT": "0", "DEBIAN_FRONTEND": "noninteractive", "PAGER": "cat", "GIT_PAGER": "cat"})
    return env


class _Capture:
    """Keeps the beginning and the end of a command's output, which is where the answers usually are."""

    def __init__(self) -> None:
        self.head = ""
        self.tail = ""
        self.total = 0

    def add(self, text: str) -> None:
        self.total += len(text)
        room = _HEAD_CHARS - len(self.head)
        if room > 0:
            self.head += text[:room]
            text = text[room:]
        if text:
            self.tail = (self.tail + text)[-_TAIL_CHARS:]

    def text(self) -> str:
        omitted = self.total - len(self.head) - len(self.tail)
        if omitted > 0:
            return f"{self.head}\n... [{omitted} characters omitted] ...\n{self.tail}"
        return self.head + self.tail

    def last(self, chars: int = 2000) -> str:
        return (self.head + self.tail)[-chars:]


def _kill_group(proc: asyncio.subprocess.Process, sig: int = signal.SIGTERM) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)


async def _terminate(proc: asyncio.subprocess.Process, grace: float = 2.0) -> None:
    """Ask the process tree to stop, then insist."""
    _kill_group(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), grace)
    except TimeoutError:
        _kill_group(proc, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 2.0)


async def _spawn(command: str, cwd: Path) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        "/bin/bash", "-c", command,
        cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=command_env(),
        start_new_session=True,      # own process group, so the whole tree can be stopped
    )


async def _pump(proc: asyncio.subprocess.Process, capture: _Capture) -> None:
    assert proc.stdout is not None
    while chunk := await proc.stdout.read(4096):
        capture.add(chunk.decode("utf-8", errors="replace"))


# ------------------------------------------------------------- background jobs


@dataclass
class Job:
    id: str
    command: str
    cwd: str
    started_at: float
    proc: asyncio.subprocess.Process = field(repr=False)
    capture: _Capture = field(default_factory=_Capture, repr=False)
    status: str = "running"            # running | finished | failed | stopped
    exit_code: int | None = None
    finished_at: float | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    def public(self, output_chars: int = 3000) -> dict:
        end = self.finished_at or time.time()
        return {
            "job_id": self.id, "command": self.command, "cwd": self.cwd, "status": self.status,
            "exit_code": self.exit_code, "running_for_seconds": round(end - self.started_at, 1),
            "recent_output": self.capture.last(output_chars),
        }


class JobManager:
    def __init__(self, bus: EventBus, max_jobs: int = 8) -> None:
        self._bus = bus
        self._jobs: dict[str, Job] = {}
        self.max_jobs = max_jobs

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def jobs(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)

    async def start(self, command: str, cwd: Path) -> Job:
        running = [j for j in self._jobs.values() if j.status == "running"]
        if len(running) >= self.max_jobs:
            raise ToolError(f"{len(running)} background commands are already running; stop one first.")
        proc = await _spawn(command, cwd)
        job = Job(uuid.uuid4().hex[:8], command, str(cwd), time.time(), proc)
        job.task = asyncio.create_task(self._watch(job))
        self._jobs[job.id] = job
        self._bus.emit("job", job=job.public(400))
        return job

    async def _watch(self, job: Job) -> None:
        try:
            await _pump(job.proc, job.capture)
            job.exit_code = await job.proc.wait()
        finally:
            job.finished_at = time.time()
            if job.status == "running":
                job.status = "finished" if job.exit_code == 0 else "failed"
            self._bus.emit("job", job=job.public(1500), finished=True)

    async def stop(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise ToolError(f"No background command with id {job_id}.")
        if job.status == "running":
            job.status = "stopped"
            await _terminate(job.proc)
        return job

    async def stop_all(self) -> int:
        running = [j for j in self._jobs.values() if j.status == "running"]
        for job in running:
            job.status = "stopped"
        await asyncio.gather(*(_terminate(j.proc) for j in running), return_exceptions=True)
        return len(running)


# ----------------------------------------------------------------------- tools


class RunCommandInput(ToolInput):
    command: str = Field(description="The shell (bash) command line to run.")
    cwd: str = Field(default="", description="Folder to run in. Empty uses the main approved folder.")
    timeout_seconds: int = Field(default=0, ge=0, le=_MAX_TIMEOUT, description="Stop the command after this long. 0 uses the default.")
    background: bool = Field(
        default=False,
        description="Start it and return at once (builds, servers, long tests). Check on it later with command_status.",
    )


def _assess_run(a: RunCommandInput, ctx: ToolContext) -> Assessment:
    cwd_check = ctx.sandbox.check(a.cwd or ctx.sandbox.default_dir, Access.READ)
    if not cwd_check.allowed:
        return Assessment(blocked=f"Commands can only run inside an approved folder: {cwd_check.reason}")
    verdict = classify_command(a.command, cwd_check.path, ctx.sandbox, ctx.config.safety.command_allowlist)
    normalized = " ".join(a.command.split())
    base = Assessment(
        risk=verdict.risk,
        title=f"Run: {normalized[:100]}",
        detail=f"$ {a.command}\n\nin {cwd_check.path}" + ("\n(in the background)" if a.background else ""),
        reasons=list(verdict.reasons) if not verdict.auto_ok else [],
        sends_external=verdict.sends_external,
        grant_key=f"shell:{normalized}",
    )
    if verdict.blocked:
        base.blocked = verdict.reasons[0]
    elif verdict.category == "unclassified":
        base.must_ask = True
        base.allow_always = True

        def remember() -> None:
            allow = ctx.config.safety.command_allowlist
            if normalized not in allow:
                allow.append(normalized)
                ctx.services.save_config()

        base.on_always = remember
    return base


async def _run_command(a: RunCommandInput, ctx: ToolContext) -> ToolResult:
    cwd = ctx.sandbox.resolve(a.cwd or ctx.sandbox.default_dir, Access.READ)
    if not cwd.is_dir():
        raise ToolError(f"{cwd} is not a folder.")

    if a.background:
        if ctx.services.jobs is None:
            raise ToolError("Background commands are not available.")
        job = await ctx.services.jobs.start(a.command, cwd)
        await asyncio.sleep(0.4)     # long enough to catch a command that fails immediately
        return ToolResult(job.public(1200), f"started in background (job {job.id})")

    timeout = a.timeout_seconds or ctx.config.safety.command_timeout_seconds
    started = time.monotonic()
    proc = await _spawn(a.command, cwd)
    capture = _Capture()
    pump = asyncio.create_task(_pump(proc, capture))
    timed_out = False
    try:
        await asyncio.wait_for(asyncio.shield(pump), timeout)
        await proc.wait()
    except TimeoutError:
        timed_out = True
        await _terminate(proc)
    except asyncio.CancelledError:
        await _terminate(proc)       # emergency stop or interrupted request
        raise
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pump
    elapsed = round(time.monotonic() - started, 2)
    code = proc.returncode
    result = {"command": a.command, "cwd": str(cwd), "exit_code": code, "succeeded": code == 0 and not timed_out,
              "seconds": elapsed, "output": capture.text()}
    if timed_out:
        result["timed_out"] = f"stopped after {timeout} seconds; run it with background=true if it needs longer"
    summary = f"timed out after {timeout}s" if timed_out else f"exit code {code} in {elapsed}s"
    return ToolResult(result, summary)


class CommandStatusInput(ToolInput):
    job_id: Trimmed = Field(default="", description="Id returned by run_command with background=true. Empty lists every job.")


async def _run_status(a: CommandStatusInput, ctx: ToolContext) -> ToolResult:
    jobs = ctx.services.jobs
    if jobs is None:
        raise ToolError("Background commands are not available.")
    if not a.job_id:
        listing = [j.public(300) for j in jobs.jobs()]
        return ToolResult({"jobs": listing}, f"{len(listing)} background command(s)")
    job = jobs.get(a.job_id)
    if job is None:
        raise ToolError(f"No background command with id {a.job_id}.")
    return ToolResult(job.public(6000), f"job {job.id}: {job.status}")


class StopCommandInput(ToolInput):
    job_id: Trimmed


def _assess_stop(a: StopCommandInput, ctx: ToolContext) -> Assessment:
    return Assessment(risk=Risk.LOW, title=f"Stop background command {a.job_id}")


async def _run_stop(a: StopCommandInput, ctx: ToolContext) -> ToolResult:
    if ctx.services.jobs is None:
        raise ToolError("Background commands are not available.")
    job = await ctx.services.jobs.stop(a.job_id)
    return ToolResult(job.public(1500), f"job {job.id}: {job.status}")


SHELL_TOOLS: list[Tool] = [
    Tool(
        "run_command",
        "Run a shell command on the user's computer and return its output and exit code. Read-only commands inside "
        "approved folders run immediately; anything else is shown to the user for approval first. Prefer the "
        "dedicated file tools for reading and changing files.",
        RunCommandInput, _run_command, _assess_run, timeout=None, group="shell",
    ),
    Tool(
        "command_status",
        "Check a background command started earlier: whether it is still running, its exit code and recent output. "
        "Use this when the user asks whether a build, test run or other long task has finished.",
        CommandStatusInput, _run_status, group="shell",
    ),
    Tool("stop_command", "Stop a background command that Jarvis started.", StopCommandInput, _run_stop, _assess_stop, group="shell"),
]
