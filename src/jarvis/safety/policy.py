"""Decides whether an action runs, needs the user's approval, or is refused.

The model proposes; this module disposes. Tools describe what they are about
to do as an `Assessment`, and the policy combines that with the user's chosen
autonomy level and with what has entered the conversation so far.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Literal


class Risk(IntEnum):
    READ = 0      # observes only
    LOW = 1       # additive and easily undone: new file or folder, open an app, set a reminder
    MODIFY = 2    # changes something that exists, recoverably: edit with backup, move, send to trash
    HIGH = 3      # hard to undo or broad in effect: permanent delete, installs, credentials, sending


RISK_LABEL = {Risk.READ: "read-only", Risk.LOW: "low", Risk.MODIFY: "changes data", Risk.HIGH: "high"}

# The lowest risk that still needs the user's approval, per autonomy level.
ASK_FROM: dict[str, Risk] = {"cautious": Risk.LOW, "balanced": Risk.MODIFY, "trusting": Risk.HIGH}


class Verdict(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass
class Assessment:
    """What a tool call would do, expressed for the policy and for the approval dialog."""

    risk: Risk = Risk.READ
    title: str = ""
    detail: str = ""
    reasons: list[str] = field(default_factory=list)
    blocked: str | None = None          # refuse outright, with this explanation
    must_ask: bool = False              # ask regardless of the autonomy level
    sends_external: bool = False        # can carry data off this computer
    reads_private: bool = False         # brings the user's own files or screen into the conversation
    ingests_untrusted: bool = False     # brings outside content (web pages, mail) into the conversation
    grant_key: str | None = None        # identifies "the same kind of action" for request-scoped approval
    allow_always: bool = False          # offer "always allow" in the approval dialog


@dataclass
class ConversationFlags:
    """What has entered the model's context during the current conversation."""

    tainted: bool = False        # untrusted outside content has been read
    private_read: bool = False   # the user's private data has been read

    def reset(self) -> None:
        self.tainted = False
        self.private_read = False


@dataclass
class Decision:
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)


class PolicyEngine:
    def __init__(self, autonomy: Literal["cautious", "balanced", "trusting"] = "balanced", exfiltration_guard: bool = True) -> None:
        self.autonomy = autonomy
        self.exfiltration_guard = exfiltration_guard

    def decide(self, a: Assessment, flags: ConversationFlags, grants: set[str] | None = None) -> Decision:
        if a.blocked:
            return Decision(Verdict.DENY, [a.blocked])

        reasons = list(a.reasons)
        ask = a.must_ask or a.risk >= ASK_FROM[self.autonomy]
        if a.risk >= Risk.HIGH:
            ask = True
        if ask and not reasons:
            reasons.append(f"This action is rated '{RISK_LABEL[a.risk]}' and your autonomy level is '{self.autonomy}'.")

        if self.exfiltration_guard:
            # Content from the web can contain instructions aimed at the assistant. Once such
            # content is in the conversation, nothing that changes data runs unattended...
            if flags.tainted and a.risk >= Risk.MODIFY and not ask:
                ask = True
                reasons.append("Web content was read earlier in this conversation, so changes need your confirmation.")
            # ...and nothing leaves the machine if private data has also been read.
            if a.sends_external and flags.tainted and flags.private_read:
                ask = True
                reasons.append(
                    "This would contact the internet after both web content and your private files were read "
                    "in this conversation - the pattern used to steal data through an assistant."
                )

        if not ask:
            return Decision(Verdict.ALLOW)
        # A request-scoped approval covers repeats of the same kind of action, never high-risk ones.
        if grants and a.grant_key and a.grant_key in grants and a.risk < Risk.HIGH:
            return Decision(Verdict.ALLOW)
        return Decision(Verdict.ASK, reasons)
