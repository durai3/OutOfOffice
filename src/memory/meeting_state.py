"""
Live meeting memory.

:class:`MeetingState` is updated on every utterance the delegate hears (and
every reply it gives) and keeps four things current:

* ``active_topic``     - what the room is talking about right now;
* ``speaker_turns``    - a rolling window of the most recent utterances;
* ``agreed_decisions`` - statements the group agreed to, plus anything the
                         human approved or rejected through an escalation;
* ``pending_actions``  - tasks with owners and (resolved) deadlines.

It also keeps the full transcript and the questions the delegate could not
answer, because the post-meeting briefing needs both. Extraction is
rule-based (:mod:`src.memory.extraction`) so the update costs microseconds.

:class:`MeetingStateRegistry` holds one state per ``meeting_id`` for the
lifetime of the process; the turn processor records into it and the
post-meeting reporter drains it.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from collections.abc import Callable
from datetime import date, datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

from src.brain.models import (
    DecisionState,
    GroundingSource,
    MeetingContext,
    PolicyDecision,
    PolicyTier,
)
from src.escalation.models import EscalationOutcome, EscalationResult
from src.memory.extraction import (
    extract_actions,
    extract_decision,
    infer_topic,
    leading_addressee,
)

logger = logging.getLogger("deleg8.memory")

DEFAULT_WINDOW: int = 12
"""Utterances kept in the rolling ``speaker_turns`` window."""

DELEGATE_LABEL: str = "DELEG8"
"""Speaker label used for the delegate's own replies in the transcript."""


def memory_window_from_env() -> int:
    """Rolling window size from ``DELEG8_MEMORY_WINDOW``, or :data:`DEFAULT_WINDOW`.

    The single source of truth for both entry points that construct a
    :class:`MeetingStateRegistry` (the HTTP API and the Google Meet session),
    so the setting is honoured the same way regardless of which one is running.
    """
    raw = os.getenv("DELEG8_MEMORY_WINDOW", "").strip()
    if not raw:
        return DEFAULT_WINDOW
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("DELEG8_MEMORY_WINDOW=%r is not an integer; using %d", raw, DEFAULT_WINDOW)
        return DEFAULT_WINDOW


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DecisionOrigin(str, Enum):
    """How a decision entered the record."""

    UTTERANCE = "utterance"
    """Someone in the room said it was agreed."""
    HUMAN_APPROVED = "human_approved"
    """The principal tapped Approve on an escalation; the delegate committed."""
    HUMAN_REJECTED = "human_rejected"
    """The principal tapped Reject; the delegate declined on the record."""


class SpeakerTurn(BaseModel):
    """One utterance in the meeting, with what the brain made of it."""

    index: int = Field(..., ge=0, description="Position in the full transcript.")
    speaker: str
    text: str
    timestamp: datetime
    decision: DecisionState | None = Field(default=None, description="Brain verdict; None for the delegate's own replies.")
    topic: str | None = None
    tier: PolicyTier | None = None
    is_delegate: bool = Field(default=False, description="True when the delegate said this.")


class AgreedDecision(BaseModel):
    """Something the meeting agreed to."""

    text: str
    proposed_by: str
    origin: DecisionOrigin
    turn_index: int
    timestamp: datetime


class ActionItem(BaseModel):
    """A task with an owner and, when spoken, a deadline."""

    description: str
    owner: str
    owner_inferred: bool = Field(default=False, description="True when the owner was implied, not named.")
    due_text: str | None = Field(default=None, description="Deadline phrase as spoken.")
    due_date: date | None = Field(default=None, description="Deadline resolved against the utterance time.")
    turn_index: int
    timestamp: datetime

    @property
    def due_label(self) -> str | None:
        """Resolved date when known, else the spoken phrase."""
        if self.due_date is not None:
            return self.due_date.strftime("%a %d %b %Y")
        return self.due_text


class OpenQuestion(BaseModel):
    """A request the delegate could not settle itself."""

    text: str
    asked_by: str
    topic: str | None
    tier: PolicyTier | None
    outcome: str = Field(..., description="Why it is open, e.g. 'escalated: TIMEOUT' or 'no grounded answer'.")
    turn_index: int
    timestamp: datetime


class MeetingStats(BaseModel):
    """Counters for the briefing header."""

    utterances: int = 0
    auto_replies: int = 0
    escalations: int = 0
    silent: int = 0
    human_decisions: int = 0


class TurnUpdate(BaseModel):
    """What changed in the meeting state because of one turn."""

    turn_index: int
    active_topic: str | None
    topic_changed: bool
    new_decisions: list[AgreedDecision] = Field(default_factory=list)
    new_actions: list[ActionItem] = Field(default_factory=list)
    new_questions: list[OpenQuestion] = Field(default_factory=list)

    @property
    def changed(self) -> bool:
        """True when anything beyond the transcript moved."""
        return self.topic_changed or bool(self.new_decisions or self.new_actions or self.new_questions)


class MeetingSnapshot(BaseModel):
    """Serialisable copy of a meeting's memory (API responses, the summariser)."""

    meeting_id: str
    user_id: str
    started_at: datetime
    concluded_at: datetime | None
    participants: list[str]
    active_topic: str | None
    speaker_turns: list[SpeakerTurn] = Field(..., description="Rolling window of recent utterances.")
    transcript: list[SpeakerTurn] = Field(..., description="Every utterance and delegate reply, in order.")
    agreed_decisions: list[AgreedDecision]
    pending_actions: list[ActionItem]
    open_questions: list[OpenQuestion]
    stats: MeetingStats

    @property
    def duration_minutes(self) -> float:
        """Wall time from the first utterance to conclusion (or the last utterance)."""
        end = self.concluded_at or (self.transcript[-1].timestamp if self.transcript else self.started_at)
        return max(0.0, (end - self.started_at).total_seconds() / 60.0)


class MeetingState:
    """Rolling memory for one meeting. Not thread-safe; use from the event loop."""

    def __init__(
        self,
        meeting_id: str,
        user_id: str,
        *,
        window: int = DEFAULT_WINDOW,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Start an empty record. ``clock`` is injectable for tests."""
        if window < 1:
            raise ValueError("window must be at least 1")
        self.meeting_id = meeting_id
        self.user_id = user_id
        self._clock = clock
        self.started_at: datetime = clock()
        self.concluded_at: datetime | None = None
        self.active_topic: str | None = None
        self.speaker_turns: deque[SpeakerTurn] = deque(maxlen=window)
        self.transcript: list[SpeakerTurn] = []
        self.agreed_decisions: list[AgreedDecision] = []
        self.pending_actions: list[ActionItem] = []
        self.open_questions: list[OpenQuestion] = []
        self.participants: list[str] = []
        self.stats = MeetingStats()

    # ------------------------------------------------------------------ #
    # Updates
    # ------------------------------------------------------------------ #

    def record_turn(
        self,
        context: MeetingContext,
        decision: PolicyDecision,
        *,
        response: str | None = None,
        escalation: EscalationResult | None = None,
    ) -> TurnUpdate:
        """Fold one processed turn into the state and report what changed."""
        if self.concluded_at is not None:
            logger.warning("meeting %s already concluded; turn recorded anyway", self.meeting_id)
        for participant in context.participants:
            if participant and participant not in self.participants:
                self.participants.append(participant)
        speaker = (context.speaker or "Unknown").strip() or "Unknown"
        if speaker not in self.participants and speaker != "Unknown":
            self.participants.append(speaker)

        turn = SpeakerTurn(
            index=len(self.transcript),
            speaker=speaker,
            text=context.transcript,
            timestamp=context.timestamp,
            decision=decision.state,
            topic=decision.topic,
            tier=decision.tier,
        )
        self.transcript.append(turn)
        self.speaker_turns.append(turn)
        self._count(decision, escalation)

        update = TurnUpdate(turn_index=turn.index, active_topic=self.active_topic, topic_changed=False)
        if context.transcript:
            self._extract(turn, context, decision, update)
            self._record_escalation(turn, context, decision, escalation, update)

        if response:
            reply = SpeakerTurn(
                index=len(self.transcript), speaker=DELEGATE_LABEL, text=response,
                timestamp=self._clock(), decision=None, topic=decision.topic, tier=decision.tier, is_delegate=True,
            )
            self.transcript.append(reply)
            self.speaker_turns.append(reply)

        previous = self.active_topic
        grounding = decision.grounding
        document_subject = grounding.subject if grounding and grounding.source is GroundingSource.DOCUMENTS else None
        self.active_topic = infer_topic(
            [t.text for t in self.speaker_turns if not t.is_delegate],
            policy_topic=decision.topic,
            grounding_subject=document_subject,
            exclude_names=set(self.participants) | {DELEGATE_LABEL},
        ) or previous
        update.active_topic = self.active_topic
        update.topic_changed = self.active_topic != previous
        if update.changed:
            logger.info(
                "meeting %s turn %d: topic=%s decisions+%d actions+%d questions+%d",
                self.meeting_id, turn.index, self.active_topic,
                len(update.new_decisions), len(update.new_actions), len(update.new_questions),
            )
        return update

    def _count(self, decision: PolicyDecision, escalation: EscalationResult | None) -> None:
        self.stats.utterances += 1
        if decision.state is DecisionState.AUTO_REPLY:
            self.stats.auto_replies += 1
        elif decision.state is DecisionState.ESCALATE:
            self.stats.escalations += 1
        else:
            self.stats.silent += 1
        if escalation is not None and escalation.human_decided:
            self.stats.human_decisions += 1

    def _extract(self, turn: SpeakerTurn, context: MeetingContext, decision: PolicyDecision, update: TurnUpdate) -> None:
        addressee = context.addressed_to or leading_addressee(context.transcript, self.participants)
        statement = extract_decision(context.transcript)
        if statement:
            agreed = AgreedDecision(
                text=statement, proposed_by=turn.speaker, origin=DecisionOrigin.UTTERANCE,
                turn_index=turn.index, timestamp=turn.timestamp,
            )
            self.agreed_decisions.append(agreed)
            update.new_decisions.append(agreed)
        for action in extract_actions(
            context.transcript, speaker=turn.speaker, addressee=addressee,
            participants=self.participants, now=turn.timestamp,
        ):
            item = ActionItem(
                description=action.description, owner=action.owner, owner_inferred=action.owner_inferred,
                due_text=action.deadline.text if action.deadline else None,
                due_date=action.deadline.due if action.deadline else None,
                turn_index=turn.index, timestamp=turn.timestamp,
            )
            self.pending_actions.append(item)
            update.new_actions.append(item)

    def _record_escalation(
        self,
        turn: SpeakerTurn,
        context: MeetingContext,
        decision: PolicyDecision,
        escalation: EscalationResult | None,
        update: TurnUpdate,
    ) -> None:
        if decision.state is not DecisionState.ESCALATE:
            return
        outcome = escalation.outcome if escalation is not None else None
        if outcome is EscalationOutcome.APPROVED:
            agreed = AgreedDecision(
                text=f"Approved: {context.transcript}", proposed_by=turn.speaker,
                origin=DecisionOrigin.HUMAN_APPROVED, turn_index=turn.index, timestamp=turn.timestamp,
            )
            self.agreed_decisions.append(agreed)
            update.new_decisions.append(agreed)
            return
        if outcome is EscalationOutcome.REJECTED:
            agreed = AgreedDecision(
                text=f"Declined: {context.transcript}", proposed_by=turn.speaker,
                origin=DecisionOrigin.HUMAN_REJECTED, turn_index=turn.index, timestamp=turn.timestamp,
            )
            self.agreed_decisions.append(agreed)
            update.new_decisions.append(agreed)
            return
        label = f"escalated: {outcome.value}" if outcome is not None else "escalated: pending"
        if escalation is not None and escalation.responded_by and outcome is EscalationOutcome.DECLINED_POLITELY:
            label += f" by {escalation.responded_by} (follow-up promised)"
        question = OpenQuestion(
            text=context.transcript, asked_by=turn.speaker, topic=decision.topic, tier=decision.tier,
            outcome=label, turn_index=turn.index, timestamp=turn.timestamp,
        )
        self.open_questions.append(question)
        update.new_questions.append(question)

    def record_escalation_outcome(
        self, context: MeetingContext, decision: PolicyDecision, escalation: EscalationResult
    ) -> None:
        """Record a human's verdict that arrived after the turn had already finished.

        Escalation used to block the turn, so the outcome was always known by the
        time :meth:`record_turn` ran. It is now dispatched in the background so a
        live meeting is never held in silence waiting for a phone, which means
        the verdict lands here instead - often several turns later.
        """
        speaker = (context.speaker or "Unknown").strip() or "Unknown"
        turn_index = next(
            (t.index for t in reversed(self.transcript) if not t.is_delegate and t.text == context.transcript),
            max(len(self.transcript) - 1, 0),
        )
        if escalation.human_decided:
            self.stats.human_decisions += 1

        # record_turn already logged this as "escalated: pending"; resolve that
        # entry rather than leaving a stale open question beside the answer.
        pending = next(
            (q for q in self.open_questions if q.turn_index == turn_index and q.outcome.endswith("pending")),
            None,
        )
        if escalation.outcome in {EscalationOutcome.APPROVED, EscalationOutcome.REJECTED}:
            approved = escalation.outcome is EscalationOutcome.APPROVED
            self.agreed_decisions.append(
                AgreedDecision(
                    text=f"{'Approved' if approved else 'Declined'}: {context.transcript}",
                    proposed_by=speaker,
                    origin=DecisionOrigin.HUMAN_APPROVED if approved else DecisionOrigin.HUMAN_REJECTED,
                    turn_index=turn_index,
                    timestamp=context.timestamp,
                )
            )
            if pending is not None:
                self.open_questions.remove(pending)
        elif pending is not None:
            label = f"escalated: {escalation.outcome.value}"
            if escalation.responded_by and escalation.outcome is EscalationOutcome.DECLINED_POLITELY:
                label += f" by {escalation.responded_by} (follow-up promised)"
            self.open_questions[self.open_questions.index(pending)] = pending.model_copy(update={"outcome": label})
        logger.info(
            "meeting %s: escalation for turn %d resolved as %s",
            self.meeting_id, turn_index, escalation.outcome.value,
        )

    # ------------------------------------------------------------------ #
    # Read side
    # ------------------------------------------------------------------ #

    def snapshot(self) -> MeetingSnapshot:
        """Copy the state into a serialisable model."""
        return MeetingSnapshot(
            meeting_id=self.meeting_id,
            user_id=self.user_id,
            started_at=self.started_at,
            concluded_at=self.concluded_at,
            participants=list(self.participants),
            active_topic=self.active_topic,
            speaker_turns=list(self.speaker_turns),
            transcript=list(self.transcript),
            agreed_decisions=list(self.agreed_decisions),
            pending_actions=list(self.pending_actions),
            open_questions=list(self.open_questions),
            stats=self.stats.model_copy(),
        )

    def conclude(self) -> MeetingSnapshot:
        """Mark the meeting as over (idempotent) and return the final snapshot."""
        if self.concluded_at is None:
            self.concluded_at = self._clock()
            logger.info(
                "meeting %s concluded: %d utterances, %d decisions, %d actions, %d open questions",
                self.meeting_id, self.stats.utterances, len(self.agreed_decisions),
                len(self.pending_actions), len(self.open_questions),
            )
        return self.snapshot()

    @property
    def is_empty(self) -> bool:
        """True when nothing has been recorded yet."""
        return not self.transcript


class MeetingStateRegistry:
    """One :class:`MeetingState` per live meeting, keyed by ``meeting_id``."""

    def __init__(self, *, window: int = DEFAULT_WINDOW) -> None:
        """``window`` is applied to every meeting created here."""
        self._window = window
        self._meetings: dict[str, MeetingState] = {}

    def get_or_create(self, meeting_id: str, user_id: str) -> MeetingState:
        """Return the live state for ``meeting_id``, creating it on first sight."""
        state = self._meetings.get(meeting_id)
        if state is None:
            state = MeetingState(meeting_id, user_id, window=self._window)
            self._meetings[meeting_id] = state
            logger.info("meeting %s started (principal %s)", meeting_id, user_id)
        return state

    def get(self, meeting_id: str) -> MeetingState | None:
        """Return the live state or ``None``."""
        return self._meetings.get(meeting_id)

    def remove(self, meeting_id: str) -> MeetingState | None:
        """Forget a meeting (after it has been concluded and reported)."""
        return self._meetings.pop(meeting_id, None)

    def active(self) -> list[MeetingState]:
        """Every meeting currently held, oldest first."""
        return list(self._meetings.values())

    def __len__(self) -> int:
        return len(self._meetings)
