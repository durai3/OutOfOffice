"""
Standalone verification for Sprint 2: the Telegram Escalation Bridge.

What it does, step by step:

    1. Simulates a high-stakes meeting prompt.
    2. Runs it through the Policy Engine and confirms the state is ESCALATE.
    3. Sends a real alert, with buttons, to the configured Telegram chat.
    4. Waits (bounded by ESCALATION_TIMEOUT_SECONDS) for you to tap a button.
    5. Prints your action and the sentence the delegate would say in the meeting.

Default mode runs the bridge in this process. A FastAPI server must not be
running at the same time with the same ``TELEGRAM_BOT_TOKEN`` - Telegram
allows exactly one poller per token, which is precisely why the server owns a
single :class:`~src.escalation.hub.TelegramHub` and routes by chat id rather
than letting each tenant poll.

``--via-server`` exercises the multi-tenant path instead: it creates a
throwaway account, links *your* chat id to it, and calls
``POST /api/v1/process-turn``. The alert you receive is then proof that the
hub resolved a tenant and reached the right chat - not merely that a bot can
send a message.

Usage::

    python tests/test_escalation.py               # in-process, real Telegram
    python tests/test_escalation.py --timeout 60  # longer decision window
    python tests/test_escalation.py --via-server  # through the running API

Exit codes: 0 = a human tapped a button, 1 = timed out or undelivered or
the engine did not escalate, 2 = configuration problem (instructions printed).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from client import ApiError, test_account  # noqa: E402

from src.brain import (  # noqa: E402
    DecisionState,
    MeetingContext,
    PolicyEngine,
    profile_store,
)
from src.escalation import (  # noqa: E402
    SETUP_INSTRUCTIONS,
    EscalationConfigError,
    EscalationOutcome,
    TelegramEscalationBridge,
    TelegramSettings,
    final_response,
    propose_response,
)

PROMPT: str = "Can you commit to delivering the security audit by this Friday?"
USER_ID: str = "durai"
MEETING_ID: str = "sprint2-escalation-check"
SPEAKER: str = "Mike"
PARTICIPANTS: list[str] = ["Durai Kumaran", "Sarah", "Mike"]

_HUMAN_LABEL: dict[EscalationOutcome, str] = {
    EscalationOutcome.APPROVED: "✅ APPROVED",
    EscalationOutcome.REJECTED: "❌ REJECTED",
    EscalationOutcome.DECLINED_POLITELY: "🙏 DECLINED POLITELY",
    EscalationOutcome.TIMEOUT: "⏰ TIMEOUT (no button tapped in time)",
    EscalationOutcome.UNDELIVERED: "⚠️ UNDELIVERED (alert never reached Telegram)",
}


def step(number: int, title: str) -> None:
    """Print a numbered step banner."""
    print(f"\n[{number}] {title}")


def build_context() -> MeetingContext:
    """The simulated meeting turn under test."""
    return MeetingContext(
        meeting_id=MEETING_ID,
        user_id=USER_ID,
        transcript=PROMPT,
        speaker=SPEAKER,
        participants=PARTICIPANTS,
    )


def load_settings(timeout_override: float | None) -> TelegramSettings | None:
    """Read Telegram settings; print setup instructions and return None if missing."""
    try:
        settings = TelegramSettings.from_env()
    except EscalationConfigError as exc:
        print(f"\nConfiguration problem: {exc}")
        return None
    if timeout_override is not None:
        settings = dataclasses.replace(settings, timeout_seconds=timeout_override)
    return settings


async def run_in_process(timeout_override: float | None) -> int:
    """Drive the engine and the Telegram bridge directly from this process."""
    print("DELEG8 Sprint 2 verification: Telegram Escalation Bridge (in-process)")

    settings = load_settings(timeout_override)
    if settings is None:
        return 2

    step(1, "Simulating a high-stakes meeting prompt")
    context = build_context()
    print(f"    meeting      : {context.meeting_id}")
    print(f"    speaker      : {context.speaker}")
    print(f"    transcript   : {context.transcript!r}")

    step(2, "Running the Policy Engine")
    engine = PolicyEngine(profile_store)
    decision = engine.evaluate(context)
    print(f"    state        : {decision.state.value}")
    print(f"    topic / tier : {decision.topic} / {decision.tier.value if decision.tier else None}")
    print(f"    triggers     : {', '.join(decision.matched_triggers) or 'none'}")
    print(f"    reason       : {decision.reason}")
    if decision.state is not DecisionState.ESCALATE:
        print(f"\nExpected ESCALATE but the engine returned {decision.state.value}; nothing to escalate.")
        return 1
    print("    -> ESCALATE detected; a human decision is required.")

    profile = profile_store.get_profile(USER_ID)
    proposed = propose_response(context, decision, profile)

    step(3, "Connecting to Telegram and sending the alert")
    bridge = TelegramEscalationBridge(settings)
    try:
        await bridge.start()
    except EscalationConfigError as exc:
        print(f"\n{exc}")
        return 2
    print(f"    bridge       : {bridge.describe()}")
    print(f"    proposed     : {proposed!r}")
    print(f"    buttons      : [✅ Approve] [❌ Reject] [🙏 Decline Politely]")

    try:
        step(4, f"Waiting up to {settings.timeout_seconds:g}s for you to tap a button on your phone")
        result = await bridge.send_escalation_request(context, proposed, decision)
    finally:
        await bridge.stop()

    step(5, "Resolution")
    print(f"    delivered    : {result.delivered}" + (f" (message id {result.message_id})" if result.message_id else ""))
    print(f"    your action  : {_HUMAN_LABEL[result.outcome]}")
    if result.responded_by:
        print(f"    responded by : {result.responded_by} after {result.response_seconds:.1f}s")
    if result.detail:
        print(f"    detail       : {result.detail}")
    print(f"\n    The delegate says in the meeting:\n    \"{final_response(result.outcome, proposed)}\"\n")
    return 0 if result.human_decided else 1


def _link_chat(user_id: str, chat_id: str) -> None:
    """Point a throwaway account's notification channel at the operator's own chat.

    Normally a user links their chat by sending ``/link <code>`` to the bot.
    This test cannot type into Telegram, so it writes the verified channel
    directly - which is the same row the ``/link`` handler would have written,
    and exercises the same routing.
    """
    import uuid as _uuid

    from src.db import repo
    from src.db.base import init_engine, session_scope

    init_engine()
    with session_scope() as session:
        repo.upsert_channel(
            session,
            user_id=_uuid.UUID(user_id),
            kind="telegram",
            address=str(chat_id),
            verified=True,
            link_code=None,
            label="escalation test",
        )


def run_via_server(base_url: str, timeout_override: float | None) -> int:
    """Exercise the multi-tenant escalation path on an already running server."""
    print(f"DELEG8 Sprint 2 verification: Telegram Escalation Bridge (via {base_url})")
    wait_seconds = (timeout_override or float(os.getenv("ESCALATION_TIMEOUT_SECONDS", "30") or 30)) + 20

    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        print("\nTELEGRAM_CHAT_ID is not set, so there is no chat to route the alert to.")
        return 2

    step(1, "Creating a throwaway account and linking your Telegram chat to it")
    client = test_account("Escalation Tester")
    _link_chat(client.user_id, chat_id)
    # The workspace caches its bridge, so force it to be rebuilt from the row
    # that was just written rather than the "nothing linked" one it opened with.
    client.put("/api/v1/settings/preferences", {"escalation_timeout_seconds": timeout_override or 30.0})
    print(f"    account      : {client.email} (principal {client.principal})")
    print(f"    channel      : {client.get('/api/v1/settings')['channel']['describe']}")

    step(2, "Simulating a high-stakes meeting prompt")
    context = build_context()
    print(f"    transcript   : {context.transcript!r}")

    step(3, "Calling POST /api/v1/process-turn (the server decides, escalates, and waits)")
    print("    Tap a button on your phone within the server's escalation window.")
    payload = context.model_dump(mode="json")
    payload["user_id"] = client.principal
    try:
        turn: dict[str, Any] = client.call("POST", "/api/v1/process-turn", payload)
    except ApiError as exc:
        print(f"\n{exc}")
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"\nCannot reach {base_url} ({exc}). Start the server with: python -m src.main serve")
        return 2

    step(4, "Resolution")
    decision = turn["decision"]
    escalation = turn.get("escalation") or {}
    print(f"    state        : {turn['state']}  (topic={decision.get('topic')}, tier={decision.get('tier')})")
    if turn["state"] != DecisionState.ESCALATE.value:
        print(f"\nExpected ESCALATE but the server returned {turn['state']}.")
        print(f"    Leaving {client.email} in place so the failure can be investigated.")
        return 1

    # The escalation is deliberately non-blocking: the delegate speaks a holding
    # line at once and your verdict is spoken later as a follow-up, because a
    # delegate standing mute in a call for thirty seconds is worse than one that
    # says "I'm checking with Dana". So what a single HTTP call can prove is
    # that the turn escalated, that something was said immediately, and that the
    # alert reached *this tenant's* chat - which is what you just saw on your
    # phone. The verdict itself is exercised end to end by the in-process mode
    # and by a real meeting.
    print(f"    proposed     : {turn.get('proposed_response')!r}")
    print(f"\n    The delegate said immediately:\n    \"{turn['response']}\"\n")
    if escalation:
        outcome = EscalationOutcome(escalation["outcome"])
        print(f"    your action  : {_HUMAN_LABEL[outcome]}")
        if escalation.get("responded_by"):
            print(f"    responded by : {escalation['responded_by']} after {escalation['response_seconds']:.1f}s")
    else:
        print("    Your verdict will be spoken as a follow-up; check the alert on your phone.")

    if not turn.get("response"):
        print("\nThe delegate said nothing while waiting, which is the failure this flow exists to prevent.")
        print(f"    Leaving {client.email} in place so the failure can be investigated.")
        return 1
    client.destroy()
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--via-server", action="store_true", help="use a running API instead of an in-process bridge")
    parser.add_argument("--base-url", default=os.getenv("DELEG8_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--timeout", type=float, default=None, help="override ESCALATION_TIMEOUT_SECONDS (in-process only)")
    parser.add_argument("--setup-help", action="store_true", help="print the Telegram setup instructions and exit")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """Entry point."""
    args = parse_args(argv)
    if args.setup_help:
        print(SETUP_INSTRUCTIONS)
        return 0
    if args.via_server:
        return run_via_server(args.base_url, args.timeout)
    return asyncio.run(run_in_process(args.timeout))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
