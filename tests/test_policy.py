from __future__ import annotations

import asyncio

import pytest

from jarvis.events import EventBus
from jarvis.safety.approvals import ApprovalBroker
from jarvis.safety.estop import EmergencyStop
from jarvis.safety.policy import Assessment, ConversationFlags, PolicyEngine, Risk, Verdict


def decide(autonomy: str, a: Assessment, flags: ConversationFlags | None = None, grants: set[str] | None = None):
    return PolicyEngine(autonomy).decide(a, flags or ConversationFlags(), grants).verdict  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("autonomy", "risk", "expected"),
    [
        ("cautious", Risk.READ, Verdict.ALLOW),
        ("cautious", Risk.LOW, Verdict.ASK),
        ("balanced", Risk.LOW, Verdict.ALLOW),
        ("balanced", Risk.MODIFY, Verdict.ASK),
        ("trusting", Risk.MODIFY, Verdict.ALLOW),
        ("trusting", Risk.HIGH, Verdict.ASK),
        ("cautious", Risk.HIGH, Verdict.ASK),
    ],
)
def test_autonomy_levels(autonomy: str, risk: Risk, expected: Verdict):
    assert decide(autonomy, Assessment(risk=risk)) == expected


def test_blocked_actions_are_denied_at_every_level():
    for autonomy in ("cautious", "balanced", "trusting"):
        assert decide(autonomy, Assessment(risk=Risk.READ, blocked="no")) == Verdict.DENY


def test_must_ask_overrides_a_trusting_setting():
    assert decide("trusting", Assessment(risk=Risk.MODIFY, must_ask=True)) == Verdict.ASK


def test_request_scoped_grant_covers_repeats_but_never_high_risk():
    grants = {"shell:make"}
    assert decide("balanced", Assessment(risk=Risk.MODIFY, grant_key="shell:make"), grants=grants) == Verdict.ALLOW
    assert decide("balanced", Assessment(risk=Risk.MODIFY, grant_key="shell:other"), grants=grants) == Verdict.ASK
    assert decide("balanced", Assessment(risk=Risk.HIGH, grant_key="shell:make"), grants=grants) == Verdict.ASK


def test_web_content_in_the_conversation_removes_unattended_changes():
    tainted = ConversationFlags(tainted=True)
    assert decide("trusting", Assessment(risk=Risk.MODIFY), tainted) == Verdict.ASK
    # reading and purely additive actions are still fine
    assert decide("trusting", Assessment(risk=Risk.LOW), tainted) == Verdict.ALLOW


def test_data_cannot_leave_after_web_content_and_private_files_were_both_read():
    outbound = Assessment(risk=Risk.READ, sends_external=True)
    assert decide("trusting", outbound, ConversationFlags(tainted=True)) == Verdict.ALLOW
    assert decide("trusting", outbound, ConversationFlags(private_read=True)) == Verdict.ALLOW
    both = ConversationFlags(tainted=True, private_read=True)
    decision = PolicyEngine("trusting").decide(outbound, both)
    assert decision.verdict == Verdict.ASK
    assert "steal data" in " ".join(decision.reasons)


def test_guard_can_be_switched_off():
    engine = PolicyEngine("trusting", exfiltration_guard=False)
    both = ConversationFlags(tainted=True, private_read=True)
    assert engine.decide(Assessment(risk=Risk.READ, sends_external=True), both).verdict == Verdict.ALLOW


# ------------------------------------------------------------------ approvals


async def _request(broker: ApprovalBroker, **kw):
    return await broker.request(tool="t", title="Do it", detail="", risk=kw.pop("risk", "changes data"), reasons=[], **kw)


async def test_approval_round_trip_publishes_events():
    bus = EventBus()
    q = bus.subscribe()
    broker = ApprovalBroker(bus, timeout_seconds=5)
    task = asyncio.create_task(_request(broker))
    event = await asyncio.wait_for(q.get(), 1)
    assert event["type"] == "approval_request"
    assert broker.resolve(event["request"]["id"], True, "request")
    outcome = await task
    assert outcome.approved and outcome.scope == "request"
    assert (await q.get())["type"] == "approval_resolved"
    assert broker.pending() == []


async def test_unanswered_request_is_declined_on_timeout():
    broker = ApprovalBroker(EventBus(), timeout_seconds=0.05)
    outcome = await _request(broker)
    assert not outcome.approved and outcome.by == "timeout"


async def test_always_scope_is_only_honoured_when_offered():
    bus = EventBus()
    broker = ApprovalBroker(bus, timeout_seconds=5)
    task = asyncio.create_task(_request(broker, allow_always=False))
    await asyncio.sleep(0)
    broker.resolve(broker.pending()[0].id, True, "always")
    assert (await task).scope == "once"


async def test_resolving_twice_or_unknown_ids_is_harmless():
    broker = ApprovalBroker(EventBus(), timeout_seconds=5)
    task = asyncio.create_task(_request(broker))
    await asyncio.sleep(0)
    rid = broker.pending()[0].id
    assert broker.resolve(rid, False)
    assert not broker.resolve(rid, True)
    assert not broker.resolve("nope", True)
    assert not (await task).approved


async def test_voice_cannot_approve_high_risk_without_explicit_confirmation():
    broker = ApprovalBroker(EventBus(), timeout_seconds=5)
    task = asyncio.create_task(_request(broker, risk="high"))
    await asyncio.sleep(0)
    assert broker.resolve_latest(True, by="voice") is None          # a casual "yes" is not enough
    assert broker.resolve_latest(True, by="voice", allow_high_risk=True) is not None
    assert (await task).approved


async def test_emergency_stop_declines_everything_waiting():
    broker = ApprovalBroker(EventBus(), timeout_seconds=5)
    tasks = [asyncio.create_task(_request(broker)) for _ in range(3)]
    await asyncio.sleep(0)
    assert broker.decline_all() == 3
    outcomes = await asyncio.gather(*tasks)
    assert all(not o.approved and o.by == "emergency-stop" for o in outcomes)


async def test_emergency_stop_runs_every_handler_even_if_one_fails():
    stop = EmergencyStop()
    seen: list[str] = []

    def broken(reason: str) -> None:
        raise RuntimeError("boom")

    async def slow(reason: str) -> None:
        seen.append(f"async:{reason}")

    stop.register("broken", broken)
    stop.register("sync", lambda reason: seen.append(f"sync:{reason}"))
    stop.register("async", slow)
    failed = await stop.trigger("test")
    assert failed == ["broken"]
    assert seen == ["sync:test", "async:test"]
    assert stop.count == 1 and stop.last_reason == "test"
