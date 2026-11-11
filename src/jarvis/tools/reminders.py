"""Reminders that Jarvis speaks and shows when they come due."""

from __future__ import annotations

import time
from datetime import datetime

from pydantic import Field

from jarvis.safety import Assessment, Risk
from jarvis.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult


class CreateReminderInput(ToolInput):
    text: str = Field(description="What to remind the user about, phrased as it should be read out.")
    when: str = Field(default="", description="Local date and time in ISO form, e.g. '2026-11-12T09:00'. Work it out from the current time given in the request.")
    in_minutes: float = Field(default=0, ge=0, description="Alternative to 'when': minutes from now.")


def parse_when(when: str, in_minutes: float, now: float | None = None) -> float:
    now = time.time() if now is None else now
    if in_minutes:
        return now + in_minutes * 60
    if not when:
        raise ToolError("Give either 'when' (a date and time) or 'in_minutes'.")
    try:
        moment = datetime.fromisoformat(when)
    except ValueError as exc:
        raise ToolError(f"'{when}' is not a valid date and time. Use the form 2026-11-12T09:00.") from exc
    due = moment.timestamp() if moment.tzinfo else moment.astimezone().timestamp()   # naive means local time
    if due < now - 60:
        raise ToolError(f"{when} is in the past (it is now {datetime.fromtimestamp(now).isoformat(timespec='minutes')}).")
    return due


def _assess_create(a: CreateReminderInput, ctx: ToolContext) -> Assessment:
    return Assessment(risk=Risk.LOW, title=f"Set a reminder: {a.text[:80]}")


async def _run_create(a: CreateReminderInput, ctx: ToolContext) -> ToolResult:
    reminder = ctx.store.add_reminder(a.text, parse_when(a.when, a.in_minutes))
    ctx.services.bus.emit("reminders_changed")
    return ToolResult({"reminder": reminder.public()}, f"reminder set for {reminder.public()['due']}")


class ListRemindersInput(ToolInput):
    include_past: bool = False


async def _run_list(a: ListRemindersInput, ctx: ToolContext) -> ToolResult:
    items = ctx.store.reminders(None if a.include_past else "pending")
    return ToolResult({"reminders": [r.public() for r in items]}, f"{len(items)} reminder(s)")


class CancelReminderInput(ToolInput):
    reminder_id: int


def _assess_cancel(a: CancelReminderInput, ctx: ToolContext) -> Assessment:
    return Assessment(risk=Risk.LOW, title=f"Cancel reminder {a.reminder_id}")


async def _run_cancel(a: CancelReminderInput, ctx: ToolContext) -> ToolResult:
    if not ctx.store.set_reminder_status(a.reminder_id, "cancelled"):
        raise ToolError(f"There is no pending reminder with id {a.reminder_id}.")
    ctx.services.bus.emit("reminders_changed")
    return ToolResult({"cancelled": a.reminder_id}, "reminder cancelled")


REMINDER_TOOLS: list[Tool] = [
    Tool(
        "create_reminder",
        "Schedule a reminder. Jarvis will say it aloud and show it when the time comes, even in a later session.",
        CreateReminderInput, _run_create, _assess_create, group="reminders",
    ),
    Tool("list_reminders", "List upcoming reminders.", ListRemindersInput, _run_list, group="reminders"),
    Tool("cancel_reminder", "Cancel a pending reminder.", CancelReminderInput, _run_cancel, _assess_cancel, group="reminders"),
]
