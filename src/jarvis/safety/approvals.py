"""Holds an action until the user approves or declines it.

A request is published on the event bus; any front end (web page, terminal,
voice) can answer it. Unanswered requests are declined when they time out, and
the emergency stop declines everything that is waiting.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Literal

from jarvis.events import EventBus

Scope = Literal["once", "request", "always"]


@dataclass
class ApprovalRequest:
    id: str
    tool: str
    title: str
    detail: str
    risk: str
    reasons: list[str]
    allow_always: bool
    allow_request_scope: bool
    created_at: float
    expires_at: float

    def public(self) -> dict:
        return asdict(self)


@dataclass
class ApprovalOutcome:
    approved: bool
    scope: Scope = "once"
    by: str = "user"          # user | voice | timeout | emergency-stop | terminal


@dataclass
class _Pending:
    request: ApprovalRequest
    future: asyncio.Future[ApprovalOutcome] = field(repr=False)


class ApprovalBroker:
    def __init__(self, bus: EventBus, timeout_seconds: float = 120.0) -> None:
        self._bus = bus
        self.timeout_seconds = timeout_seconds
        self._pending: dict[str, _Pending] = {}

    def pending(self) -> list[ApprovalRequest]:
        return [p.request for p in self._pending.values()]

    async def request(
        self,
        *,
        tool: str,
        title: str,
        detail: str,
        risk: str,
        reasons: list[str],
        allow_always: bool = False,
        allow_request_scope: bool = True,
    ) -> ApprovalOutcome:
        now = time.time()
        req = ApprovalRequest(
            id=uuid.uuid4().hex[:12],
            tool=tool,
            title=title,
            detail=detail,
            risk=risk,
            reasons=reasons,
            allow_always=allow_always,
            allow_request_scope=allow_request_scope,
            created_at=now,
            expires_at=now + self.timeout_seconds,
        )
        future: asyncio.Future[ApprovalOutcome] = asyncio.get_running_loop().create_future()
        self._pending[req.id] = _Pending(req, future)
        self._bus.emit("approval_request", request=req.public())
        try:
            outcome = await asyncio.wait_for(future, timeout=self.timeout_seconds)
        except TimeoutError:
            outcome = ApprovalOutcome(False, by="timeout")
        except asyncio.CancelledError:
            # The request was interrupted (emergency stop or a cancelled turn): tidy up and propagate.
            self._pending.pop(req.id, None)
            self._bus.emit("approval_resolved", id=req.id, approved=False, by="cancelled")
            raise
        self._pending.pop(req.id, None)
        self._bus.emit("approval_resolved", id=req.id, approved=outcome.approved, scope=outcome.scope, by=outcome.by)
        return outcome

    def resolve(self, request_id: str, approved: bool, scope: Scope = "once", by: str = "user") -> bool:
        """Answer a waiting request. Returns False if it is unknown or already answered."""
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            return False
        if scope == "always" and not pending.request.allow_always:
            scope = "once"
        if scope == "request" and not pending.request.allow_request_scope:
            scope = "once"
        pending.future.set_result(ApprovalOutcome(approved, scope if approved else "once", by))
        return True

    def resolve_latest(self, approved: bool, by: str = "voice", allow_high_risk: bool = False) -> ApprovalRequest | None:
        """Answer the most recent request, for spoken or keyboard confirmation."""
        for pending in reversed(list(self._pending.values())):
            if pending.future.done():
                continue
            if approved and pending.request.risk == "high" and not allow_high_risk:
                return None
            self.resolve(pending.request.id, approved, by=by)
            return pending.request
        return None

    def decline_all(self, by: str = "emergency-stop") -> int:
        count = 0
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_result(ApprovalOutcome(False, by=by))
                count += 1
        return count
