"""Short commands (yes, no, stop...) recognised without asking the model; shared by voice and phone."""

from __future__ import annotations

import re

_POLITE = re.compile(r"\b(please|jarvis|thanks|thank you)\b")

_INTENTS: dict[str, set[str]] = {
    "stop": {"stop", "stop it", "stop that", "stop talking", "cancel", "cancel that", "abort", "quiet", "be quiet", "shut up",
             "silence", "enough", "stop everything", "emergency stop", "halt", "that's enough", "thats enough"},
    "nevermind": {"never mind", "nevermind", "forget it", "nothing", "no nothing"},
    "pause": {"stop listening", "pause listening", "go to sleep", "sleep", "mute", "mute yourself", "pause the microphone", "pause microphone"},
    "confirm": {"confirm", "i confirm", "yes confirm", "confirmed", "yes i confirm", "confirm it"},
    "yes": {"yes", "yeah", "yep", "yes please", "approve", "approved", "approve it", "go ahead", "do it", "sure", "okay", "ok",
            "allow", "allow it", "proceed", "yes do it", "yes go ahead"},
    "no": {"no", "nope", "no thanks", "deny", "denied", "deny it", "don't", "dont", "do not", "decline", "no don't", "no dont"},
}



def local_intent(text: str) -> str | None:
    """Recognise short spoken commands that are handled at once, without asking the model."""
    phrase = re.sub(r"[^a-z' ]", " ", text.lower())
    phrase = " ".join(_POLITE.sub(" ", phrase).split())
    for intent, phrases in _INTENTS.items():
        if phrase in phrases:
            return intent
    return None
