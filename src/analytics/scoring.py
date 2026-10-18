"""
Turns one archived meeting's payload into health scores.

Deliberately decoupled from FastAPI and the database: the only input is the
``payload`` already stored on a :class:`~src.db.models.MeetingReport` row -
the full :class:`~src.memory.post_meeting.PostMeetingReport`, archived at the
moment the meeting concluded - so these are plain functions, testable without
a running server or a live meeting.

Four scores, each 0-1, averaged into one health score:

* ``decision_clarity`` - agreed decisions versus questions left open.
* ``participation_score`` - penalises one speaker dominating the room past
  60% of the human turns.
* ``agenda_adherence`` - how much of the conversation stayed on the meeting's
  dominant topic.
* ``action_item_clarity`` - how many action items came out with a real owner.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

MONOPOLY_THRESHOLD = 0.6
"""Share of human turns above which one speaker is flagged as dominating."""


@dataclass(frozen=True)
class HealthScores:
    """One meeting's four component scores, their average, and any flags."""

    decision_clarity: float
    participation_score: float
    agenda_adherence: float
    action_item_clarity: float
    health_score: float
    flags: list[str] = field(default_factory=list)


def _unclear_owner(owner: str) -> bool:
    cleaned = (owner or "").strip().lower()
    return not cleaned or cleaned == "unassigned" or "inferred" in cleaned


def score_meeting(payload: dict[str, Any]) -> HealthScores:
    """Score one archived :class:`~src.memory.post_meeting.PostMeetingReport` payload."""
    snapshot = payload.get("snapshot") or {}
    transcript = snapshot.get("transcript") or []
    agreed_decisions = snapshot.get("agreed_decisions") or []
    open_questions = snapshot.get("open_questions") or []
    pending_actions = snapshot.get("pending_actions") or []
    action_items = (payload.get("briefing") or {}).get("action_items") or pending_actions

    flags: list[str] = []

    decided, open_count = len(agreed_decisions), len(open_questions)
    decision_clarity = decided / (decided + open_count) if (decided + open_count) else 1.0
    if open_count and decided == 0:
        flags.append("No decisions reached; questions were left open")

    human_turns = [turn for turn in transcript if not turn.get("is_delegate")]
    speaker_counts = Counter(turn.get("speaker") or "Unknown" for turn in human_turns)
    participation_score = 1.0
    if speaker_counts:
        total = sum(speaker_counts.values())
        top_speaker, top_count = speaker_counts.most_common(1)[0]
        share = top_count / total if total else 0.0
        participation_score = max(0.0, 1.0 - max(0.0, share - MONOPOLY_THRESHOLD) / (1 - MONOPOLY_THRESHOLD))
        if share > MONOPOLY_THRESHOLD:
            flags.append(f"{top_speaker} accounted for {share:.0%} of the conversation")

    topics = [turn.get("topic") for turn in human_turns if turn.get("topic")]
    agenda_adherence = 1.0
    if topics:
        _main_topic, main_count = Counter(topics).most_common(1)[0]
        agenda_adherence = main_count / len(topics)
        if agenda_adherence < 0.4:
            flags.append("Conversation drifted across many topics with no clear focus")

    action_item_clarity = 1.0
    if action_items:
        clear = sum(1 for item in action_items if not _unclear_owner(item.get("owner", "")))
        action_item_clarity = clear / len(action_items)
        if action_item_clarity < 0.5:
            flags.append("No clear owner for most action items")

    health_score = (decision_clarity + participation_score + agenda_adherence + action_item_clarity) / 4
    return HealthScores(
        decision_clarity=round(decision_clarity, 3),
        participation_score=round(participation_score, 3),
        agenda_adherence=round(agenda_adherence, 3),
        action_item_clarity=round(action_item_clarity, 3),
        health_score=round(health_score, 3),
        flags=flags,
    )
