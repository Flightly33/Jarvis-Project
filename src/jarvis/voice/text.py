"""Text handling for speech: sentence splitting, cleaning text for reading aloud, spoken commands."""

from __future__ import annotations

import difflib
import re

from jarvis.persona import IGNORE_SENTINEL

_SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*\s")
_ABBREVIATIONS = {"mr.", "mrs.", "ms.", "dr.", "prof.", "vs.", "etc.", "approx.", "no.", "st.", "jr.", "sr.", "inc.", "ltd."}
_GREETING = r"(?:hey|hi|ok|okay|yo|hello)"
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


class SentenceChunker:
    """Collects streamed reply text and releases it one sentence at a time, so speech can start early."""

    def __init__(self, min_chars: int = 28) -> None:
        self._buffer = ""
        self._min = min_chars

    def feed(self, text: str) -> list[str]:
        self._buffer += text
        out: list[str] = []
        while True:
            cut = self._boundary()
            if cut is None:
                return [s for s in out if s]
            out.append(self._buffer[:cut].strip())
            self._buffer = self._buffer[cut:]

    def _boundary(self) -> int | None:
        newline = self._buffer.find("\n")
        if newline >= 0 and self._buffer[:newline].strip():
            return newline + 1
        if newline >= 0:
            self._buffer = self._buffer[newline + 1:]
            return self._boundary()
        for match in _SENTENCE_END.finditer(self._buffer):
            if match.end() < self._min:        # very short fragments are not worth a pause
                continue
            words = self._buffer[:match.start() + 1].split()
            last = words[-1].lower() if words else ""
            if last in _ABBREVIATIONS or re.fullmatch(r"(?:[a-z]\.)+", last):     # "e.g.", "Dr.", initials
                continue
            return match.end()
        return None

    def flush(self) -> list[str]:
        rest, self._buffer = self._buffer.strip(), ""
        return [rest] if rest else []


def speakable(text: str) -> str:
    """Turn reply text into something that sounds right when read aloud."""
    text = text.replace(IGNORE_SENTINEL, " ")
    text = re.sub(r"```.*?(```|$)", " ", text, flags=re.DOTALL)                    # code is for the screen
    text = re.sub(r"\[([^\]]+)\]\((?:https?://)[^)]*\)", r"\1", text)              # [label](link) -> label
    text = re.sub(r"https?://(?:www\.)?([^/\s)]+)[^\s)]*", r"\1", text)            # bare link -> site name
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", text, flags=re.MULTILINE)       # list markers
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_~#>|]+", "", text)
    text = re.sub(r"[\U00010000-\U0010ffff☀-➿️]", "", text)         # emoji and pictographs
    return " ".join(text.split())


def _letters(text: str) -> str:
    return re.sub(r"[^a-z ]", "", text.lower()).strip()


def strip_wake_word(text: str, wake_word: str) -> tuple[str, bool]:
    """Remove a leading wake word ("Jarvis, ...", "Hey Jarvis ...") from a transcript.

    Speech recognition often mishears a name ("Jervis", "Jarvas"), so the first words are compared
    loosely. Returns the remaining text and whether a wake word was found.
    """
    stripped = text.strip()
    exact = re.match(rf"^\W*(?:{_GREETING}\W+)?{re.escape(wake_word)}\b[\s,.!?:;-]*", stripped, re.IGNORECASE)
    if exact:
        return stripped[exact.end():].strip(), True
    words = stripped.split()
    target = _letters(wake_word)
    count = len(target.split())
    for skip in (0, 1):             # allow one greeting word in front
        if skip and not (words and re.fullmatch(_GREETING, _letters(words[0]) or "-")):
            continue
        candidate = _letters(" ".join(words[skip:skip + count]))
        if candidate and difflib.SequenceMatcher(None, candidate, target).ratio() >= 0.75:
            return " ".join(words[skip + count:]).strip(" ,.!?:;-"), True
    return stripped, False


def local_intent(text: str) -> str | None:
    """Recognise short spoken commands that are handled at once, without asking the model."""
    phrase = re.sub(r"[^a-z' ]", " ", text.lower())
    phrase = " ".join(_POLITE.sub(" ", phrase).split())
    for intent, phrases in _INTENTS.items():
        if phrase in phrases:
            return intent
    return None
