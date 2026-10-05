"""
Pydantic contracts for the DELEG8 Policy Router Brain.

Three contracts cross the API boundary:

* :class:`MeetingContext`  - what the brain is asked to evaluate (input).
* :class:`UserProfile`     - the principal's Representation Policy (stored).
* :class:`PolicyDecision`  - the brain's verdict (output).

Everything else in this module is a building block of one of those three.
:class:`Grounding` (Sprint 4) records the evidence behind an AUTO_REPLY.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import (
    BaseModel,
    Field,
    computed_field,
    field_validator,
    model_validator,
)


def _utc_now() -> datetime:
    """Timezone-aware UTC timestamp used for defaults."""
    return datetime.now(timezone.utc)


def _normalise_phrases(phrases: list[str]) -> list[str]:
    """Lower-case, trim, and de-duplicate a phrase list while preserving order."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for phrase in phrases:
        key = " ".join(phrase.strip().lower().split())
        if key and key not in seen:
            seen.add(key)
            cleaned.append(key)
    return cleaned


class PolicyTier(str, Enum):
    """Authorization level that a Representation Policy assigns to a topic.

    GREEN  - the delegate may answer autonomously.
    YELLOW - the delegate must obtain human approval before committing.
    RED    - the delegate must never answer; the human always handles it.
    """

    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"

    @property
    def restrictiveness(self) -> int:
        """Ordinal rank; higher means safer to escalate. Used to resolve overlaps."""
        return _TIER_RANK[self]


_TIER_RANK: dict[PolicyTier, int] = {
    PolicyTier.GREEN: 0,
    PolicyTier.YELLOW: 1,
    PolicyTier.RED: 2,
}


class DecisionState(str, Enum):
    """Terminal states of the Policy Router state machine.

    AUTO_REPLY - the delegate knows the answer and is authorized to speak.
    SILENT     - someone else is answering, or the utterance is not relevant.
    ESCALATE   - a human must approve or handle the response.
    """

    AUTO_REPLY = "AUTO_REPLY"
    SILENT = "SILENT"
    ESCALATE = "ESCALATE"


class PolicyRule(BaseModel):
    """One line of a Representation Policy: a topic, its tier, and how to spot it."""

    topic: str = Field(
        ...,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="Stable snake_case identifier for the topic, e.g. 'project_status'.",
    )
    tier: PolicyTier = Field(..., description="Authorization tier for this topic.")
    description: str = Field(
        ..., min_length=1, description="Human-readable statement of what the tier permits."
    )
    triggers: list[str] = Field(
        ...,
        min_length=1,
        description="Lower-cased phrases whose presence classifies an utterance into this topic.",
    )

    @field_validator("triggers")
    @classmethod
    def _clean_triggers(cls, triggers: list[str]) -> list[str]:
        cleaned = _normalise_phrases(triggers)
        if not cleaned:
            raise ValueError("at least one non-empty trigger phrase is required")
        return cleaned


class KnowledgeEntry(BaseModel):
    """A grounded fact the delegate is allowed to state verbatim for a GREEN topic."""

    topic: str = Field(..., description="Topic this fact answers; must exist in the policy.")
    subject: str = Field(
        ..., pattern=r"^[a-z][a-z0-9_]*$", description="What the fact is about, e.g. 'api_integration'."
    )
    keywords: list[str] = Field(
        ..., min_length=1, description="Phrases that identify the subject inside an utterance."
    )
    answer: str = Field(..., min_length=1, description="The exact grounded response to give.")

    @field_validator("keywords")
    @classmethod
    def _clean_keywords(cls, keywords: list[str]) -> list[str]:
        cleaned = _normalise_phrases(keywords)
        if not cleaned:
            raise ValueError("at least one non-empty keyword is required")
        return cleaned


class UserProfile(BaseModel):
    """The principal being represented, their Representation Policy, and grounded knowledge."""

    user_id: str = Field(
        ..., pattern=r"^[a-z0-9][a-z0-9_\-.]*$", description="Stable identifier of the principal."
    )
    display_name: str = Field(..., min_length=1, description="Full name as it appears in meetings.")
    aliases: list[str] = Field(
        default_factory=list, description="Other names or nicknames the principal is addressed by."
    )
    policy: list[PolicyRule] = Field(..., min_length=1, description="The Representation Policy.")
    knowledge_base: list[KnowledgeEntry] = Field(
        default_factory=list, description="Facts the delegate may state for GREEN topics."
    )

    @model_validator(mode="after")
    def _check_consistency(self) -> UserProfile:
        topics = [rule.topic for rule in self.policy]
        duplicates = sorted({t for t in topics if topics.count(t) > 1})
        if duplicates:
            raise ValueError(f"duplicate policy topics: {duplicates}")
        unknown = sorted({entry.topic for entry in self.knowledge_base} - set(topics))
        if unknown:
            raise ValueError(f"knowledge entries reference topics missing from policy: {unknown}")
        return self

    @property
    def names(self) -> frozenset[str]:
        """Every lower-cased form the principal may be addressed by (full name, first name, aliases)."""
        parts = self.display_name.split()
        first_name = parts[0] if parts else ""
        return frozenset(
            name.strip().lower()
            for name in (self.display_name, first_name, *self.aliases)
            if name.strip()
        )

    def rule_for(self, topic: str) -> PolicyRule | None:
        """Return the policy rule for ``topic``, or ``None`` if the policy is silent on it."""
        return next((rule for rule in self.policy if rule.topic == topic), None)

    def knowledge_for(self, topic: str) -> list[KnowledgeEntry]:
        """Return every knowledge entry that answers ``topic``."""
        return [entry for entry in self.knowledge_base if entry.topic == topic]


class MeetingContext(BaseModel):
    """A single transcript snippet plus the situational facts needed to route it."""

    meeting_id: str = Field(..., min_length=1, description="Identifier of the live meeting.")
    user_id: str = Field(
        ..., min_length=1, description="Principal whose Representation Policy governs this evaluation."
    )
    transcript: str = Field(
        ..., description="The utterance to evaluate. Typically one sentence or one speaker turn."
    )
    speaker: str | None = Field(
        default=None, description="Participant who said the transcript, if diarization knows."
    )
    addressed_to: str | None = Field(
        default=None,
        description="Explicit addressee supplied by an upstream stage. Overrides heuristic detection.",
    )
    responder: str | None = Field(
        default=None,
        description="Participant who has already begun answering this utterance, if any.",
    )
    participants: list[str] = Field(
        default_factory=list, description="Display names of everyone in the meeting."
    )
    participant_count: int | None = Field(
        default=None,
        description=(
            "Total attendees on the call right now, the delegate included, when known "
            "(e.g. read from the Google Meet UI). None when unknown - callers should treat "
            "that as 'assume a one-on-one call' rather than guessing a number."
        ),
    )
    timestamp: datetime = Field(
        default_factory=_utc_now, description="When the utterance was captured (UTC)."
    )

    @field_validator("transcript")
    @classmethod
    def _strip_transcript(cls, transcript: str) -> str:
        return transcript.strip()


class GroundingSource(str, Enum):
    """Where a grounded answer came from."""

    PROFILE = "profile"
    """A curated KnowledgeEntry on the Representation Policy."""
    DOCUMENTS = "documents"
    """A passage retrieved from the ingested document index."""


class Grounding(BaseModel):
    """Evidence behind an AUTO_REPLY answer, for audit and for the human on Telegram."""

    source: GroundingSource = Field(..., description="Which knowledge source produced the answer.")
    subject: str = Field(..., min_length=1, description="Profile subject or document title the answer is about.")
    similarity: float | None = Field(
        default=None, ge=-1.0, le=1.0, description="Cosine similarity of the best passage (documents only)."
    )
    citations: list[str] = Field(default_factory=list, description="Human-readable pointers to the evidence.")
    detail: str | None = Field(default=None, description="Extra diagnostics, e.g. matched keywords.")

    def describe(self) -> str:
        """Short label for traces and logs."""
        where = ", ".join(self.citations) if self.citations else self.subject
        if self.similarity is None:
            return f"{self.source.value}: {where}"
        return f"{self.source.value}: {where} (similarity {self.similarity:.2f})"


class TraceStep(BaseModel):
    """One recorded transition of the state machine, for auditability."""

    stage: str = Field(..., description="State-machine stage that was evaluated.")
    outcome: str = Field(..., description="What the stage observed or decided.")


class PolicyDecision(BaseModel):
    """The brain's verdict for one :class:`MeetingContext`."""

    state: DecisionState = Field(..., description="Terminal routing state.")
    reason: str = Field(..., min_length=1, description="Plain-language justification.")
    topic: str | None = Field(default=None, description="Policy topic that governed the decision.")
    tier: PolicyTier | None = Field(default=None, description="Tier of the governing topic.")
    matched_triggers: list[str] = Field(
        default_factory=list, description="Trigger phrases found in the transcript."
    )
    suggested_response: str | None = Field(
        default=None, description="Grounded answer to speak. Only set for AUTO_REPLY."
    )
    grounding: Grounding | None = Field(
        default=None, description="Evidence behind suggested_response. Only set for AUTO_REPLY."
    )
    trace: list[TraceStep] = Field(
        default_factory=list, description="Ordered state-machine transitions that led here."
    )
    evaluated_at: datetime = Field(default_factory=_utc_now, description="Decision time (UTC).")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def requires_human(self) -> bool:
        """True when a human must act before anything is said."""
        return self.state is DecisionState.ESCALATE
