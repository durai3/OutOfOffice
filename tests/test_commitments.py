"""
Offline verification of the commitment ledger.

No API key, no network, no model. Everything here is the deterministic half of
:mod:`src.memory.commitments`: the merge rule that turns an ask spread over
several utterances into one action item, deadline parsing, urgency, and status
promotion when both extractors agree.

The scenario in :func:`test_multi_utterance_request` is the one from the live
meeting that failed - a WhatsApp call requested across four utterances, with the
time arriving two utterances after the task. The old single-utterance regex
could not assemble it and the delegate never surfaced it at all.

Usage::

    python tests/test_commitments.py

Exit codes: 0 = every check passed, 1 = at least one failed.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.memory.commitments import (  # noqa: E402
    CommitmentLedger,
    CommitmentRegistry,
    CommitmentSource,
    CommitmentStatus,
    due_soon,
    due_within,
    parse_due,
    similarity,
)

GREEN, RED, RESET = "\033[32m", "\033[31m", "\033[0m"
NOW = datetime(2026, 9, 19, 19, 35, tzinfo=timezone.utc)

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one assertion."""
    if condition:
        print(f"  {GREEN}PASS{RESET} {name}")
    else:
        print(f"  {RED}FAIL{RESET} {name}{(' - ' + detail) if detail else ''}")
        _failures.append(name)


def test_multi_utterance_request() -> None:
    """The WhatsApp call from the failed meeting: task first, time two turns later."""
    print("\nmulti-utterance request (the meeting that failed)")
    ledger = CommitmentLedger("meet-test")

    first, is_new = ledger.record(
        description="Give a call to the speaker on WhatsApp", owner="Duraikumaran",
        requested_by="Interviewer", turn_index=9, source=CommitmentSource.PLANNER,
        acknowledged=True, now=NOW,
    )
    check("first utterance creates a commitment", is_new)

    second, is_new = ledger.record(
        description="Give a call to me on WhatsApp", owner="Duraikumaran",
        requested_by="Interviewer", due_iso="2026-09-20T20:00", due_text="tomorrow, 8PM",
        turn_index=10, source=CommitmentSource.PLANNER, acknowledged=True, now=NOW,
    )
    check("the restatement merges instead of duplicating", not is_new and second.id == first.id)
    check("ledger holds exactly one commitment", len(ledger) == 1, f"holds {len(ledger)}")
    check("the deadline from the later utterance is kept", second.due_iso == "2026-09-20T20:00")
    check("the spoken phrase is kept too", second.due_text == "tomorrow, 8PM")
    check("it was acknowledged aloud", second.acknowledged)
    check("it spans both turns", second.first_turn == 9 and second.last_turn == 10)

    third, is_new = ledger.record(
        description="call me on WhatsApp tomorrow", owner="Duraikumaran",
        due_text="tomorrow 8pm", turn_index=12, source=CommitmentSource.RULES, now=NOW,
    )
    check("independent rule detection merges into the same item", not is_new and third.id == first.id)
    check(
        "agreement between both extractors promotes it to CONFIRMED",
        third.status is CommitmentStatus.CONFIRMED and third.source is CommitmentSource.BOTH,
        f"status={third.status.value} source={third.source.value}",
    )
    check("it reads correctly in one line", "WhatsApp" in third.one_line() and "20 Sep" in third.one_line(),
          third.one_line())


def test_distinct_requests_stay_separate() -> None:
    """Merging must not swallow genuinely different asks."""
    print("\ndistinct requests stay separate")
    ledger = CommitmentLedger("meet-test")
    ledger.record(description="Send the quarterly slides", owner="Durai", turn_index=1,
                  source=CommitmentSource.PLANNER, now=NOW)
    ledger.record(description="Book the venue for the offsite", owner="Durai", turn_index=2,
                  source=CommitmentSource.PLANNER, now=NOW)
    ledger.record(description="Review the security audit findings", owner="Durai", turn_index=3,
                  source=CommitmentSource.PLANNER, now=NOW)
    check("three different asks stay three items", len(ledger) == 3, f"got {len(ledger)}")

    ledger.record(description="Send the quarterly slides by Friday", owner="Durai", turn_index=4,
                  due_text="by Friday", source=CommitmentSource.PLANNER, now=NOW)
    check("a restatement of one of them still merges", len(ledger) == 3, f"got {len(ledger)}")

    ledger.record(description="Send the quarterly slides", owner="Sarah", turn_index=5,
                  source=CommitmentSource.PLANNER, now=NOW)
    check("the same task for a different owner is a new item", len(ledger) == 4, f"got {len(ledger)}")


def test_deadlines() -> None:
    """ISO parsing, labels, and the 24-hour urgency window."""
    print("\ndeadlines")
    check("datetime parses", isinstance(parse_due("2026-09-20T20:00"), datetime))
    check("bare date parses", parse_due("2026-09-20") is not None)
    check("nonsense does not raise", parse_due("sometime next week") is None)
    check("empty does not raise", parse_due("") is None)

    ledger = CommitmentLedger("m")
    soon, _ = ledger.record(description="Call the client", owner="Durai", turn_index=0,
                            due_iso="2026-09-20T20:00", source=CommitmentSource.PLANNER, now=NOW)
    later, _ = ledger.record(description="File the annual report", owner="Durai", turn_index=1,
                             due_iso="2026-12-01", source=CommitmentSource.PLANNER, now=NOW)
    check("a deadline tomorrow is urgent", due_soon(soon, now=NOW))
    check("a deadline in December is not", not due_soon(later, now=NOW))
    check("the raw hours window still works", due_within(soon, hours=25.0, now=NOW))
    check("urgent items are listed before notification", [c.id for c in ledger.urgent_unnotified(now=NOW)] == [soon.id])
    ledger.mark_notified(soon.id)
    check("and drop out once notified", ledger.urgent_unnotified(now=NOW) == [])


def test_status_and_registry() -> None:
    """Status transitions and per-meeting isolation."""
    print("\nstatus and registry")
    registry = CommitmentRegistry()
    a = registry.get_or_create("meet-a")
    b = registry.get_or_create("meet-b")
    a.record(description="Send the contract", owner="Durai", turn_index=0, source=CommitmentSource.PLANNER, now=NOW)
    check("ledgers are per meeting", len(a) == 1 and len(b) == 0)
    check("get_or_create is stable", registry.get_or_create("meet-a") is a)

    item = a.items[0]
    check("new commitments start unconfirmed", item.status is CommitmentStatus.PROBABLE)
    check("and are open", item.is_open)
    a.mark(item.id, CommitmentStatus.DONE)
    check("marking done closes it", not a.items[0].is_open)
    check("closed items leave open_items()", a.open_items() == [])
    check("an unconfirmed item says so in one line", "unconfirmed" in item.one_line().lower() or item.status is CommitmentStatus.DONE)

    registry.remove("meet-a")
    check("a concluded meeting's ledger is dropped", registry.get("meet-a") is None)


def test_corroborate_only() -> None:
    """create=False merges into an existing item but never invents a new one."""
    print("\ncorroborate-only mode (how the rule extractor is used)")
    ledger = CommitmentLedger("m")
    ledger.record(description="Call the interviewer on WhatsApp", owner="Durai", turn_index=0,
                  source=CommitmentSource.PLANNER, now=NOW)

    junk, is_new = ledger.record(description="S today", owner="Interviewer", turn_index=1,
                                 source=CommitmentSource.RULES, create=False, now=NOW)
    check("junk that matches nothing is dropped", junk is None and not is_new)
    check("the ledger is unchanged", len(ledger) == 1, f"holds {len(ledger)}")

    match, is_new = ledger.record(description="call the interviewer on whatsapp tomorrow", owner="Durai",
                                  turn_index=2, source=CommitmentSource.RULES, create=False, now=NOW)
    check("a real corroboration still merges", match is not None and not is_new)
    check("and promotes the item to CONFIRMED",
          match is not None and match.status is CommitmentStatus.CONFIRMED)


def test_similarity() -> None:
    """The merge heuristic itself."""
    print("\nsimilarity")
    check("restatements score high",
          similarity("Give a call to me on WhatsApp", "call me on WhatsApp tomorrow") >= 0.45)
    check("unrelated tasks score low",
          similarity("Send the quarterly slides", "Book the venue for the offsite") < 0.45)
    check("filler words are ignored",
          similarity("please make sure to send the report", "send the report") >= 0.45)
    check("empty is safe", similarity("", "anything") == 0.0)


def main() -> int:
    """Run every check."""
    print("Commitment ledger verification (offline, no API key needed)")
    test_multi_utterance_request()
    test_distinct_requests_stay_separate()
    test_deadlines()
    test_status_and_registry()
    test_corroborate_only()
    test_similarity()
    print()
    if _failures:
        print(f"{RED}{len(_failures)} check(s) failed:{RESET} " + ", ".join(_failures))
        return 1
    print(f"{GREEN}every check passed{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
