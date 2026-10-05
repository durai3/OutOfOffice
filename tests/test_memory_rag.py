"""
Standalone verification for Sprint 4: the dynamic knowledge base (RAG) and
the meeting memory engine.

What it does, step by step:

    1. Writes a sample document to data/documents/project_alpha.md
       (architecture, stack, deployment dates, owners).
    2. Ingests it into ChromaDB (data/chroma_db) with the CPU embedder.
    3. Runs three meeting turns through the full turn processor:
         Turn 1  "What database is Project Alpha using?"
                 -> AUTO_REPLY grounded in the document (RAG), not in the profile.
         Turn 2  "Can you guarantee a 50% discount for enterprise clients?"
                 -> ESCALATE: RED pricing policy, alert on your phone.
         Turn 3  "Great, let's agree to finish the security review by next Tuesday."
                 -> the delegate stays silent, memory records a decision and an
                    action item (owner + resolved due date).
    4. Concludes the meeting: the executive briefing is generated (Claude when
       ANTHROPIC_API_KEY is set, otherwise the deterministic template) and
       pushed to your Telegram chat.

Default mode uses the real Telegram bridge. A FastAPI server started WITH
Telegram credentials must not be running at the same time (Telegram allows
one poller per bot token). Turn 2 waits up to --timeout seconds for you to
tap Approve / Reject / Decline Politely; not tapping is fine, the question
is then listed as unresolved in the briefing.

Usage::

    python tests/test_memory_rag.py               # real Telegram
    python tests/test_memory_rag.py --timeout 45  # longer window for your tap on turn 2
    python tests/test_memory_rag.py --offline     # no Telegram: escalation resolves UNDELIVERED,
                                                  #   briefing is printed here instead
    python tests/test_memory_rag.py --reset       # wipe and rebuild the vector index first

Exit codes: 0 = every check passed, 1 = a check failed, 2 = configuration problem.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from src.brain import (  # noqa: E402
    DecisionState,
    GroundingSource,
    MeetingContext,
    PolicyEngine,
    PolicyTier,
    profile_store,
)
from src.escalation import (  # noqa: E402
    EscalationBridge,
    EscalationConfigError,
    EscalationOutcome,
    NullEscalationBridge,
    TelegramEscalationBridge,
    TelegramSettings,
)
from src.knowledge import (  # noqa: E402
    KnowledgeConfigError,
    KnowledgeSettings,
    KnowledgeStore,
    RetrievalStatus,
    build_knowledge_source,
)
from src.knowledge.ingest import DocumentIngestor  # noqa: E402
from src.memory import MeetingStateRegistry, PostMeetingReporter  # noqa: E402
from src.memory.extraction import resolve_deadline  # noqa: E402
from src.turn_processor import TurnProcessor, TurnResult  # noqa: E402

USER_ID: str = "durai"
MEETING_ID: str = "sprint4-memory-rag"
PARTICIPANTS: list[str] = ["Durai Kumaran", "Sarah", "Mike"]
DOCUMENT_NAME: str = "project_alpha.md"

SAMPLE_DOCUMENT: str = """\
# Project Alpha

## Overview

Project Alpha is the customer analytics platform that replaces the legacy reporting stack.
The programme is led by Durai Kumaran, with Sarah as engineering lead and Mike as product owner.
It serves the enterprise tier first and the self-serve tier one quarter later.

## Architecture

Project Alpha uses PostgreSQL 16 on Amazon RDS as its primary database, with Redis 7 for caching and session storage.
The backend is written in Python 3.12 using FastAPI and runs as containers on AWS ECS Fargate behind an Application Load Balancer.
Events flow through Amazon Kinesis into a dbt-managed warehouse on Snowflake for analytics workloads.
The web client is a React 19 single-page application served from CloudFront.

## Tech stack

| Layer      | Choice                              |
| ---------- | ----------------------------------- |
| Database   | PostgreSQL 16 (Amazon RDS)          |
| Cache      | Redis 7                             |
| Backend    | Python 3.12, FastAPI, ECS Fargate   |
| Streaming  | Amazon Kinesis                      |
| Warehouse  | Snowflake with dbt                  |
| Frontend   | React 19 on CloudFront              |

## Deployment timeline

- User acceptance testing starts on 29 September 2026 and runs for two weeks.
- A code freeze begins on 7 October 2026; only critical fixes are merged after that date.
- Production go-live for Project Alpha is scheduled for Tuesday 14 October 2026.
- The self-serve tier follows in January 2027.

## Security

The security review is owned by the platform team and must be completed before the production deployment.
Secrets are stored in AWS Secrets Manager and rotated every 90 days.
All customer data is encrypted at rest with AWS KMS and in transit with TLS 1.3.
"""


@dataclass(frozen=True)
class Turn:
    """One simulated utterance and the routing state it must produce."""

    name: str
    speaker: str
    transcript: str
    expected_state: DecisionState


TURNS: tuple[Turn, Turn, Turn] = (
    Turn("RAG-grounded question", "Mike", "What database is Project Alpha using?", DecisionState.AUTO_REPLY),
    Turn("RED pricing request", "Sarah", "Can you guarantee a 50% discount for enterprise clients?", DecisionState.ESCALATE),
    Turn("Agreement with a deadline", "Mike", "Great, let's agree to finish the security review by next Tuesday.", DecisionState.SILENT),
)

_HUMAN_LABEL: dict[EscalationOutcome, str] = {
    EscalationOutcome.APPROVED: "APPROVED",
    EscalationOutcome.REJECTED: "REJECTED",
    EscalationOutcome.DECLINED_POLITELY: "DECLINED POLITELY",
    EscalationOutcome.TIMEOUT: "TIMEOUT (no button tapped in time)",
    EscalationOutcome.UNDELIVERED: "UNDELIVERED (alert never reached Telegram)",
}


class Checker:
    """Collects PASS/FAIL checks and prints them as they happen."""

    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        """Record one check."""
        self.results.append((name, ok, detail))
        print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
        return ok

    @property
    def failures(self) -> int:
        """Number of failed checks."""
        return sum(1 for _, ok, _ in self.results if not ok)


def step(number: int, title: str) -> None:
    """Print a numbered step banner."""
    print(f"\n[{number}] {title}")


def build_bridge(args: argparse.Namespace) -> EscalationBridge | None:
    """Telegram unless --offline or unconfigured; None on a configuration error."""
    if args.offline:
        return NullEscalationBridge("--offline")
    if not TelegramSettings.is_configured():
        print("    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; running as if --offline.")
        print("    Setup guide: python tests/test_escalation.py --setup-help")
        return NullEscalationBridge("Telegram not configured")
    try:
        settings = TelegramSettings.from_env()
    except EscalationConfigError as exc:
        print(f"\nConfiguration problem: {exc}")
        return None
    if args.timeout is not None:
        settings = dataclasses.replace(settings, timeout_seconds=args.timeout)
    return TelegramEscalationBridge(settings)


def print_turn(result: TurnResult) -> None:
    """Human-readable dump of one processed turn."""
    decision = result.decision
    tier = decision.tier.value if decision.tier else "-"
    print(f"    [BRAIN]    {decision.state.value} topic={decision.topic or '-'} tier={tier}")
    print(f"               {decision.reason}")
    if decision.grounding is not None:
        print(f"    [RAG]      {decision.grounding.describe()}")
    if result.escalation is not None:
        who = f" by {result.escalation.responded_by}" if result.escalation.responded_by else ""
        print(f"    [TELEGRAM] {_HUMAN_LABEL[result.escalation.outcome]}{who} after {result.escalation.response_seconds:.1f}s"
              + (f" ({result.escalation.detail})" if result.escalation.detail else ""))
    print(f"    [BOT]      {result.response!r}" if result.response else "    [BOT]      (silent)")
    if result.memory is not None:
        update = result.memory
        print(f"    [MEMORY]   active_topic={update.active_topic!r}"
              + (" (changed)" if update.topic_changed else ""))
        for item in update.new_decisions:
            print(f"               + decision: {item.text}  [{item.origin.value}, by {item.proposed_by}]")
        for action in update.new_actions:
            owner = f"{action.owner}{' (inferred)' if action.owner_inferred else ''}"
            print(f"               + action: {action.description}  owner={owner} due={action.due_label or '-'}")
        for question in update.new_questions:
            print(f"               + open question: {question.text}  [{question.outcome}]")


async def run(args: argparse.Namespace) -> int:
    """Execute the four steps and return the exit code."""
    checks = Checker()
    print("DELEG8 Sprint 4 verification: dynamic knowledge base (RAG) + meeting memory engine")

    try:
        settings = KnowledgeSettings.from_env()
    except KnowledgeConfigError as exc:
        print(f"\nConfiguration problem: {exc}")
        return 2

    # ---------------------------------------------------------------- 1
    step(1, f"Creating the sample document {settings.documents_dir / DOCUMENT_NAME}")
    settings.documents_dir.mkdir(parents=True, exist_ok=True)
    document_path = settings.documents_dir / DOCUMENT_NAME
    document_path.write_text(SAMPLE_DOCUMENT, encoding="utf-8")
    print(f"    wrote {len(SAMPLE_DOCUMENT)} chars ({SAMPLE_DOCUMENT.count(chr(10))} lines)")

    # ---------------------------------------------------------------- 2
    step(2, f"Ingesting into ChromaDB at {settings.chroma_dir} ({settings.embedding_model}, cpu)")
    store = KnowledgeStore(settings)
    bridge: EscalationBridge | None = None
    try:
        await store.open_async()
        if args.reset:
            await store.run(store.reset)
            print("    index reset")
        report = await DocumentIngestor(store, settings).ingest_directory(force=args.reset)
        for item in report.files:
            print(f"    [{item.status:8}] {item.source}: {item.chunks} chunks, {item.characters} chars")
        print(f"    {report.summary()}")
        ours = next((f for f in report.files if f.source == DOCUMENT_NAME), None)
        checks.check("sample document is in the index", ours is not None and ours.status != "failed" and ours.chunks > 0,
                     f"{ours.status}, {ours.chunks} chunks" if ours else "not found")
        checks.check("index is non-empty", report.index_size > 0, f"{report.index_size} chunks")

        knowledge, documents = build_knowledge_source(store, settings)
        retriever = documents.retriever
        grounded = await retriever.retrieve(TURNS[0].transcript)
        print(f"    probe {TURNS[0].transcript!r}: {grounded.status.value} best={grounded.best_similarity} "
              f"threshold={grounded.threshold} -> {grounded.citations()}")
        checks.check("retriever grounds the Project Alpha question",
                     grounded.status is RetrievalStatus.GROUNDED and any(DOCUMENT_NAME in c for c in grounded.citations()),
                     f"best similarity {grounded.best_similarity}")
        unknown_query = "What is the capital of France?"
        unknown = await retriever.retrieve(unknown_query)
        print(f"    probe {unknown_query!r}: {unknown.status.value} best={unknown.best_similarity}")
        checks.check("off-topic question is flagged UNKNOWN_CONTEXT", unknown.status is RetrievalStatus.UNKNOWN_CONTEXT,
                     f"best similarity {unknown.best_similarity}")

        # ------------------------------------------------------------ 3
        step(3, "Simulating three meeting turns through the turn processor")
        bridge = build_bridge(args)
        if bridge is None:
            return 2
        try:
            await bridge.start()
        except EscalationConfigError as exc:
            print(f"\n{exc}")
            return 2
        print(f"    escalation bridge: {bridge.describe()}")
        engine = PolicyEngine(profile_store, knowledge)
        memory = MeetingStateRegistry()
        processor = TurnProcessor(engine=engine, store=profile_store, bridge=bridge, memory=memory)
        reporter = PostMeetingReporter(bridge, store=profile_store)
        print(f"    knowledge: {knowledge.describe()}")
        print(f"    briefing:  {reporter.describe()}")
        offline = isinstance(bridge, NullEscalationBridge)

        results: list[TurnResult] = []
        for number, turn in enumerate(TURNS, start=1):
            print(f"\n    --- Turn {number}: {turn.name} ---")
            print(f"    [{turn.speaker.upper()}] {turn.transcript}")
            if turn.expected_state is DecisionState.ESCALATE and not offline:
                print(f"    [TELEGRAM] alert sent; tap a button on your phone or wait for the timeout...")
            context = MeetingContext(
                meeting_id=MEETING_ID, user_id=USER_ID, transcript=turn.transcript,
                speaker=turn.speaker, participants=PARTICIPANTS,
            )
            result = await processor.process_turn(context)
            results.append(result)
            print_turn(result)
            checks.check(f"turn {number} routes to {turn.expected_state.value}", result.state is turn.expected_state,
                         f"got {result.state.value}")
            update = result.memory
            assert update is not None

            if number == 1:
                grounding = result.decision.grounding
                checks.check("turn 1 answer is grounded in the documents, not the profile",
                             grounding is not None and grounding.source is GroundingSource.DOCUMENTS,
                             grounding.describe() if grounding else "no grounding")
                checks.check("turn 1 answer names PostgreSQL", bool(result.response) and "postgresql" in (result.response or "").lower(),
                             result.response or "")
                checks.check("turn 1 sets the active topic to Project Alpha",
                             (update.active_topic or "").lower() == "project alpha", repr(update.active_topic))
            elif number == 2:
                decision = result.decision
                checks.check("turn 2 is governed by the RED pricing rule",
                             decision.tier is PolicyTier.RED and decision.topic == "pricing_negotiation",
                             f"{decision.topic}/{decision.tier.value if decision.tier else None}")
                checks.check("turn 2 was not answered from the knowledge base", decision.grounding is None)
                escalation = result.escalation
                if offline:
                    checks.check("turn 2 escalation resolved UNDELIVERED (offline)",
                                 escalation is not None and escalation.outcome is EscalationOutcome.UNDELIVERED)
                else:
                    checks.check("turn 2 alert was delivered to Telegram", escalation is not None and escalation.delivered,
                                 escalation.detail or "" if escalation else "no escalation")
                approved = escalation is not None and escalation.outcome in (EscalationOutcome.APPROVED, EscalationOutcome.REJECTED)
                if approved:
                    checks.check("turn 2 human verdict recorded as a decision", bool(update.new_decisions))
                else:
                    checks.check("turn 2 recorded as an unresolved question", bool(update.new_questions))
                checks.check("turn 2 sets the active topic to pricing", "pricing" in (update.active_topic or "").lower(),
                             repr(update.active_topic))
            else:
                decisions = [d.text.lower() for d in update.new_decisions]
                checks.check("turn 3 captured the agreement as a decision",
                             any("security review" in d for d in decisions), "; ".join(decisions) or "none")
                actions = update.new_actions
                expected_due = resolve_deadline("next tuesday", context.timestamp)
                action = next((a for a in actions if "security review" in a.description.lower()), None)
                checks.check("turn 3 captured the action item with an owner",
                             action is not None and action.owner == turn.speaker,
                             f"{action.description} -> {action.owner}" if action else "no action item")
                checks.check("turn 3 resolved 'by next Tuesday' to a date",
                             action is not None and action.due_date == expected_due,
                             f"{action.due_date} (expected {expected_due})" if action else "no action item")
                checks.check("turn 3 sets the active topic to the security review",
                             "security review" in (update.active_topic or "").lower(), repr(update.active_topic))

        state = memory.get(MEETING_ID)
        assert state is not None
        snapshot = state.snapshot()
        print(f"\n    meeting state: topic={snapshot.active_topic!r} window={len(snapshot.speaker_turns)} "
              f"decisions={len(snapshot.agreed_decisions)} actions={len(snapshot.pending_actions)} "
              f"open={len(snapshot.open_questions)} stats={snapshot.stats.model_dump()}")
        checks.check("rolling window holds every turn and reply", len(snapshot.speaker_turns) == len(snapshot.transcript),
                     f"{len(snapshot.speaker_turns)} of {len(snapshot.transcript)}")

        # ------------------------------------------------------------ 4
        step(4, "Concluding the meeting: executive briefing -> Telegram")
        post = await reporter.conclude(state, registry=memory)
        print(f"    summariser: {post.summariser}" + (f"  (fallback reason: {post.fallback_reason})" if post.fallback_reason else ""))
        print(f"    delivery:   {'delivered' if post.receipt.delivered else 'NOT delivered'} via {post.receipt.channel}"
              + (f", message id {post.receipt.message_id}" if post.receipt.message_id else "")
              + (f" ({post.receipt.detail})" if post.receipt.detail else ""))
        print("\n" + "\n".join("    | " + line for line in post.text.splitlines()) + "\n")

        briefing = post.briefing
        checks.check("briefing has a 2-3 sentence executive summary",
                     2 <= briefing.executive_summary.count(". ") + 1 <= 4 and len(briefing.executive_summary) > 40,
                     f"{len(briefing.executive_summary)} chars")
        checks.check("briefing lists the security review action item",
                     any("security review" in a.description.lower() for a in briefing.action_items),
                     "; ".join(a.description for a in briefing.action_items) or "none")
        checks.check("briefing names an owner for the action item",
                     all(a.owner.strip() for a in briefing.action_items) and bool(briefing.action_items))
        escalated = results[1].escalation
        if escalated is None or escalated.outcome not in (EscalationOutcome.APPROVED, EscalationOutcome.REJECTED):
            checks.check("briefing lists the discount question as unresolved",
                         any("discount" in q.lower() for q in briefing.unresolved_questions),
                         "; ".join(briefing.unresolved_questions) or "none")
        else:
            checks.check("briefing records the human's verdict on the discount",
                         any("discount" in d.lower() for d in briefing.decisions_made),
                         "; ".join(briefing.decisions_made) or "none")
        if offline:
            checks.check("briefing was rendered (offline, not dispatched)", bool(post.text) and not post.receipt.delivered)
        else:
            checks.check("briefing was pushed to Telegram", post.receipt.delivered, post.receipt.detail or "")
        checks.check("meeting memory released after conclusion", memory.get(MEETING_ID) is None)
    finally:
        if bridge is not None:
            await bridge.stop()
        store.close()

    total = len(checks.results)
    print(f"\n{total - checks.failures}/{total} checks passed.")
    return 1 if checks.failures else 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="do not use Telegram; print the briefing instead")
    parser.add_argument("--timeout", type=float, default=20.0, help="seconds to wait for your tap on turn 2 (default 20)")
    parser.add_argument("--reset", action="store_true", help="wipe the vector index and re-embed everything")
    parser.add_argument("--log-level", default="WARNING", help="python log level for component logs")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """Entry point."""
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return asyncio.run(run(args))


def test_memory_rag_offline() -> None:
    """pytest entry point: the whole flow without Telegram."""
    assert main(["--offline"]) == 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
