"""
Turn orchestration: floor check -> planner -> guard -> speech, memory, commitments.

This is where the inversion described in :mod:`src.brain.planner` actually
happens. The shape of a turn used to be:

    policy state machine -> (document hit?) -> speak, else SILENT

which made a retrieval miss and "nothing to say" the same outcome, and made
silence the default for everything the lexicon did not recognise. It is now:

    floor check -> sensitivity -> planner -> output guard -> speak

Each stage has one job. The floor check (deterministic, sub-millisecond) only
asks whether the turn belongs to someone else. The sensitivity check tells the
planner what is restricted. The planner writes the reply. The guard verifies it
before a word is spoken. Silence is now a decision the planner makes, not the
default that happens when nothing else fires.

Two behavioural changes matter as much as the structure:

* **Escalation no longer blocks the room.** The old flow sent a Telegram alert
  and then waited, in silence, for up to the escalation timeout - in a live
  meeting that is a dead air gap of tens of seconds with a person watching a
  muted delegate. Now the delegate speaks its holding line immediately ("I
  can't commit to that myself - I'm checking with Durai"), the alert goes out
  in the background, and the human's answer is spoken later as a follow-up
  through :attr:`TurnProcessor.followups`.
* **Every turn reaches memory**, including silent ones. Previously the
  orchestrator returned early on SILENT and never called into memory at all, so
  the majority of a meeting's transcript - and any action item inside it - was
  simply discarded.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import BaseModel, Field, computed_field

from src.brain.models import (
    DecisionState,
    Grounding,
    GroundingSource,
    MeetingContext,
    PolicyDecision,
    PolicyTier,
    TraceStep,
)
from src.brain.planner import (
    GuardedReply,
    GuardVerdict,
    OutputGuard,
    PlannedReply,
    PlannerInput,
    ResponsePlanner,
    TurnType,
)
from src.brain.policy_engine import PolicyEngine, SensitivityCheck
from src.brain.profile_store import ProfileStore
from src.brain.text import matched_phrases, normalise
from src.escalation.bridge import EscalationBridge
from src.escalation.models import EscalationOutcome, EscalationResult
from src.escalation.responses import final_response, propose_response
from src.health import COMPONENT_PLANNER, report_degraded, report_healthy
from src.knowledge.dossier import DossierStore
from src.knowledge.retriever import KnowledgeRetriever
from src.llm import LlmError
from src.memory.commitments import CommitmentRegistry, CommitmentSource
from src.memory.extraction import extract_actions, leading_addressee
from src.memory.meeting_state import MeetingStateRegistry, TurnUpdate

logger = logging.getLogger("deleg8.turn")

_LEAVE_AFFIRM_RE = re.compile(
    r"^\W*(?:yes|yeah|yep|yup|sure|correct|confirmed|okay|ok|please\s*(?:do|leave)|go\s*ahead|"
    r"do\s*it|that'?s\s*right|leave)\b",
    re.IGNORECASE,
)
_LEAVE_DECLINE_RE = re.compile(
    r"^\W*(?:no|nah|nope|don'?t|do\s*not|wait|hold\s*on|actually|never\s*mind|nevermind|stay|"
    r"cancel|not\s*yet|one\s*more|hang\s*on)\b",
    re.IGNORECASE,
)
_LEAVE_GOODBYE: str = "Sure - I'll leave the call now. Goodbye!"
_LEAVE_CANCELLED: str = "Okay, I'll stay on the call."


def _classify_leave_response(text: str) -> bool | None:
    """Read a reply to "are you sure you'd like me to leave?" deterministically.

    Handled outside the planner on purpose: the words that answer this question
    ("yes", "no", "okay") are exactly the acknowledgement noise the floor check
    otherwise treats as filler and silences, and the decision to actually leave a
    live call must not depend on a model being reachable. Returns ``None`` when
    the reply is neither - the confirmation has lapsed and the utterance should
    be handled as a normal turn instead.
    """
    body = normalise(text)
    if not body:
        return None
    if _LEAVE_DECLINE_RE.match(body):
        return False
    if _LEAVE_AFFIRM_RE.match(body):
        return True
    return None

_QUESTION_WORDS: tuple[str, ...] = (
    "what", "why", "who", "when", "where", "which", "how",
    "can", "could", "would", "will", "do", "does", "did", "is", "are", "shall", "should",
)


def _asks_something(text: str) -> bool:
    """True when ``text`` reads as a question (a question mark, or a question word)."""
    return "?" in text or bool(matched_phrases(normalise(text), _QUESTION_WORDS))


_HISTORY_TURNS: int = 12
"""Recent utterances handed to the planner. Without this, a follow-up like
"Which company?" is unanswerable - and that exact question got silence in
testing because the old answer path saw the question and nothing else."""

_RETRIEVAL_FLOOR: float = 0.22
"""Similarity below the configured gate at which chunks are still shown to the
planner. Safe to be generous now: an irrelevant passage can no longer produce a
wrong answer, because the planner decides what is relevant and the guard
verifies every claim. The old threshold had to be strict because whatever
cleared it was spoken almost verbatim."""

_MAX_DOCUMENT_CHARS: int = 6000

_RECOVERY_COOLDOWN_SECONDS: float = 90.0
"""Minimum gap between spoken apologies when the model is unreachable."""


class TurnResult(BaseModel):
    """What happened for one meeting turn, end to end."""

    state: DecisionState = Field(..., description="Routing state, derived from the planned turn type.")
    response: str | None = Field(default=None, description="What the delegate says. None when silent.")
    proposed_response: str | None = Field(default=None, description="Draft sent to the human during escalation, if any.")
    decision: PolicyDecision = Field(..., description="Brain decision, including trace and grounding.")
    escalation: EscalationResult | None = Field(default=None, description="How the human resolved it, when resolved in time.")
    memory: TurnUpdate | None = Field(default=None, description="What meeting memory recorded (None when memory is off).")
    turn_type: TurnType | None = Field(default=None, description="The planner's classification of this turn.")
    guard_verdict: GuardVerdict | None = Field(default=None, description="What the output guard did.")
    commitment_ids: list[str] = Field(default_factory=list, description="Commitments created or updated by this turn.")
    should_leave: bool = Field(
        default=False,
        description="True once leaving the call has been confirmed; the voice loop should end the session after speaking.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def spoke(self) -> bool:
        """True when the delegate has something to say."""
        return self.response is not None


@dataclass
class FollowUp:
    """Something to say after the turn that prompted it has finished."""

    meeting_id: str
    text: str
    reason: str


@dataclass
class TurnComponents:
    """The collaborators a :class:`TurnProcessor` needs beyond the brain itself."""

    planner: ResponsePlanner | None = None
    guard: OutputGuard = field(default_factory=OutputGuard)
    dossiers: DossierStore = field(default_factory=DossierStore)
    retriever: KnowledgeRetriever | None = None
    commitments: CommitmentRegistry = field(default_factory=CommitmentRegistry)


class TurnProcessor:
    """Runs a meeting turn: decide, speak, remember, and chase the human when needed."""

    def __init__(
        self,
        engine: PolicyEngine,
        store: ProfileStore,
        bridge: EscalationBridge,
        memory: MeetingStateRegistry | None = None,
        components: TurnComponents | None = None,
    ) -> None:
        """Wire the brain, the profile store, the escalation channel, memory and the planner stack."""
        self._engine = engine
        self._store = store
        self._bridge = bridge
        self._memory = memory
        self._components = components or TurnComponents()
        self.followups: asyncio.Queue[FollowUp] = asyncio.Queue()
        """Replies that arrived after their turn ended (a human answering an
        escalation). The voice loop drains this alongside new utterances."""
        self._escalation_tasks: set[asyncio.Task[None]] = set()
        self._last_recovery_at: float | None = None
        self._pending_leave: dict[str, bool] = {}
        """Meetings currently awaiting a yes/no answer to "are you sure you'd like
        me to leave?", keyed by meeting_id. Consumed by :meth:`_maybe_resolve_leave_confirmation`."""

    # ------------------------------------------------------------------ #
    # Accessors
    # ------------------------------------------------------------------ #

    @property
    def memory(self) -> MeetingStateRegistry | None:
        """The registry turns are recorded into, if any."""
        return self._memory

    @property
    def bridge(self) -> EscalationBridge:
        """Where ESCALATE turns go."""
        return self._bridge

    @bridge.setter
    def bridge(self, bridge: EscalationBridge) -> None:
        """Swap the escalation channel in place.

        A tenant linking or unlinking their Telegram chat must take effect on a
        processor that is already assembled - and possibly already in a meeting -
        without rebuilding the whole component bundle underneath it.
        """
        self._bridge = bridge

    @property
    def components(self) -> TurnComponents:
        """The planner, guard, dossiers, retriever and commitments this processor runs on."""
        return self._components

    @property
    def commitments(self) -> CommitmentRegistry:
        """The commitment ledgers, one per meeting."""
        return self._components.commitments

    @property
    def planner(self) -> ResponsePlanner | None:
        """The response planner, when a model is configured."""
        return self._components.planner

    def describe(self) -> str:
        """Status line."""
        planner = self._components.planner
        return planner.describe() if planner is not None else "planner: none (no language model configured)"

    # ------------------------------------------------------------------ #
    # One turn
    # ------------------------------------------------------------------ #

    async def process_turn(
        self, context: MeetingContext, *, on_planning: Callable[[], None] | None = None
    ) -> TurnResult:
        """Run one utterance end to end.

        ``on_planning`` fires once the floor check has passed and the model call
        is about to start. The voice loop uses it to decide whether to play a
        short holding phrase: it is the only moment at which "this turn will
        produce speech, and it will not be instant" is known, and without it a
        filler would sometimes play over a turn that turns out to be silent.

        Raises:
            ProfileNotFoundError: if ``context.user_id`` has no Representation Policy.
        """
        trace: list[TraceStep] = []
        profile = self._store.get_profile(context.user_id)

        leave_result = self._maybe_resolve_leave_confirmation(context)
        if leave_result is not None:
            return leave_result

        floor = self._engine.check_floor(context)
        trace.append(TraceStep(stage="FLOOR_CHECK", outcome=f"{floor.verdict.value}: {floor.reason}"))
        if not floor.should_respond:
            return self._finish(
                context, self._silent_decision(floor.reason, trace), response=None,
                turn_type=TurnType.SILENT, guard_verdict=None, escalation=None, commitment_ids=[],
            )

        sensitivity = self._engine.check_sensitivity(context)
        trace.append(TraceStep(stage="SENSITIVITY", outcome=sensitivity.describe()))

        planner = self._components.planner
        if planner is None:
            report_degraded(COMPONENT_PLANNER, "no language model configured; the delegate cannot answer")
            trace.append(TraceStep(stage="PLANNER", outcome="unavailable: no language model configured"))
            return self._finish(
                context, self._silent_decision("No language model is configured.", trace), response=None,
                turn_type=TurnType.SILENT, guard_verdict=None, escalation=None, commitment_ids=[],
            )

        dossier = self._components.dossiers.load(context.user_id).markdown
        documents = await self._retrieve(context.transcript)
        history = self._history(context)
        meeting_summary = self._meeting_summary(context)
        if on_planning is not None:
            on_planning()

        try:
            plan = await planner.plan(
                PlannerInput(
                    context=context, profile=profile, dossier=dossier,
                    documents=documents, history=history, meeting_summary=meeting_summary,
                )
            )
            report_healthy(COMPONENT_PLANNER)
        except LlmError as exc:
            report_degraded(COMPONENT_PLANNER, "the delegate could not compose a reply", str(exc))
            trace.append(TraceStep(stage="PLANNER", outcome=f"failed: {exc}"))
            recovery = self._recovery_line()
            decision = self._silent_decision(f"The planner failed: {exc}", trace)
            if recovery is not None:
                decision = decision.model_copy(update={"suggested_response": recovery})
            return self._finish(
                context, decision, response=recovery,
                turn_type=TurnType.SILENT if recovery is None else TurnType.SOCIAL,
                guard_verdict=None, escalation=None, commitment_ids=[],
            )

        trace.append(
            TraceStep(
                stage="PLANNER",
                outcome=f"{plan.turn_type.value} (confidence {plan.confidence:.2f}): {plan.reason or 'no reason given'}",
            )
        )

        if plan.turn_type is TurnType.SILENT and floor.addressee and _asks_something(context.transcript):
            # Being asked something by name is the one case where silence is always
            # wrong: from the room it is indistinguishable from the delegate having
            # crashed. The planner is told this, and a small model still sometimes
            # goes quiet on "Durai, what does everyone think...", so the rule is
            # enforced here rather than hoped for. The line says nothing about the
            # principal and asserts no fact, so the guard has nothing to object to.
            principal = profile.display_name
            plan = plan.model_copy(
                update={
                    "turn_type": TurnType.UNKNOWN,
                    "reply": f"I'm not sure where I stand on that one - let me check with {principal} and come back to you.",
                    "states_personal_fact": False,
                    "supporting_quotes": [],
                    "reason": f"{plan.reason or 'planner chose silence'} (overridden: addressed by name, so never left unanswered)",
                }
            )
            trace.append(TraceStep(stage="PLANNER", outcome="silence overridden: a question addressed by name always gets an answer"))

        sources = f"{dossier}\n\n{documents}"
        guarded = self._components.guard.check(plan, profile=profile, sources=sources)
        trace.append(TraceStep(stage="GUARD", outcome=f"{guarded.verdict.value}: {guarded.detail}"))

        if plan.turn_type is TurnType.LEAVE and guarded.spoken_text is not None:
            self._pending_leave[context.meeting_id] = True
            trace.append(TraceStep(stage="LEAVE_REQUEST", outcome="awaiting confirmation before leaving"))

        decision = self._build_decision(plan, guarded, sensitivity, trace, documents)
        escalation = await self._maybe_escalate(context, plan, guarded, sensitivity, decision)
        result = self._finish(
            context, decision, response=guarded.spoken_text, turn_type=plan.turn_type,
            guard_verdict=guarded.verdict, escalation=escalation, commitment_ids=[],
        )
        result.commitment_ids = self._record_commitments(context, plan, guarded, result)
        return result

    def _maybe_resolve_leave_confirmation(self, context: MeetingContext) -> TurnResult | None:
        """Resolve a pending "are you sure you'd like me to leave?" deterministically.

        Returns ``None`` when there is nothing pending, or when the reply was
        neither a clear yes nor no (the confirmation lapses and this utterance
        falls through to the normal floor check / planner instead - the person
        may simply have moved on to something else).
        """
        if not self._pending_leave.pop(context.meeting_id, False):
            return None
        verdict = _classify_leave_response(context.transcript)
        if verdict is None:
            return None

        trace = [TraceStep(stage="LEAVE_CONFIRMATION", outcome=f"confirmed={verdict}")]
        response = _LEAVE_GOODBYE if verdict else _LEAVE_CANCELLED
        decision = PolicyDecision(
            state=DecisionState.AUTO_REPLY,
            reason="Leaving the call, as confirmed." if verdict else "Staying on the call; leaving was declined.",
            trace=trace,
            suggested_response=response,
        )
        result = self._finish(
            context, decision, response=response, turn_type=TurnType.LEAVE,
            guard_verdict=GuardVerdict.PASSED, escalation=None, commitment_ids=[],
        )
        result.should_leave = verdict
        return result

    async def complete_turn(self, context: MeetingContext, decision: PolicyDecision) -> TurnResult:
        """Back-compatible entry point for callers holding a pre-computed decision.

        The planner produces the decision and the reply in one step, so there is
        no longer a useful split here; this simply records the turn.
        """
        return self._finish(
            context, decision, response=decision.suggested_response,
            turn_type=None, guard_verdict=None, escalation=None, commitment_ids=[],
        )

    # ------------------------------------------------------------------ #
    # Context assembly
    # ------------------------------------------------------------------ #

    def _history(self, context: MeetingContext) -> list[tuple[str, str]]:
        """Recent ``(speaker, text)`` pairs from meeting memory, oldest first."""
        if self._memory is None:
            return []
        state = self._memory.get(context.meeting_id)
        if state is None:
            return []
        return [(turn.speaker, turn.text) for turn in list(state.speaker_turns)[-_HISTORY_TURNS:]]

    def _meeting_summary(self, context: MeetingContext) -> str:
        """A structured recap of the whole meeting so far, for recap/summary requests.

        ``_history`` only covers the last :data:`_HISTORY_TURNS` utterances, which
        is not enough to answer "recap what we've discussed" once a meeting runs
        past that window. This instead draws on the same extracted decisions,
        action items and open questions the post-meeting briefing uses, so a
        recap stays accurate for the length of the whole call.
        """
        if self._memory is None:
            return ""
        state = self._memory.get(context.meeting_id)
        if state is None or state.is_empty:
            return ""
        parts: list[str] = []
        if state.active_topic:
            parts.append(f"Current topic: {state.active_topic}")
        if state.agreed_decisions:
            parts.append("Decisions agreed:\n" + "\n".join(f"- {d.text}" for d in state.agreed_decisions))
        ledger = self._components.commitments.get(context.meeting_id)
        if ledger is not None and ledger.items:
            parts.append("Action items / commitments:\n" + "\n".join(f"- {c.one_line()}" for c in ledger.items))
        if state.open_questions:
            parts.append(
                "Open questions not yet answered:\n"
                + "\n".join(f"- {q.text} ({q.outcome})" for q in state.open_questions)
            )
        return "\n\n".join(parts)

    async def _retrieve(self, query: str) -> str:
        """Passages that might bear on ``query``, as a block of text for the planner.

        Retrieval no longer gates anything, so it can afford to be generous: the
        planner judges relevance and the guard verifies every claim against this
        same text.
        """
        retriever = self._components.retriever
        if retriever is None or not query.strip():
            return ""
        try:
            result = await retriever.retrieve(query, min_similarity=_RETRIEVAL_FLOOR)
        except Exception as exc:  # noqa: BLE001 - retrieval must never fail a live turn
            logger.warning("retrieval failed for %r: %s", query[:60], exc)
            return ""
        if not result.chunks:
            return ""
        parts: list[str] = []
        budget = _MAX_DOCUMENT_CHARS
        for chunk in result.chunks:
            body = chunk.body.strip()[:budget]
            if not body:
                continue
            parts.append(f"--- {chunk.citation} (similarity {chunk.similarity:.2f}) ---\n{body}")
            budget -= len(body)
            if budget <= 0:
                break
        return "\n\n".join(parts)

    # ------------------------------------------------------------------ #
    # Decision assembly
    # ------------------------------------------------------------------ #

    @staticmethod
    def _silent_decision(reason: str, trace: list[TraceStep]) -> PolicyDecision:
        return PolicyDecision(
            state=DecisionState.SILENT, reason=reason,
            trace=[*trace, TraceStep(stage="SILENT", outcome=reason)],
        )

    @staticmethod
    def _build_decision(
        plan: PlannedReply,
        guarded: GuardedReply,
        sensitivity: SensitivityCheck,
        trace: list[TraceStep],
        documents: str,
    ) -> PolicyDecision:
        """Map a planned and guarded reply onto the :class:`PolicyDecision` contract.

        Kept because the console, the live feed, meeting memory and the archive
        all speak this language; the planner slots into it rather than forcing a
        rewrite of everything downstream.
        """
        if guarded.spoken_text is None:
            state = DecisionState.SILENT
        elif plan.needs_human or plan.turn_type is TurnType.DEFLECT or sensitivity.forbidden:
            state = DecisionState.ESCALATE
        else:
            state = DecisionState.AUTO_REPLY

        topic = sensitivity.rule.topic if sensitivity.rule is not None else plan.turn_type.value
        tier = sensitivity.tier
        grounding: Grounding | None = None
        if plan.states_personal_fact and guarded.verdict is GuardVerdict.PASSED:
            grounding = Grounding(
                source=GroundingSource.DOCUMENTS,
                subject="dossier" if not documents else "dossier + documents",
                similarity=None,
                citations=plan.supporting_quotes[:3],
                detail=f"turn_type={plan.turn_type.value}; confidence={plan.confidence:.2f}; guard={guarded.verdict.value}",
            )

        reason = plan.reason.strip() or guarded.detail
        return PolicyDecision(
            state=state,
            reason=reason or "The planner decided this turn.",
            topic=topic,
            tier=tier,
            matched_triggers=list(sensitivity.triggers),
            suggested_response=guarded.spoken_text,
            grounding=grounding,
            trace=[*trace, TraceStep(stage=state.value, outcome=reason or guarded.detail)],
        )

    # ------------------------------------------------------------------ #
    # Escalation (never blocks the room)
    # ------------------------------------------------------------------ #

    async def _maybe_escalate(
        self,
        context: MeetingContext,
        plan: PlannedReply,
        guarded: GuardedReply,
        sensitivity: SensitivityCheck,
        decision: PolicyDecision,
    ) -> EscalationResult | None:
        """Alert the human in the background when this turn needs them.

        Returns ``None`` immediately: the delegate has already said its holding
        line, and whatever the human replies arrives later on
        :attr:`followups`. Blocking here is what used to leave a live meeting in
        silence for the length of the escalation timeout.
        """
        needs_human = (
            plan.needs_human
            or plan.turn_type is TurnType.DEFLECT
            or sensitivity.forbidden
            or guarded.verdict is not GuardVerdict.PASSED
        )
        if not needs_human:
            return None

        proposed = guarded.spoken_text or plan.reply or context.transcript
        if decision.tier in (PolicyTier.RED, PolicyTier.YELLOW):
            # What the human is asked to approve is the *answer* they would be committing to
            # ("comfortable moving forward in principle", "we can commit to that"), not the
            # holding line the delegate has already said aloud. Otherwise "Approve" would just
            # make it repeat "I can't speak to that" and the approval would mean nothing.
            try:
                proposed = propose_response(context, decision, self._store.get_profile(context.user_id))
            except Exception:  # noqa: BLE001 - keep the holding line rather than lose the escalation
                logger.warning("could not draft an approvable answer; proposing the holding line", exc_info=True)
        logger.info(
            "escalating meeting=%s topic=%s tier=%s via %s (non-blocking)",
            context.meeting_id, decision.topic, decision.tier.value if decision.tier else None,
            self._bridge.describe(),
        )
        task = asyncio.create_task(
            self._escalate_in_background(context, proposed, decision),
            name=f"deleg8-escalate-{context.meeting_id}",
        )
        self._escalation_tasks.add(task)
        task.add_done_callback(self._escalation_tasks.discard)
        return None

    async def _escalate_in_background(
        self, context: MeetingContext, proposed: str, decision: PolicyDecision
    ) -> None:
        """Wait on the human off the critical path and queue whatever they decide."""
        try:
            result = await self._bridge.send_escalation_request(context, proposed, decision)
        except Exception as exc:  # noqa: BLE001 - a broken bridge must not kill the meeting
            logger.warning("escalation failed for %s: %s", context.meeting_id, exc)
            return

        if self._memory is not None:
            state = self._memory.get(context.meeting_id)
            if state is not None:
                state.record_escalation_outcome(context, decision, result)

        if result.outcome in {EscalationOutcome.TIMEOUT, EscalationOutcome.UNDELIVERED}:
            logger.info("escalation for %s ended as %s; nothing to say", context.meeting_id, result.outcome.value)
            return
        spoken = final_response(result.outcome, proposed)
        if not spoken:
            return
        await self.followups.put(
            FollowUp(
                meeting_id=context.meeting_id,
                text=spoken,
                reason=f"{result.outcome.value} by {result.responded_by or 'the principal'}",
            )
        )

    # ------------------------------------------------------------------ #
    # Memory and commitments
    # ------------------------------------------------------------------ #

    def _finish(
        self,
        context: MeetingContext,
        decision: PolicyDecision,
        *,
        response: str | None,
        turn_type: TurnType | None,
        guard_verdict: GuardVerdict | None,
        escalation: EscalationResult | None,
        commitment_ids: list[str],
    ) -> TurnResult:
        """Record the turn in memory and assemble the result.

        Every turn passes through here, silent ones included - which is the fix
        for the transcript that recorded 6 of 17 utterances and lost an action
        item with them.
        """
        memory: TurnUpdate | None = None
        if self._memory is not None:
            state = self._memory.get_or_create(context.meeting_id, context.user_id)
            memory = state.record_turn(context, decision, response=response, escalation=escalation)
        return TurnResult(
            state=decision.state,
            response=response,
            proposed_response=None,
            decision=decision,
            escalation=escalation,
            memory=memory,
            turn_type=turn_type,
            guard_verdict=guard_verdict,
            commitment_ids=commitment_ids,
        )

    def _record_commitments(
        self, context: MeetingContext, plan: PlannedReply, guarded: GuardedReply, result: TurnResult
    ) -> list[str]:
        """Fold this turn's action items into the meeting's ledger.

        Both extractors are consulted. Agreement between them promotes the
        commitment to CONFIRMED; that is the whole reason the deterministic
        rules are kept alongside a model that is better at the job.
        """
        ledger = self._components.commitments.get_or_create(context.meeting_id)
        turn_index = result.memory.turn_index if result.memory is not None else 0
        speaker = (context.speaker or "Someone").strip()
        now = datetime.now(timezone.utc)
        touched: list[str] = []

        if plan.has_action:
            commitment, _ = ledger.record(
                description=plan.action_description,
                owner=plan.action_owner or self._principal_name(context.user_id),
                requested_by=speaker,
                due_iso=plan.action_due_iso,
                due_text=plan.action_due_text,
                turn_index=turn_index,
                source=CommitmentSource.PLANNER,
                acknowledged=plan.turn_type is TurnType.COMMITMENT and guarded.spoken_text is not None,
                now=now,
            )
            if commitment is not None:
                touched.append(commitment.id)

        # The deterministic extractor corroborates by default and only creates a
        # commitment of its own when the planner is unavailable. It reads a whole
        # utterance at once with no sense of the conversation, so left to create
        # freely it manufactures items out of any sentence containing "action
        # item" - including the question "What are the action items today?".
        planner_worked = self._components.planner is not None
        addressee = context.addressed_to or leading_addressee(context.transcript, list(context.participants))
        for action in extract_actions(
            context.transcript, speaker=speaker, addressee=addressee,
            participants=list(context.participants), now=context.timestamp,
        ):
            commitment, _ = ledger.record(
                description=action.description,
                owner=action.owner,
                requested_by=speaker,
                due_iso=action.deadline.due.isoformat() if action.deadline and action.deadline.due else "",
                due_text=action.deadline.text if action.deadline else "",
                turn_index=turn_index,
                source=CommitmentSource.RULES,
                create=not planner_worked,
                now=now,
            )
            if commitment is not None and commitment.id not in touched:
                touched.append(commitment.id)

        return touched

    def _recovery_line(self) -> str | None:
        """Something to say when the model could not be reached at all.

        Going mute is the worst possible response: it is exactly what the broken
        version did, and from the room it is indistinguishable from the delegate
        having crashed. A short "sorry, say that again" buys a retry and keeps
        the conversation alive.

        Rate limited to once per :data:`_RECOVERY_COOLDOWN_SECONDS` so a
        sustained outage does not turn the delegate into a parrot apologising
        after every sentence - after the first, it falls silent and the console
        banner carries the failure instead.
        """
        now = time.monotonic()
        if self._last_recovery_at is not None and now - self._last_recovery_at < _RECOVERY_COOLDOWN_SECONDS:
            return None
        self._last_recovery_at = now
        return "Sorry, I didn't quite catch that — could you say it again?"

    def _principal_name(self, user_id: str) -> str:
        try:
            return self._store.get_profile(user_id).display_name
        except Exception:  # noqa: BLE001 - only used as a label
            return user_id


__all__ = ["FollowUp", "TurnComponents", "TurnProcessor", "TurnResult"]
