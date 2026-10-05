"""
Standalone verification for the request-detection fix (silence bug).

Two things are checked, both fully offline (no server, no API key, no network):

    1. The expanded ``_REQUEST_MARKERS`` lexicon in
       src/brain/policy_engine.py: table-driven (transcript -> expected
       DecisionState) cases covering the phrase families that were missing
       before ("why did", "what happened", "who owns", "when did", "where
       did", "how did", "status on", "explain", ...), plus the negative
       fixtures that must still come out unchanged - most importantly the
       exact sentence pinned in test_memory_rag.py's turn 3
       ("Great, let's agree to finish the security review by next Tuesday.")
       and a plain topic-mention-with-no-question sentence.

    2. The optional LLM intent classifier's wiring in PolicyEngine: a
       hand-rolled scripted stub (no real Anthropic client) proves the
       classifier is consulted only from evaluate_async, only when the
       lexicon alone said "not a request" AND a True verdict could actually
       change the outcome (PolicyEngine._worth_checking), and never from the
       synchronous evaluate().

Usage::

    python tests/test_policy_signals.py

Exit codes: 0 = every check passed, 1 = at least one failed.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.brain import DecisionState, MeetingContext, PolicyEngine
from src.brain.profile_store import InMemoryProfileStore, seed_profiles

USER_ID: str = "durai"
MEETING_ID: str = "policy-signals-check"
SPEAKER: str = "Mike"
PARTICIPANTS: list[str] = ["Durai Kumaran", "Sarah", "Mike"]

_PASSED = 0
_FAILED = 0


def _context(transcript: str) -> MeetingContext:
    return MeetingContext(
        meeting_id=MEETING_ID, user_id=USER_ID, transcript=transcript, speaker=SPEAKER, participants=PARTICIPANTS
    )


def _check(label: str, condition: bool, detail: str) -> None:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"  [✓] {label}")
    else:
        _FAILED += 1
        print(f"  [✗] {label} — {detail}")


# --------------------------------------------------------------------------- #
# 1. Lexicon coverage
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Scenario:
    name: str
    transcript: str
    expected_state: DecisionState


SCENARIOS: tuple[Scenario, ...] = (
    # Previously missed phrasings that must now be recognised as requests.
    Scenario(
        "why-is question on a GREEN topic -> AUTO_REPLY",
        "Why is the api integration progress behind schedule",
        DecisionState.AUTO_REPLY,
    ),
    Scenario(
        "what-happened question on a GREEN topic -> AUTO_REPLY",
        "What happened to the mobile app progress since we last talked",
        DecisionState.AUTO_REPLY,
    ),
    Scenario(
        "what-happened question on a YELLOW topic -> ESCALATE",
        "What happened to the delivery date for this",
        DecisionState.ESCALATE,
    ),
    Scenario(
        "how-did question on a YELLOW topic -> ESCALATE",
        "How did we land on this timeline",
        DecisionState.ESCALATE,
    ),
    # Negative fixtures: must stay exactly as before the lexicon change.
    Scenario(
        "pinned negative (test_memory_rag.py turn 3) -> SILENT",
        "Great, let's agree to finish the security review by next Tuesday.",
        DecisionState.SILENT,
    ),
    Scenario(
        "topic mentioned, nothing asked -> SILENT",
        "The API integration progress looks solid this week.",
        DecisionState.SILENT,
    ),
)


def run_lexicon_scenarios() -> None:
    print("\n1. Request lexicon coverage (synchronous PolicyEngine.evaluate)")
    store = InMemoryProfileStore(seed_profiles())
    engine = PolicyEngine(store)
    for scenario in SCENARIOS:
        decision = engine.evaluate(_context(scenario.transcript))
        _check(
            f"{scenario.name}: got {decision.state.value}",
            decision.state is scenario.expected_state,
            f"expected {scenario.expected_state.value}, reason: {decision.reason}",
        )


# --------------------------------------------------------------------------- #
# 2. Optional LLM intent classifier wiring
# --------------------------------------------------------------------------- #

@dataclass
class ScriptedIntentClassifier:
    """Hand-rolled stub: no Anthropic client, no network."""

    verdict: bool
    calls: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return f"scripted(verdict={self.verdict})"

    async def looks_like_request(self, utterance: str) -> bool:
        self.calls.append(utterance)
        return self.verdict


GREEN_TOPIC_NO_MARKER = "The api integration status hasn't changed much this sprint"
"""Matches the project_status GREEN topic; contains no request marker and no '?'."""

NO_TOPIC_NO_WH_WORD = "The team finished the migration ahead of schedule"
"""No lexical topic match and no interrogative word - must never reach the classifier."""


async def run_intent_classifier_checks() -> None:
    print("\n2. Optional LLM intent classifier gating (asynchronous PolicyEngine.evaluate_async)")
    store = InMemoryProfileStore(seed_profiles())

    stub_true = ScriptedIntentClassifier(verdict=True)
    engine = PolicyEngine(store, intent_classifier=stub_true)
    decision = await engine.evaluate_async(_context(GREEN_TOPIC_NO_MARKER))
    _check(
        "classifier says True on a worth-checking GREEN-topic utterance -> AUTO_REPLY",
        decision.state is DecisionState.AUTO_REPLY,
        f"got {decision.state.value}: {decision.reason}",
    )
    _check("classifier called exactly once", len(stub_true.calls) == 1, f"called {len(stub_true.calls)} times")

    stub_false = ScriptedIntentClassifier(verdict=False)
    engine = PolicyEngine(store, intent_classifier=stub_false)
    decision = await engine.evaluate_async(_context(GREEN_TOPIC_NO_MARKER))
    _check(
        "classifier says False on the same utterance -> stays SILENT",
        decision.state is DecisionState.SILENT,
        f"got {decision.state.value}: {decision.reason}",
    )
    _check("classifier still called exactly once", len(stub_false.calls) == 1, f"called {len(stub_false.calls)} times")

    stub_unused_a = ScriptedIntentClassifier(verdict=True)
    engine = PolicyEngine(store, intent_classifier=stub_unused_a)
    await engine.evaluate_async(_context("Why is the api integration progress behind schedule"))
    _check(
        "lexicon already recognised the request -> classifier not consulted",
        len(stub_unused_a.calls) == 0,
        f"called {len(stub_unused_a.calls)} times",
    )

    stub_unused_b = ScriptedIntentClassifier(verdict=True)
    engine = PolicyEngine(store, intent_classifier=stub_unused_b)
    decision = await engine.evaluate_async(_context(NO_TOPIC_NO_WH_WORD))
    _check(
        "no topic match and no interrogative word -> classifier not consulted",
        len(stub_unused_b.calls) == 0,
        f"called {len(stub_unused_b.calls)} times",
    )
    _check(
        "...and the decision is unaffected by the (unconsulted) classifier -> SILENT",
        decision.state is DecisionState.SILENT,
        f"got {decision.state.value}: {decision.reason}",
    )

    stub_sync = ScriptedIntentClassifier(verdict=True)
    engine = PolicyEngine(store, intent_classifier=stub_sync)
    engine.evaluate(_context(GREEN_TOPIC_NO_MARKER))
    _check(
        "synchronous evaluate() never touches the classifier",
        len(stub_sync.calls) == 0,
        f"called {len(stub_sync.calls)} times",
    )


def main() -> int:
    print("DELEG8 request-detection fix: standalone verification")
    run_lexicon_scenarios()
    asyncio.run(run_intent_classifier_checks())
    print(f"\n{_PASSED} passed, {_FAILED} failed")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
