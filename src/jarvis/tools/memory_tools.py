"""Long-term notes about the user: preferences, projects, standing facts."""

from __future__ import annotations

from pydantic import Field

from jarvis.safety import Assessment, Risk
from jarvis.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult


class RememberInput(ToolInput):
    content: str = Field(description="One self-contained fact, e.g. 'Beat Wayfarer project lives in ~/Projects/BeatWayfarer (Godot 4)'.")
    category: str = Field(default="general", description="A short label such as 'preference', 'project', 'school', 'person'.")


def _assess_remember(a: RememberInput, ctx: ToolContext) -> Assessment:
    return Assessment(risk=Risk.LOW, title=f"Remember: {a.content[:80]}")


async def _run_remember(a: RememberInput, ctx: ToolContext) -> ToolResult:
    try:
        memory = ctx.store.remember(a.content, a.category)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    from jarvis.tools.vault import append_memory

    append_memory(ctx, a.content, a.category)
    return ToolResult({"saved": memory.public()}, "noted")


class RecallInput(ToolInput):
    query: str = Field(default="", description="What to look for. Empty returns the most recent notes.")
    category: str = ""
    limit: int = Field(default=8, ge=1, le=50)


async def _run_recall(a: RecallInput, ctx: ToolContext) -> ToolResult:
    if a.category and not a.query:
        found = ctx.store.memories(a.category, a.limit)
    else:
        found = ctx.store.recall(a.query, a.limit)
        if a.category:
            found = [m for m in found if m.category == a.category.lower()]
    return ToolResult({"notes": [m.public() for m in found]}, f"{len(found)} note(s)")


class ForgetInput(ToolInput):
    note_id: int = Field(description="The id of the note, as returned by recall.")


def _assess_forget(a: ForgetInput, ctx: ToolContext) -> Assessment:
    return Assessment(risk=Risk.LOW, title=f"Forget note {a.note_id}")


async def _run_forget(a: ForgetInput, ctx: ToolContext) -> ToolResult:
    if not ctx.store.forget(a.note_id):
        raise ToolError(f"There is no note with id {a.note_id}.")
    return ToolResult({"forgotten": a.note_id}, "forgotten")


MEMORY_TOOLS: list[Tool] = [
    Tool(
        "remember",
        "Save a lasting fact about the user so it is known in future conversations: preferences, where projects "
        "live, courses, recurring commitments. Call it when the user states such a fact or asks you to remember something. "
        "Never store passwords, keys or other secrets.",
        RememberInput, _run_remember, _assess_remember, group="memory",
    ),
    Tool("recall", "Look up what has been remembered about the user.", RecallInput, _run_recall, group="memory"),
    Tool("forget", "Delete a remembered note that is wrong or no longer wanted.", ForgetInput, _run_forget, _assess_forget, group="memory"),
]
