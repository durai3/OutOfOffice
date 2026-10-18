"""
What a meeting session is made of, without the audio and browser imports.

:class:`SessionOptions` are the per-call overrides (command line or web
console). :class:`SessionComponents` bundles the long-lived pieces a session
runs on: the escalation bridge, the knowledge source, the policy engine,
meeting memory, the turn processor and the post-meeting reporter.

The HTTP API builds one bundle for its whole lifetime and hands the *same*
bundle to every delegate it dispatches, so a meeting attended from the web
console shares the Telegram poller, the vector index, and the memory
registry with the REST endpoints. ``python -m src.main join`` builds a
bundle of its own for one call.

This module deliberately imports nothing from :mod:`src.audio` or
:mod:`src.gateway.meet_bot`, so the API can import it even on a machine
where PortAudio or Playwright is not installed; those are pulled in lazily
when a delegate is actually dispatched.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from src.brain.intent import build_intent_classifier
from src.brain.knowledge import KnowledgeSource, ProfileKnowledge
from src.brain.planner import OutputGuard, ResponsePlanner, build_planner
from src.brain.policy_engine import PolicyEngine
from src.brain.profile_store import ProfileStore, profile_store
from src.escalation.bridge import EscalationBridge, NullEscalationBridge
from src.escalation.telegram_bot import TelegramEscalationBridge, TelegramSettings
from src.health import COMPONENT_LLM, report_degraded, report_healthy
from src.knowledge.config import KnowledgeSettings
from src.knowledge.dossier import DossierStore
from src.knowledge.retriever import (
    DocumentKnowledge,
    KnowledgeRetriever,
    build_knowledge_source,
)
from src.knowledge.store import KnowledgeStore
from src.llm import LlmClient, allow_no_llm, build_llm, preflight
from src.memory.commitments import CommitmentRegistry
from src.memory.meeting_state import MeetingStateRegistry, memory_window_from_env
from src.memory.post_meeting import PostMeetingReporter, ReportArchive
from src.turn_processor import TurnComponents, TurnProcessor


@dataclass(frozen=True)
class SessionOptions:
    """Per-call overrides for one meeting session."""

    join_url: str | None = None
    """Meet link; defaults to MEET_JOIN_URL."""
    meeting_id: str | None = None
    """Memory / briefing id; defaults to the Meet code."""
    speaker_label: str = "Meeting"
    """Who the incoming audio represents (there is no diarization yet)."""
    half_duplex: bool = False
    """Mute the bot's mic while it speaks (disables barge-in)."""
    briefing: bool = True
    """Send the executive briefing to Telegram when the session ends."""
    dry_run: bool = False
    """Open the pre-join screen, report what is there, and exit without joining or starting audio."""
    max_call_seconds: float | None = None
    """Leave automatically after this long (safety net for unattended runs)."""


def _print(line: str) -> None:
    print(line, flush=True)


def build_bridge() -> EscalationBridge:
    """Telegram if configured, else a null bridge with a clear warning.

    A dispatched worker deliberately has no bot token - its escalations go home
    over the job token instead (:class:`~src.dispatch.relay.RelayEscalationBridge`,
    installed by :func:`~src.gateway.session.run_meeting_session` right after
    this) - so warning about the missing token there would be actively
    misleading.
    """
    if TelegramSettings.is_configured():
        return TelegramEscalationBridge(TelegramSettings.from_env())
    from src.dispatch.callback import is_worker

    if not is_worker():
        _print("[WARN] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set: escalations and the briefing will not reach your phone.")
    return NullEscalationBridge("Telegram not configured")


async def open_knowledge() -> tuple[KnowledgeSource, KnowledgeStore | None, DocumentKnowledge | None]:
    """Document index plus profile facts; profile facts alone if the index cannot be opened.

    Deliberately more permissive than the HTTP API's ``_open_knowledge`` (which
    refuses to start on a malformed ``KnowledgeConfigError``): missing a live
    meeting over a knowledge-base typo would be worse than joining with
    degraded answers, so every failure here - config or runtime - degrades
    rather than aborting the join.
    """
    try:
        store = KnowledgeStore(KnowledgeSettings.from_env())
        await store.open_async()
    except Exception as exc:
        _print(f"[WARN] document index unavailable ({exc}); answers come from profile facts only")
        return ProfileKnowledge(), None, None
    knowledge, documents = build_knowledge_source(store)
    return knowledge, store, documents


@dataclass
class SessionComponents:
    """The long-lived pieces one or more meeting sessions run on."""

    bridge: EscalationBridge
    knowledge: KnowledgeSource
    knowledge_store: KnowledgeStore | None
    documents: DocumentKnowledge | None
    engine: PolicyEngine
    memory: MeetingStateRegistry
    turns: TurnProcessor
    reporter: PostMeetingReporter
    store: ProfileStore
    llm: LlmClient | None = None
    planner: ResponsePlanner | None = None
    dossiers: DossierStore = None  # type: ignore[assignment]
    commitments: CommitmentRegistry = None  # type: ignore[assignment]

    @classmethod
    def assemble(
        cls,
        *,
        bridge: EscalationBridge,
        knowledge: KnowledgeSource,
        knowledge_store: KnowledgeStore | None,
        documents: DocumentKnowledge | None,
        store: ProfileStore | None = None,
        archive: ReportArchive | None = None,
        memory: MeetingStateRegistry | None = None,
        llm: LlmClient | None = None,
        dossiers: DossierStore | None = None,
        commitments: CommitmentRegistry | None = None,
        user_id: uuid.UUID | None = None,
    ) -> SessionComponents:
        """Wire engine, planner, memory, turn processor and reporter around an open bridge and knowledge.

        ``store``, ``archive``, ``dossiers`` and ``commitments`` are all
        injectable because in a multi-tenant server every one of them is
        per-tenant: the profile store is scoped to one account's principals,
        the archive writes that account's briefs, the commitments registry
        mirrors to that account's rows. A bundle assembled for one user shares
        nothing mutable with a bundle assembled for another.

        ``user_id`` is the real account id (not the brain's principal slug),
        passed only by the multi-tenant server so the reporter can look up
        that account's enabled integrations when a meeting concludes; the
        command-line and worker paths leave it ``None`` and dispatch nothing.
        """
        principal_store: ProfileStore = store if store is not None else profile_store
        engine = PolicyEngine(principal_store, knowledge, intent_classifier=build_intent_classifier())
        registry = memory if memory is not None else MeetingStateRegistry(window=memory_window_from_env())

        client = llm if llm is not None else build_llm()
        planner = build_planner(client)
        dossiers = dossiers if dossiers is not None else DossierStore()
        commitments = commitments if commitments is not None else CommitmentRegistry()
        retriever = KnowledgeRetriever(knowledge_store) if knowledge_store is not None else None

        turns = TurnProcessor(
            engine=engine,
            store=principal_store,
            bridge=bridge,
            memory=registry,
            components=TurnComponents(
                planner=planner,
                guard=OutputGuard(),
                dossiers=dossiers,
                retriever=retriever,
                commitments=commitments,
            ),
        )
        reporter = PostMeetingReporter(
            bridge, store=principal_store, archive=archive, llm=client, commitments=commitments, user_id=user_id
        )
        return cls(
            bridge=bridge,
            knowledge=knowledge,
            knowledge_store=knowledge_store,
            documents=documents,
            engine=engine,
            memory=registry,
            turns=turns,
            reporter=reporter,
            store=principal_store,
            llm=client,
            planner=planner,
            dossiers=dossiers,
            commitments=commitments,
        )

    @classmethod
    async def build(
        cls, *, archive: ReportArchive | None = None, store: ProfileStore | None = None
    ) -> SessionComponents:
        """Open everything from the environment (the command-line and worker path).

        The bridge is not started. ``store`` is supplied by a dispatched worker,
        which holds one principal fetched from the control plane rather than a
        directory of JSON files or a database connection.
        """
        bridge = build_bridge()
        knowledge, knowledge_store, documents = await open_knowledge()
        return cls.assemble(
            bridge=bridge, knowledge=knowledge, knowledge_store=knowledge_store,
            documents=documents, archive=archive, store=store,
        )

    async def check_llm(self, *, fatal: bool = True) -> bool:
        """Verify the language model actually answers before anything depends on it.

        This is the guard against the failure that motivated the whole rebuild: a
        retired model id, caught by nothing, degrading every component to a
        deterministic fallback while the product still appeared to work. A dead
        brain now stops the join rather than producing a delegate that quotes
        fragments of PDFs at people.

        Returns ``True`` when the model is usable. When it is not and ``fatal``
        is set, the caller should refuse to continue unless
        ``DELEG8_ALLOW_NO_LLM`` is on.
        """
        result = await preflight(self.llm)
        _print(result.line())
        if result.ok:
            report_healthy(COMPONENT_LLM)
            return True
        report_degraded(COMPONENT_LLM, "the delegate has no working language model", result.detail)
        if fatal and not allow_no_llm():
            _print("[LLM] refusing to continue: fix the key or model id, or set DELEG8_ALLOW_NO_LLM=true.")
        return False

    async def ensure_dossier(self, user_id: str, principal: str) -> str:
        """Compile the principal's dossier if the documents have changed since the last build.

        Cheap when nothing moved (a content hash comparison, no model call), and
        never overwrites a dossier the principal has edited by hand. Run before
        joining so the delegate is never in a meeting knowing less about its
        principal than the uploaded documents contain.
        """
        from src.knowledge.dossier import DossierBuilder

        builder = DossierBuilder(llm=self.llm, store=self.dossiers)
        try:
            settings = KnowledgeSettings.from_env()
        except Exception as exc:  # noqa: BLE001 - a config typo must not stop the join
            _print(f"[WARN] could not read knowledge settings ({exc}); using whatever dossier exists")
            return self.dossiers.load(user_id).describe()
        dossier = await builder.build_quietly(user_id=user_id, principal=principal, settings=settings)
        return dossier.describe()

    def close_knowledge(self) -> None:
        """Release the vector store's worker thread (the index on disk stays intact)."""
        if self.knowledge_store is not None:
            self.knowledge_store.close()
