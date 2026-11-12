"""Reaching the user on their paired phone (a private Telegram chat)."""

from __future__ import annotations

from pydantic import Field

from jarvis.safety import Assessment, Risk
from jarvis.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult


class PhoneMessageInput(ToolInput):
    text: str = Field(min_length=1, max_length=3500, description="What to tell the user, in plain text.")
    spoken: bool = Field(default=False, description="Also send it as a voice message in Jarvis's voice. Use this when the user asks to be called or spoken to.")


def _assess(a: PhoneMessageInput, ctx: ToolContext) -> Assessment:
    return Assessment(risk=Risk.LOW, title=f"Message the user's phone: {a.text[:80]}")


async def _run(a: PhoneMessageInput, ctx: ToolContext) -> ToolResult:
    phone = ctx.services.phone
    if phone is None or not phone.token:
        raise ToolError("The phone is not set up. The user can connect it under Settings > Phone.")
    if not phone.owner:
        raise ToolError("The phone is not paired yet. The user needs to send /pair and the code from Settings > Phone to their bot.")
    if not await phone.send(a.text):
        raise ToolError(phone.problem or "The message could not be delivered; Telegram may be unreachable.")
    voiced = a.spoken and await phone.send_voice(a.text)
    note = "" if voiced or not a.spoken else " The voice message could not be sent, only the written one."
    return ToolResult({"sent": True, "voice_message": bool(voiced)}, "sent to the phone" + (" with a voice message" if voiced else "") + note)


PHONE_TOOLS: list[Tool] = [
    Tool(
        "phone_message",
        "Send a message to the user's own phone through their private Telegram chat. It cannot ring the phone or place a real "
        "call: when asked to call, send a spoken voice message instead and say so. It cannot message anyone else.",
        PhoneMessageInput, _run, _assess, group="phone",
    ),
]
