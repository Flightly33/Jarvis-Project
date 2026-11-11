"""A calendar kept on this computer, shown in the interface and usable by voice."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from pydantic import Field

from jarvis.safety import Assessment, Risk
from jarvis.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult


def parse_moment(text: str) -> float:
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise ToolError(f"'{text}' is not a valid date and time. Use the form 2026-11-12T09:00 (or 2026-11-12 for a whole day).") from exc
    return moment.timestamp() if moment.tzinfo else moment.astimezone().timestamp()     # no zone given means local time


class AddEventInput(ToolInput):
    title: str
    start: str = Field(description="Local start in ISO form, e.g. '2026-11-12T14:30'. For an all-day event give just the date.")
    end: str = Field(default="", description="Optional local end time in the same form.")
    all_day: bool = False
    notes: str = ""


def _assess_add(a: AddEventInput, ctx: ToolContext) -> Assessment:
    return Assessment(risk=Risk.LOW, title=f"Add to calendar: {a.title[:80]}")


async def _run_add(a: AddEventInput, ctx: ToolContext) -> ToolResult:
    all_day = a.all_day or "T" not in a.start.strip()
    try:
        event = ctx.store.add_event(a.title, parse_moment(a.start), parse_moment(a.end) if a.end else None, all_day, a.notes)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    ctx.services.bus.emit("calendar_changed")
    return ToolResult({"event": event}, f"added for {event['when']}")


class ListEventsInput(ToolInput):
    start: str = Field(default="", description="First day to include (ISO date). Empty means today.")
    days: int = Field(default=7, ge=1, le=366, description="How many days to cover.")


async def _run_list(a: ListEventsInput, ctx: ToolContext) -> ToolResult:
    if a.start:
        begin = datetime.fromtimestamp(parse_moment(a.start))
    else:
        begin = datetime.fromtimestamp(time.time())
    begin = begin.replace(hour=0, minute=0, second=0, microsecond=0)
    events = ctx.store.events(begin.timestamp(), (begin + timedelta(days=a.days)).timestamp())
    return ToolResult({"from": begin.strftime("%Y-%m-%d"), "days": a.days, "events": events}, f"{len(events)} event(s)")


class DeleteEventInput(ToolInput):
    event_id: int = Field(description="The id of the event, as returned by list_calendar_events.")


def _assess_delete(a: DeleteEventInput, ctx: ToolContext) -> Assessment:
    return Assessment(risk=Risk.MODIFY, title=f"Remove calendar event {a.event_id}", grant_key="calendar:delete")


async def _run_delete(a: DeleteEventInput, ctx: ToolContext) -> ToolResult:
    if not ctx.store.delete_event(a.event_id):
        raise ToolError(f"There is no calendar event with id {a.event_id}.")
    ctx.services.bus.emit("calendar_changed")
    return ToolResult({"deleted": a.event_id}, "event removed")


CALENDAR_TOOLS: list[Tool] = [
    Tool(
        "add_calendar_event",
        "Put an event, deadline or appointment on the user's calendar in Jarvis (kept on this computer; it is not "
        "Google or Outlook). Work out the date from the current time given in the request.",
        AddEventInput, _run_add, _assess_add, group="calendar",
    ),
    Tool("list_calendar_events", "List what is on the user's Jarvis calendar for a range of days.", ListEventsInput, _run_list, group="calendar"),
    Tool("delete_calendar_event", "Remove an event from the user's Jarvis calendar.", DeleteEventInput, _run_delete, _assess_delete, group="calendar"),
]
