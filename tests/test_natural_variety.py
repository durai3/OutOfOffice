"""
Live check: does the delegate actually vary its phrasing within one meeting?

The complaint this verifies: real transcripts showed the exact same opener
("Got it - I'll make sure Duraikumaran...", "I don't have that information on
hand, I'll check with Duraikumaran and come back to you.") repeated nearly
verbatim for every commitment and every deferral in a single call. The system
prompt now explicitly tells the model to look at its own recent replies and
vary its opener; this runs two similar commitment requests and two similar
"I don't know" questions back to back in one real meeting (real Gemini call,
real planner) and checks the pairs are not near-duplicates of each other.

This is a live-model behavioural check, not a deterministic unit test - a
single run failing by a narrow margin is a signal to look, not proof of a
regression, since phrasing is inherently non-deterministic. Compare against
tests/test_fillers.py (deterministic) for the filler-selection mechanism this
complements.

Usage::

    python tests/test_natural_variety.py

Exit codes: 0 = phrasing varied as expected, 1 = it repeated itself, 2 = could not run.
"""

from __future__ import annotations

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

GREEN, RED, YELLOW, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"

MEETING_ID = "variety-check"
USER_ID = "durai"
SPEAKER = "Interviewer"

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one assertion."""
    if condition:
        print(f"  {GREEN}PASS{RESET} {name}")
    else:
        print(f"  {RED}FAIL{RESET} {name}{(' - ' + detail) if detail else ''}")
        _failures.append(name)


def _first_words(text: str, n: int = 3) -> str:
    return " ".join(text.lower().split()[:n])


async def run() -> int:
    health = await preflight(build_llm())
    print(health.line())
    if not health.ok:
        print("\nCannot check phrasing variety without a working language model.")
        return 2

    dossier = DossierStore().load(USER_ID)
    if dossier.is_empty:
        print("\nNo dossier compiled. Run: python -m src.knowledge.dossier")
        return 2

    knowledge, knowledge_store, documents = await open_knowledge()
    components = SessionComponents.assemble(
        bridge=NullEscalationBridge("variety-check"), knowledge=knowledge,
        knowledge_store=knowledge_store, documents=documents,
    )
    turns = components.turns

    commitment_asks = (
        "Can you make sure Duraikumaran calls me back tomorrow at 3pm?",
        "Also, please make sure he emails me the slides by Friday.",
    )
    unknown_asks = (
        "What was your score in the national spelling bee?",
        "What's your favourite prime number?",
    )

    try:
        print(f"\n{BOLD}commitment acknowledgements{RESET}")
        commitment_replies: list[str] = []
        for text in commitment_asks:
            context = MeetingContext(meeting_id=MEETING_ID, user_id=USER_ID, transcript=text, speaker=SPEAKER)
            result = await turns.process_turn(context)
            reply = result.response or ""
            commitment_replies.append(reply)
            print(f"  {DIM}[{SPEAKER}]{RESET} {text}")
            print(f"  {GREEN}[DELEG8]{RESET} {reply}")

        print(f"\n{BOLD}honest deferrals{RESET}")
        unknown_replies: list[str] = []
        for text in unknown_asks:
            context = MeetingContext(meeting_id=MEETING_ID, user_id=USER_ID, transcript=text, speaker=SPEAKER)
            result = await turns.process_turn(context)
            reply = result.response or ""
            unknown_replies.append(reply)
            print(f"  {DIM}[{SPEAKER}]{RESET} {text}")
            print(f"  {GREEN}[DELEG8]{RESET} {reply}")

        print(f"\n{BOLD}checks{RESET}")
        check("both commitments were acknowledged", all(commitment_replies))
        if all(commitment_replies):
            check(
                "the two commitment acknowledgements are not verbatim-identical",
                commitment_replies[0] != commitment_replies[1],
            )
            check(
                "the two commitment acknowledgements do not open with the same words",
                _first_words(commitment_replies[0]) != _first_words(commitment_replies[1]),
                f"both opened with {_first_words(commitment_replies[0])!r}",
            )
        check("both unknowns got an honest deferral", all(unknown_replies))
        if all(unknown_replies):
            check(
                "the two deferrals are not verbatim-identical",
                unknown_replies[0] != unknown_replies[1],
            )
            check(
                "the two deferrals do not open with the same words",
                _first_words(unknown_replies[0]) != _first_words(unknown_replies[1]),
                f"both opened with {_first_words(unknown_replies[0])!r}",
            )
    finally:
        components.close_knowledge()

    print()
    if _failures:
        print(f"{RED}{len(_failures)} check(s) failed:{RESET} " + ", ".join(_failures))
        print(f"{YELLOW}(a single narrow miss here is a prompt-tuning signal, not necessarily a hard bug - phrasing is non-deterministic){RESET}")
        return 1
    print(f"{GREEN}{BOLD}every check passed{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
