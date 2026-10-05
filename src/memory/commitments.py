"""
The commitments ledger: what the room asked the principal to do.

The failure this replaces is worth stating precisely, because it was three
failures stacked on top of each other. Someone spent four utterances asking for
a WhatsApp call at 8pm the next day. The delegate stayed silent for all four,
so :meth:`~src.turn_processor.TurnProcessor.complete_turn` was never reached and
none of the four was recorded in meeting memory at all. Even if they had been,
the rule-based extractor works one utterance at a time, so "the action item is
that they must give a call to me" and "Tomorrow, 8PM" would have stayed two
unrelated fragments. And nothing in the pipeline ever spoke an acknowledgement,
so the person had no idea whether they had been heard.

This module fixes the third failure and the second. A commitment is a durable
record that *accumulates*: a later utterance that adds a time, changes an owner
or restates the task merges into the commitment already open rather than
creating a second one. :meth:`CommitmentLedger.record` does that merge, which is
what makes a request spread across a conversation come out as one item with a
resolved deadline.

Two extractors feed it, and the disagreement between them is signal:

* the planner (:mod:`src.brain.planner`), which sees the whole recent
  conversation and resolves "tomorrow 8PM" against the meeting clock;
* the deterministic rules (:mod:`src.memory.extraction`), which cost
  microseconds and keep working when the model is unavailable.

When both fire on the same turn the commitment is marked ``CONFIRMED``; when
only one does it is ``PROBABLE``, and the briefing says so rather than
presenting a guess as fact.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from itertools import count

from pydantic import BaseModel, Field

logger = logging.getLogger("deleg8.memory.commitments")

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'\-]*")
_FILLER: frozenset[str] = frozenset(
    """a an and the to for of on at in with by is are be will would should must that this it them
    they he she i we you my your his her their our me us please make sure give get go do""".split()
)
_MERGE_THRESHOLD: float = 0.45
"""Token overlap above which two descriptions are treated as the same commitment.

Tuned deliberately low. The cost of splitting one request into two entries (the
principal sees a duplicate and is briefly confused) is far lower than the cost
of merging two genuinely different requests into one (a real commitment silently
disappears), so the threshold sits where restatements merge and distinct asks do
not."""


class CommitmentStatus(str, Enum):
    """Where a commitment stands."""

    PROBABLE = "probable"
    """Detected by one extractor only; shown to the principal flagged as unconfirmed."""
    CONFIRMED = "confirmed"
    """Detected by both the planner and the deterministic rules, or explicitly acknowledged aloud."""
    DONE = "done"
    """Marked complete by the principal from Telegram."""
    DROPPED = "dropped"
    """Dismissed by the principal."""


class CommitmentSource(str, Enum):
    """Which extractor produced or last updated a commitment."""

    PLANNER = "planner"
    RULES = "rules"
    BOTH = "both"


class Commitment(BaseModel):
    """One thing someone asked the principal to do."""

    id: str = Field(..., description="Stable id within the meeting, e.g. 'c1'.")
    description: str = Field(..., description="The task as an imperative phrase.")
    owner: str = Field(..., description="Who owes it; usually the principal.")
    requested_by: str = Field(default="", description="Who asked for it.")
    due_iso: str = Field(default="", description="ISO-8601 date or datetime when one was stated.")
    due_text: str = Field(default="", description="Deadline exactly as spoken.")
    status: CommitmentStatus = Field(default=CommitmentStatus.PROBABLE)
    source: CommitmentSource = Field(default=CommitmentSource.PLANNER)
    acknowledged: bool = Field(default=False, description="True when the delegate said aloud that it would pass this on.")
    first_turn: int = Field(..., ge=0, description="Transcript index where it was first heard.")
    last_turn: int = Field(..., ge=0, description="Transcript index where it was last updated.")
    created_at: datetime
    updated_at: datetime
    notified_at: datetime | None = Field(default=None, description="When it was pushed to the principal, if it was.")

    @property
    def due_label(self) -> str:
        """Human-readable deadline: the resolved date when known, else the phrase."""
        if self.due_iso:
            parsed = parse_due(self.due_iso)
            if parsed is not None:
                fmt = "%a %d %b %Y at %H:%M" if isinstance(parsed, datetime) else "%a %d %b %Y"
                return parsed.strftime(fmt)
        return self.due_text

    @property
    def is_open(self) -> bool:
        """True when it still needs the principal."""
        return self.status in {CommitmentStatus.PROBABLE, CommitmentStatus.CONFIRMED}

    def one_line(self) -> str:
        """Single line for a briefing or a Telegram message."""
        parts = [self.description]
        if self.due_label:
            parts.append(f"due {self.due_label}")
        if self.requested_by:
            parts.append(f"asked by {self.requested_by}")
        line = " — ".join(parts)
        return line if self.status is CommitmentStatus.CONFIRMED else f"{line} (unconfirmed)"


def parse_due(value: str) -> datetime | date | None:
    """Parse an ISO-8601 date or datetime, returning ``None`` when it is not one.

    Models are reliable about the format but not universally so, and a malformed
    deadline must degrade to the spoken phrase rather than raise inside a live
    meeting turn.
    """
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _FILLER and len(w) > 1}


def similarity(left: str, right: str) -> float:
    """Token overlap between two task descriptions, 0.0 to 1.0."""
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _due_moment(commitment: Commitment) -> datetime | None:
    """The commitment's deadline as an aware UTC datetime, or ``None``."""
    parsed = parse_due(commitment.due_iso)
    if parsed is None:
        return None
    if isinstance(parsed, datetime):
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime(parsed.year, parsed.month, parsed.day, 23, 59, tzinfo=timezone.utc)


def due_within(commitment: Commitment, *, hours: float, now: datetime | None = None) -> bool:
    """True when the commitment falls due within ``hours`` from ``now``."""
    when = _due_moment(commitment)
    if when is None:
        return False
    moment = datetime.now(timezone.utc) if now is None else now
    delta = (when - moment).total_seconds() / 3600.0
    return -1.0 <= delta <= hours


def due_soon(commitment: Commitment, *, days: int = 1, now: datetime | None = None) -> bool:
    """True when the deadline falls on or before the end of ``now + days``.

    Calendar days rather than an hours window, because that is how people
    actually mean it. A meeting at 19:35 that produces "call me tomorrow at 8pm"
    is 24.4 hours away - an "is it due within 24 hours?" test says no and the
    alert never fires, which is precisely the kind of near-miss that loses a
    commitment the principal needed to see tonight.
    """
    when = _due_moment(commitment)
    if when is None:
        return False
    moment = datetime.now(timezone.utc) if now is None else now
    horizon = (moment + timedelta(days=days)).replace(hour=23, minute=59, second=59, microsecond=0)
    return when <= horizon


class CommitmentLedger:
    """Every commitment made in one meeting, merged as the conversation develops."""

    def __init__(self, meeting_id: str, sink: "CommitmentSink | None" = None) -> None:
        """Start an empty ledger for ``meeting_id``. ``sink`` mirrors every change."""
        self.meeting_id = meeting_id
        self._items: list[Commitment] = []
        self._ids = count(1)
        self._sink = sink

    def _mirror(self, commitment: Commitment) -> None:
        """Hand one changed commitment to the sink. Never raises."""
        if self._sink is None:
            return
        try:
            self._sink(self.meeting_id, commitment)
        except Exception:  # noqa: BLE001 - persistence must never break a live meeting
            logger.exception("commitment sink failed for %s", commitment.id)

    def __len__(self) -> int:
        return len(self._items)

    @property
    def items(self) -> list[Commitment]:
        """Every commitment, in the order first heard."""
        return list(self._items)

    def open_items(self) -> list[Commitment]:
        """Commitments still needing the principal."""
        return [c for c in self._items if c.is_open]

    def get(self, commitment_id: str) -> Commitment | None:
        """Look one up by id."""
        return next((c for c in self._items if c.id == commitment_id), None)

    def record(
        self,
        *,
        description: str,
        owner: str,
        requested_by: str = "",
        due_iso: str = "",
        due_text: str = "",
        turn_index: int,
        source: CommitmentSource,
        acknowledged: bool = False,
        create: bool = True,
        now: datetime | None = None,
    ) -> tuple[Commitment | None, bool]:
        """Add or merge a commitment. Returns ``(commitment, is_new)``.

        Merging is what lets an ask spread over several utterances end up as one
        item: a restatement that only adds a deadline updates the open
        commitment instead of creating a near-duplicate beside it.

        ``create=False`` means "corroborate only": merge into an existing
        commitment if this matches one, otherwise record nothing and return
        ``(None, False)``. That is how the deterministic extractor is used
        whenever the planner is working. Run as a creator it produces junk - on
        the very transcript this was built for it turned "What are the action
        items today?" into an action item called "S today", and matched the
        phrase "action item" mid-sentence to produce a second entry that was
        half a sentence long. As a corroborator it is valuable: agreement with
        the planner is what promotes a commitment to CONFIRMED.
        """
        description = " ".join(description.split())
        if not description:
            raise ValueError("a commitment needs a description")
        moment = now or datetime.now(timezone.utc)

        existing = self._find_match(description, owner)
        if existing is not None:
            self._merge(existing, description, requested_by, due_iso, due_text, turn_index, source, acknowledged, moment)
            logger.info("commitment %s updated from turn %d: %s", existing.id, turn_index, existing.one_line())
            self._mirror(existing)
            return existing, False
        if not create:
            return None, False

        commitment = Commitment(
            id=f"c{next(self._ids)}",
            description=description,
            owner=owner or "Unassigned",
            requested_by=requested_by,
            due_iso=due_iso,
            due_text=due_text,
            status=CommitmentStatus.PROBABLE,
            source=source,
            acknowledged=acknowledged,
            first_turn=turn_index,
            last_turn=turn_index,
            created_at=moment,
            updated_at=moment,
        )
        self._items.append(commitment)
        logger.info("commitment %s recorded at turn %d: %s", commitment.id, turn_index, commitment.one_line())
        self._mirror(commitment)
        return commitment, True

    def _find_match(self, description: str, owner: str) -> Commitment | None:
        """The open commitment this description is a restatement of, if any."""
        best: Commitment | None = None
        best_score = _MERGE_THRESHOLD
        for candidate in self._items:
            if not candidate.is_open:
                continue
            # Different owners are different commitments even for the same task,
            # but "Durai" and "Alagarsamy Duraikumaran" are the same person.
            different_owner = owner and candidate.owner and owner.lower() != candidate.owner.lower()
            if different_owner and similarity(owner, candidate.owner) < 0.5:
                continue
            score = similarity(description, candidate.description)
            if score > best_score:
                best, best_score = candidate, score
        return best

    @staticmethod
    def _merge(
        target: Commitment,
        description: str,
        requested_by: str,
        due_iso: str,
        due_text: str,
        turn_index: int,
        source: CommitmentSource,
        acknowledged: bool,
        moment: datetime,
    ) -> None:
        """Fold a restatement into an existing commitment, keeping the richer value of each field."""
        if len(description) > len(target.description):
            target.description = description
        if due_iso and not target.due_iso:
            target.due_iso = due_iso
        if due_text and not target.due_text:
            target.due_text = due_text
        if requested_by and not target.requested_by:
            target.requested_by = requested_by
        if acknowledged:
            target.acknowledged = True
        if source is not target.source:
            # Independent detection by both extractors is the strongest signal available.
            target.source = CommitmentSource.BOTH
            target.status = CommitmentStatus.CONFIRMED
        elif target.acknowledged and target.due_iso:
            target.status = CommitmentStatus.CONFIRMED
        target.last_turn = turn_index
        target.updated_at = moment

    def mark(self, commitment_id: str, status: CommitmentStatus) -> Commitment | None:
        """Set a commitment's status (used by the Telegram buttons)."""
        commitment = self.get(commitment_id)
        if commitment is None:
            return None
        commitment.status = status
        commitment.updated_at = datetime.now(timezone.utc)
        logger.info("commitment %s marked %s", commitment_id, status.value)
        self._mirror(commitment)
        return commitment

    def mark_notified(self, commitment_id: str) -> None:
        """Record that the principal has been pushed this commitment."""
        commitment = self.get(commitment_id)
        if commitment is not None:
            commitment.notified_at = datetime.now(timezone.utc)
            self._mirror(commitment)

    def urgent_unnotified(self, *, days: int = 1, now: datetime | None = None) -> list[Commitment]:
        """Open commitments due today or tomorrow that the principal has not been told about yet."""
        return [
            c for c in self._items
            if c.is_open and c.notified_at is None and due_soon(c, days=days, now=now)
        ]


CommitmentSink = Callable[[str, Commitment], None]
"""Receives ``(meeting_id, commitment)`` after every create or merge.

The ledger stays in memory - merging a restatement into an open commitment is
a hot-path operation with no business being a SQL round trip - and this is how
each change is mirrored to the database, so an action item outlives the process
that heard it and shows up on another machine's console mid-meeting."""


class CommitmentRegistry:
    """One :class:`CommitmentLedger` per meeting, keyed by ``meeting_id``."""

    def __init__(self, sink: CommitmentSink | None = None) -> None:
        """Start empty. ``sink`` mirrors every change to durable storage."""
        self._ledgers: dict[str, CommitmentLedger] = {}
        self._sink = sink

    def get_or_create(self, meeting_id: str) -> CommitmentLedger:
        """The ledger for ``meeting_id``, created on first sight."""
        ledger = self._ledgers.get(meeting_id)
        if ledger is None:
            ledger = CommitmentLedger(meeting_id, sink=self._sink)
            self._ledgers[meeting_id] = ledger
        return ledger

    def get(self, meeting_id: str) -> CommitmentLedger | None:
        """The ledger for ``meeting_id``, or ``None``."""
        return self._ledgers.get(meeting_id)

    def remove(self, meeting_id: str) -> CommitmentLedger | None:
        """Forget a meeting's ledger once it has been reported."""
        return self._ledgers.pop(meeting_id, None)
