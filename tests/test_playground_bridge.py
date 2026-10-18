"""
Offline check of the playground's approval channel - the stand-in for Telegram.

It is the only new moving part between the browser and the real pipeline, so it
gets its own pin: the delegate must be able to ask and wait, a click must release
it exactly once, silence must time out safely, and nothing may be left hanging.

Usage::

    python tests/test_playground_bridge.py

Exit codes: 0 = every check passed, 1 = at least one failed. No network, no API key.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.api.playground import PlaygroundBridge  # noqa: E402
from src.brain.models import DecisionState, MeetingContext, PolicyDecision, PolicyTier  # noqa: E402
from src.escalation.bridge import EscalationBridge, MessageDispatcher  # noqa: E402
from src.escalation.models import EscalationOutcome  # noqa: E402

_PASSED = 0
_FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion."""
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"  [✓] {label}")
    else:
        _FAILED += 1
        print(f"  [✗] {label}{f' - {detail}' if detail else ''}")


def _context(text: str = "Can you give us a discount?") -> MeetingContext:
    return MeetingContext(meeting_id="playground-test", user_id="alex", transcript=text, speaker="Daniel")


def _decision() -> PolicyDecision:
    return PolicyDecision(
        state=DecisionState.ESCALATE, reason="pricing", topic="pricing_negotiation", tier=PolicyTier.RED
    )


async def main_async() -> None:
    print("\n1. the protocols")
    bridge = PlaygroundBridge(timeout=5)
    check("it is an EscalationBridge", isinstance(bridge, EscalationBridge))
    check("it is a MessageDispatcher", isinstance(bridge, MessageDispatcher))

    print("\n2. ask, wait, be answered")
    task = asyncio.create_task(bridge.send_escalation_request(_context(), "I'll look into it.", _decision()))
    await asyncio.sleep(0.05)
    pending = bridge.pending()
    check("the request is parked and visible", len(pending) == 1 and pending[0].topic == "pricing_negotiation")
    check("the parked request is not yet resolved", not task.done())
    approval = bridge.resolve(pending[0].request_id, EscalationOutcome.DECLINED_POLITELY)
    result = await asyncio.wait_for(task, timeout=2)
    check("a click releases the waiter", result.outcome is EscalationOutcome.DECLINED_POLITELY)
    check("the result says a human decided", result.human_decided and result.delivered)
    check("resolve() returned the approval", approval is not None and approval.request_id == result.request_id)
    check("answering twice is refused", bridge.resolve(result.request_id, EscalationOutcome.APPROVED) is None)
    check("an unknown id is refused", bridge.resolve("nope", EscalationOutcome.APPROVED) is None)
    check("nothing is left pending", bridge.pending() == [])

    print("\n3. silence times out safely")
    quiet = PlaygroundBridge(timeout=0.1)
    result = await quiet.send_escalation_request(_context(), "ok", _decision())
    check("no answer -> TIMEOUT, not a hang", result.outcome is EscalationOutcome.TIMEOUT)
    check("a timeout is not a human decision", not result.human_decided)

    print("\n4. stop() unblocks everything")
    closing = PlaygroundBridge(timeout=30)
    waiter = asyncio.create_task(closing.send_escalation_request(_context(), "ok", _decision()))
    await asyncio.sleep(0.05)
    await closing.stop()
    result = await asyncio.wait_for(waiter, timeout=2)
    check("a stopped bridge resolves its waiter as TIMEOUT", result.outcome is EscalationOutcome.TIMEOUT)

    print("\n5. the briefing channel")
    receipt = await bridge.send_text("*bold*", markdown_v2=True, plain_fallback="plain briefing")
    check("a briefing is recorded as delivered", receipt.delivered and receipt.channel == "playground")
    check("the plain-text form is what the page shows", bridge.outbox == ["plain briefing"])

    print("\n6. a change hook fires but cannot break the delegate")
    seen: list[int] = []
    hooked = PlaygroundBridge(timeout=5)
    hooked.on_change(lambda: seen.append(1))
    waiter = asyncio.create_task(hooked.send_escalation_request(_context(), "ok", _decision()))
    await asyncio.sleep(0.05)
    check("the hook fired when a request arrived", len(seen) == 1)
    hooked.on_change(lambda: 1 / 0)
    hooked.resolve(hooked.pending()[0].request_id, EscalationOutcome.APPROVED)
    result = await asyncio.wait_for(waiter, timeout=2)
    check("a hook that raises does not break resolution", result.outcome is EscalationOutcome.APPROVED)


def check_enum_references() -> None:
    """Every ``EscalationOutcome.X`` named anywhere in src/ must exist.

    ``turn_processor`` once named ``EscalationOutcome.UNAVAILABLE``, which does not
    exist. The attribute was only evaluated after a human answered, inside a
    background task, so the AttributeError was swallowed by asyncio and every
    approved escalation silently never spoke. Nothing short of a real approval
    exercised it - so this checks the names statically, and the end-to-end
    playground test exercises the path itself.
    """
    import re

    print("\n7. every EscalationOutcome reference resolves")
    missing: list[str] = []
    for path in (PROJECT_ROOT / "src").rglob("*.py"):
        for name in set(re.findall(r"EscalationOutcome\.([A-Z_]+)\b", path.read_text())):
            if not hasattr(EscalationOutcome, name):
                missing.append(f"{path.relative_to(PROJECT_ROOT)}: {name}")
    check("no reference to a missing outcome", not missing, "; ".join(missing))


def main() -> int:
    print("DELEG8 playground approval bridge: offline verification")
    asyncio.run(main_async())
    check_enum_references()
    print(f"\n{_PASSED} passed, {_FAILED} failed")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
