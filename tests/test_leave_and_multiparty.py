"""
Offline verification for three behaviours that need no live model call:

1. Leave-the-meeting: a planner-detected LEAVE turn asks for one confirmation;
   the yes/no that follows is resolved deterministically (never by the model,
   since "yes"/"no"/"okay" are exactly the words the floor check otherwise
   treats as filler) and only a clear "yes" actually ends the call.
2. Meeting recap material: TurnProcessor._meeting_summary surfaces the
   decisions, commitments and open questions accumulated in meeting memory,
   which is what lets the delegate answer "can you recap what we discussed".
3. Addressing fixes: the delegate's own name is recognised as speech directed
   at it (not silenced as if addressed to some third party), a real third
   party's name still silences it, and the planner's system prompt carries
   different turn-taking guidance for a one-on-one call versus a meeting with
   three or more people.

Usage::

    python tests/test_leave_and_multiparty.py

Exit codes: 0 = every check passed, 1 = at least one failed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.brain.models import MeetingContext  # noqa: E402
from src.brain.planner import (  # noqa: E402
    OutputGuard,
    PlannedReply,
    PlannerInput,
    ResponsePlanner,
    TurnType,
    _meeting_mode_guidance,
)
from src.brain.policy_engine import PolicyEngine  # noqa: E402
from src.brain.profile_store import profile_store  # noqa: E402
from src.escalation.bridge import NullEscalationBridge  # noqa: E402
from src.knowledge.dossier import DossierStore  # noqa: E402
from src.memory.commitments import CommitmentRegistry  # noqa: E402
from src.memory.meeting_state import MeetingStateRegistry  # noqa: E402
from src.turn_processor import TurnComponents, TurnProcessor, _classify_leave_response  # noqa: E402

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
_failures: list[str] = []

USER_ID = "durai"
SPEAKER = "Interviewer"


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one assertion."""
    if condition:
        print(f"  {GREEN}PASS{RESET} {name}")
    else:
        print(f"  {RED}FAIL{RESET} {name}{(' - ' + detail) if detail else ''}")
        _failures.append(name)


def _components(planner: object, *, commitments: CommitmentRegistry | None = None) -> TurnComponents:
    return TurnComponents(
        planner=planner,  # type: ignore[arg-type]  - duck-typed fake, only .plan() is called
        guard=OutputGuard(),
        dossiers=DossierStore(),
        retriever=None,
        commitments=commitments or CommitmentRegistry(),
    )


class ScriptedPlanner:
    """Returns a LEAVE turn for one phrase, a plain SOCIAL reply otherwise. Counts calls."""

    def __init__(self) -> None:
        self.calls = 0

    async def plan(self, request: PlannerInput) -> PlannedReply:
        self.calls += 1
        text = request.context.transcript.lower()
        if "leave the meeting" in text or "end the call" in text:
            return PlannedReply(
                turn_type=TurnType.LEAVE,
                reply="Are you sure you'd like me to leave the call now?",
                confidence=0.9,
                reason="explicit leave request",
            )
        return PlannedReply(turn_type=TurnType.SOCIAL, reply="Sure, go ahead.", confidence=0.9, reason="fallback")


async def test_leave_flow() -> None:
    print(f"\n{BOLD}1. leave-the-meeting confirmation flow{RESET}")
    planner = ScriptedPlanner()
    turns = TurnProcessor(
        engine=PolicyEngine(profile_store), store=profile_store, bridge=NullEscalationBridge("test"),
        memory=MeetingStateRegistry(), components=_components(planner),
    )

    meeting_a = "leave-test-confirm"
    ask = await turns.process_turn(
        MeetingContext(meeting_id=meeting_a, user_id=USER_ID, transcript="You can leave the meeting now.", speaker=SPEAKER)
    )
    check("the first ask does not leave yet", not ask.should_leave)
    check("the planner was consulted for the first ask", planner.calls == 1, f"calls={planner.calls}")
    check("the reply asks for confirmation", bool(ask.response) and "?" in (ask.response or ""), repr(ask.response))

    calls_before_yes = planner.calls
    yes = await turns.process_turn(
        MeetingContext(meeting_id=meeting_a, user_id=USER_ID, transcript="Yes, please.", speaker=SPEAKER)
    )
    check("a clear yes actually leaves", yes.should_leave)
    check("the model is NOT consulted to resolve yes/no", planner.calls == calls_before_yes, f"calls={planner.calls}")
    check("a goodbye is spoken", bool(yes.response), repr(yes.response))

    meeting_b = "leave-test-decline"
    await turns.process_turn(
        MeetingContext(meeting_id=meeting_b, user_id=USER_ID, transcript="Please end the call.", speaker=SPEAKER)
    )
    decline = await turns.process_turn(
        MeetingContext(meeting_id=meeting_b, user_id=USER_ID, transcript="No, stay please.", speaker=SPEAKER)
    )
    check("a clear no cancels the leave", not decline.should_leave)
    check("it says it is staying", "stay" in (decline.response or "").lower(), repr(decline.response))

    meeting_c = "leave-test-ambiguous"
    await turns.process_turn(
        MeetingContext(meeting_id=meeting_c, user_id=USER_ID, transcript="You can leave the meeting now.", speaker=SPEAKER)
    )
    calls_before_ambiguous = planner.calls
    ambiguous = await turns.process_turn(
        MeetingContext(meeting_id=meeting_c, user_id=USER_ID, transcript="What's your favourite colour?", speaker=SPEAKER)
    )
    check("an unrelated follow-up does not leave", not ambiguous.should_leave)
    check(
        "the lapsed confirmation falls through to a normal planned turn",
        planner.calls == calls_before_ambiguous + 1,
        f"calls={planner.calls}",
    )


def test_classify_leave_response() -> None:
    print(f"\n{BOLD}2. deterministic yes/no classification{RESET}")
    check("'Yes.' is an affirmation", _classify_leave_response("Yes.") is True)
    check("'Yeah, go ahead.' is an affirmation", _classify_leave_response("Yeah, go ahead.") is True)
    check("a bare 'Okay.' is treated as an affirmation here", _classify_leave_response("Okay.") is True)
    check("'No.' is a decline", _classify_leave_response("No.") is False)
    check("'Wait, one more thing.' is a decline", _classify_leave_response("Wait, one more thing.") is False)
    check("an unrelated sentence is ambiguous", _classify_leave_response("What's the weather like?") is None)
    check("empty text is ambiguous", _classify_leave_response("") is None)


async def test_meeting_summary() -> None:
    print(f"\n{BOLD}3. meeting summary feeds recap requests{RESET}")

    class CommitmentPlanner:
        async def plan(self, request: PlannerInput) -> PlannedReply:
            if "call me" in request.context.transcript.lower():
                return PlannedReply(
                    turn_type=TurnType.COMMITMENT, reply="Got it, I'll pass that on.",
                    action_description="call back", action_owner="Duraikumaran",
                    action_due_text="tomorrow", confidence=0.9,
                )
            return PlannedReply(turn_type=TurnType.SOCIAL, reply="Sure.", confidence=0.9)

    turns = TurnProcessor(
        engine=PolicyEngine(profile_store), store=profile_store, bridge=NullEscalationBridge("test"),
        memory=MeetingStateRegistry(), components=_components(CommitmentPlanner()),
    )
    meeting_id = "recap-test"
    await turns.process_turn(
        MeetingContext(meeting_id=meeting_id, user_id=USER_ID, transcript="Please call me back tomorrow.", speaker=SPEAKER)
    )

    summary = turns._meeting_summary(MeetingContext(meeting_id=meeting_id, user_id=USER_ID, transcript="recap please"))
    check("the summary carries the commitment", "call" in summary.lower(), summary)
    check("the summary is non-empty", bool(summary.strip()))

    empty = turns._meeting_summary(MeetingContext(meeting_id="never-seen", user_id=USER_ID, transcript="recap please"))
    check("an unknown meeting has an empty summary", empty == "")


def test_delegate_name_addressing() -> None:
    print(f"\n{BOLD}4. addressing: the delegate's own name vs. a third party's{RESET}")
    engine = PolicyEngine(profile_store)

    to_delegate = engine.check_floor(MeetingContext(
        meeting_id="addr-test", user_id=USER_ID, transcript="Hey DELEG8, what's your name?",
        speaker=SPEAKER, participants=["Duraikumaran", SPEAKER],
    ))
    check("'Hey DELEG8, ...' is answered, not silenced", to_delegate.should_respond, to_delegate.reason)

    to_delegate_word = engine.check_floor(MeetingContext(
        meeting_id="addr-test", user_id=USER_ID, transcript="Hey Delegate, what's your name?",
        speaker=SPEAKER, participants=["Duraikumaran", SPEAKER],
    ))
    check("'Hey Delegate, ...' is answered too", to_delegate_word.should_respond, to_delegate_word.reason)

    to_third_party = engine.check_floor(MeetingContext(
        meeting_id="addr-test", user_id=USER_ID, transcript="Hey Sarah, what's your name?",
        speaker=SPEAKER, participants=["Duraikumaran", SPEAKER, "Sarah"],
    ))
    check("'Hey Sarah, ...' still silences the delegate", not to_third_party.should_respond)


def test_meeting_mode_guidance() -> None:
    print(f"\n{BOLD}5. meeting-mode guidance changes with headcount{RESET}")
    solo = _meeting_mode_guidance(None, "Duraikumaran")
    two = _meeting_mode_guidance(2, "Duraikumaran")
    group = _meeting_mode_guidance(5, "Duraikumaran")
    check("unknown headcount defaults to the one-on-one framing", "one-on-one" in solo)
    check("a 2-person call also uses the one-on-one framing", "one-on-one" in two)
    check("a 5-person call gets the group framing instead", "one voice among several" in group)
    check("group framing names both trigger conditions", "DELEG8" in group and "Duraikumaran" in group)

    planner = ResponsePlanner(llm=None)  # type: ignore[arg-type]  - build_system_prompt never touches self._llm
    profile = profile_store.get_profile(USER_ID)
    prompt_group = planner.build_system_prompt(profile, now="Monday", participant_count=5)
    prompt_solo = planner.build_system_prompt(profile, now="Monday", participant_count=None)
    check("the system prompt embeds group guidance for a 5-person call", "one voice among several" in prompt_group)
    check("the system prompt embeds solo guidance by default", "one-on-one" in prompt_solo)


async def main() -> int:
    await test_leave_flow()
    test_classify_leave_response()
    await test_meeting_summary()
    test_delegate_name_addressing()
    test_meeting_mode_guidance()
    print()
    if _failures:
        print(f"{RED}{len(_failures)} check(s) failed:{RESET} " + ", ".join(_failures))
        return 1
    print(f"{GREEN}{BOLD}every check passed{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
