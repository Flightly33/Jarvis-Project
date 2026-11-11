"""Read-only information about the computer's health."""

from __future__ import annotations

import asyncio
import time

import psutil

from jarvis.tools.base import Tool, ToolContext, ToolInput, ToolResult


class SystemStatusInput(ToolInput):
    pass


def _gb(n: float) -> float:
    return round(n / 1024**3, 1)


def _collect(roots: list[str]) -> dict:
    cpu = psutil.cpu_percent(interval=0.4)
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disks = []
    for mount in dict.fromkeys(["/", *roots]):
        try:
            usage = psutil.disk_usage(mount)
        except OSError:
            continue
        disks.append({"path": mount, "total_gb": _gb(usage.total), "free_gb": _gb(usage.free), "used_percent": usage.percent})
    procs = []
    for p in psutil.process_iter(["name", "memory_percent", "cpu_percent"]):
        try:
            procs.append((p.info["memory_percent"] or 0.0, p.info["cpu_percent"] or 0.0, p.info["name"] or "?"))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    top = sorted(procs, reverse=True)[:6]
    out = {
        "cpu_percent": cpu,
        "cpu_cores": psutil.cpu_count(),
        "load_average": [round(x, 2) for x in psutil.getloadavg()],
        "memory": {"total_gb": _gb(mem.total), "available_gb": _gb(mem.available), "used_percent": mem.percent},
        "swap": {"total_gb": _gb(swap.total), "used_percent": swap.percent},
        "disks": disks,
        "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
        "top_memory_processes": [{"name": n, "memory_percent": round(m, 1), "cpu_percent": round(c, 1)} for m, c, n in top],
    }
    battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if battery is not None:
        out["battery"] = {"percent": round(battery.percent), "plugged_in": battery.power_plugged}
    return out


async def _run_status(a: SystemStatusInput, ctx: ToolContext) -> ToolResult:
    data = await asyncio.to_thread(_collect, [str(p) for p in ctx.sandbox.approved if p.exists()])
    return ToolResult(data, f"CPU {data['cpu_percent']}%, memory {data['memory']['used_percent']}%")


SYSTEM_TOOLS: list[Tool] = [
    Tool(
        "system_status",
        "Report the computer's CPU load, memory, disk space, battery, uptime and heaviest processes. "
        "Use it for questions about performance or free space.",
        SystemStatusInput, _run_status, group="system",
    ),
]
