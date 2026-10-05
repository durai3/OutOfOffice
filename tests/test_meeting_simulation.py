"""
End-to-end simulation of a whole meeting, without audio or a browser.

Everything between the transcript and the briefing runs for real: floor check,
sensitivity, planner, output guard, meeting memory, the commitment ledger, and
the post-meeting report. Only the microphone, Google Meet and Telegram are
stubbed - the parts this test cannot assert on anyway.

It replays the meeting that motivated the rebuild and asserts on what the old
pipeline got wrong end to end, not turn by turn:

1. Most of the transcript was *discarded*. The voice loop returned early on
   SILENT before reaching memory, so 6 of 17 utterances were recorded and the
   rest - including an action item - vanished. Every utterance must now appear.
2. The delegate answered 6 questions and all six answers were unrelated
   sentences lifted from PDFs. Answers must now be grounded.
3. An action item asked for across four utterances never reached the principal.
   It must now be one commitment, with a resolved deadline, acknowledged aloud.
4. The briefing reported "0 action items assigned".

Usage::

    python tests/test_meeting_simulation.py
    python tests/test_meeting_simulation.py --verbose

Exit codes: 0 = every check passed, 1 = at least one failed, 2 = could not run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from src.brain.models import MeetingContext  # noqa: E402
from src.escalation.bridge import NullEscalationBridge  # noqa: E402
from src.gateway.components import SessionComponents, open_knowledge  # noqa: E402
from src.knowledge.dossier import DossierStore  # noqa: E402
from src.llm import build_llm, preflight  # noqa: E402
from src.memory.post_meeting import render_briefing_plain  # noqa: E402

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"

MEETING_ID = "sim-meeting"
USER_ID = "durai"
SPEAKER = "Interviewer"

# The transcript from the live meeting, verbatim.
TRANSCRIPT: tuple[str, ...] = (
    "Hi. Can you hear me?",
    "Tell me what you know.",
    "What company did I apply to?",
    "What is my GPA?",
    "Is your GPA?",
    "What university are you from?",
    "What city are you from?",
    "What is your name?",
    "Which company?",
    "What are the action items today?",
    "Okay. So I want you to do one thing very clearly. I want you to tell your owner, meaning his or her "
    "action item, I'm going to say now. The action item is that they must give a call to me on WhatsApp.",
    "Tomorrow, 8PM. Okay?",
    "You have to call me.",
    "Tomorrow 8PM.",
)

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one assertion."""
    if condition:
        print(f"  {GREEN}PASS{RESET} {name}")
    else:
        print(f"  {RED}FAIL{RESET} {name}{(' - ' + detail) if detail else ''}")
        _failures.append(name)


async def run(verbose: bool) -> int:
    """Replay the meeting through the real pipeline and check the outcome."""
    health = await preflight(build_llm())
    print(health.line())
    if not health.ok:
        print("\nCannot simulate a meeting without a working language model.")
        return 2

    dossier = DossierStore().load(USER_ID)
    print(dossier.describe())
    if dossier.is_empty:
        print("\nNo dossier compiled. Run: python -m src.knowledge.dossier")
        return 2

    knowledge, knowledge_store, documents = await open_knowledge()
    components = SessionComponents.assemble(
        bridge=NullEscalationBridge("simulation"),
        knowledge=knowledge,
        knowledge_store=knowledge_store,
        documents=documents,
    )
    turns, memory = components.turns, components.memory

    print(f"\n{BOLD}replaying {len(TRANSCRIPT)} utterances{RESET}\n")
    spoken = 0
    grounded = 0
    try:
        for index, text in enumerate(TRANSCRIPT):
            context = MeetingContext(
                meeting_id=MEETING_ID, user_id=USER_ID, transcript=text,
                speaker=SPEAKER, participants=["Duraikumaran", SPEAKER],
            )
            result = await turns.process_turn(context)
            kind = result.turn_type.value if result.turn_type else "-"
            print(f"  {DIM}{index:2}{RESET} {kind:11} {text[:58]}")
            if result.response:
                spoken += 1
                print(f"     {GREEN}->{RESET} {result.response}")
                if result.decision.grounding is not None:
                    grounded += 1
            if result.commitment_ids and verbose:
                print(f"     {DIM}commitments: {', '.join(result.commitment_ids)}{RESET}")

        state = memory.get(MEETING_ID)
        ledger = components.commitments.get(MEETING_ID)

        print(f"\n{BOLD}1. every utterance reaches memory{RESET}")
        heard = [t for t in (state.transcript if state else []) if not t.is_delegate]
        check(
            f"all {len(TRANSCRIPT)} utterances recorded (the old pipeline kept 6 of 17)",
            len(heard) == len(TRANSCRIPT),
            f"recorded {len(heard)}",
        )
        check("the delegate's own replies are in the transcript too",
              any(t.is_delegate for t in (state.transcript if state else [])))

        print(f"\n{BOLD}2. the delegate actually speaks, and is grounded{RESET}")
        check(f"spoke on most turns ({spoken}/{len(TRANSCRIPT)})", spoken >= len(TRANSCRIPT) * 0.6, f"spoke {spoken}")
        check("at least some replies carry grounding evidence", grounded >= 1, f"{grounded} grounded")
        check("no turn produced an empty reply while claiming to speak",
              all(t.text.strip() for t in (state.transcript if state else [])))

        print(f"\n{BOLD}3. the action item survives{RESET}")
        items = ledger.open_items() if ledger else []
        check("a commitment was captured", bool(items), "none captured")
        whatsapp = [c for c in items if "whatsapp" in c.description.lower() or "call" in c.description.lower()]
        check("it is the WhatsApp call", bool(whatsapp), f"captured: {[c.description for c in items]}")
        if whatsapp:
            call = whatsapp[0]
            check("spread over four utterances, it is ONE commitment", len(whatsapp) == 1, f"got {len(whatsapp)}")
            check("with a resolved deadline", bool(call.due_iso or call.due_text), "no deadline")
            check("acknowledged out loud in the meeting", call.acknowledged, "never acknowledged")
            print(f"       {DIM}{call.one_line()}{RESET}")

        print(f"\n{BOLD}4. the briefing reports it{RESET}")
        if state is not None:
            report_state = state
            snapshot = report_state.snapshot()
            briefing, label, fallback = await components.reporter.build_briefing(snapshot)
            check("the briefing lists the action item", bool(briefing.action_items),
                  "action_items is empty - the exact bug from the live run")
            check("it was written by a model, not the template fallback",
                  fallback is None, f"fell back: {fallback}")
            check("the summary is non-trivial", len(briefing.executive_summary.split()) >= 15)
            if verbose or _failures:
                print()
                print(DIM + render_briefing_plain(briefing, snapshot, summariser=label) + RESET)
    finally:
        components.close_knowledge()

    print()
    if _failures:
        print(f"{RED}{len(_failures)} check(s) failed:{RESET} " + ", ".join(_failures))
        return 1
    print(f"{GREEN}{BOLD}every check passed{RESET}")
    return 0


def main(argv: list[str]) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--verbose", action="store_true", help="print the full briefing and commitment ids")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args.verbose))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
