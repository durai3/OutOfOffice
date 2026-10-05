"""
Contracts for the human-in-the-loop escalation bridge.

An :class:`EscalationRequest` is what the delegate hands to a human; an
:class:`EscalationResult` is what comes back. The outcome vocabulary is
deliberately closed so the response playbook can be exhaustive.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, computed_field

from src.brain.models import MeetingContext, PolicyDecision


def _utc_now() -> datetime:
    """Timezone-aware UTC timestamp used for defaults."""
    return datetime.now(timezone.utc)


class EscalationOutcome(str, Enum):
    """How an escalation was resolved.

    APPROVED          - the human authorised the proposed response.
    REJECTED          - the human said no; the delegate declines firmly.
    DECLINED_POLITELY - the human wants a soft deflection instead of a commitment.
    TIMEOUT           - nobody answered within the configured window.
    UNDELIVERED       - the alert never reached a human (bridge disabled or send failed).
    """

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DECLINED_POLITELY = "DECLINED_POLITELY"
    TIMEOUT = "TIMEOUT"
    UNDELIVERED = "UNDELIVERED"

    @property
    def is_human_decision(self) -> bool:
        """True when a person actively chose this outcome."""
        return self in _HUMAN_DECISIONS


_HUMAN_DECISIONS: frozenset[EscalationOutcome] = frozenset(
    {
        EscalationOutcome.APPROVED,
        EscalationOutcome.REJECTED,
        EscalationOutcome.DECLINED_POLITELY,
    }
)


class EscalationRequest(BaseModel):
    """Everything a human needs to make a call on one meeting turn."""

    request_id: str = Field(..., min_length=1, description="Correlation id carried in button callbacks.")
    context: MeetingContext = Field(..., description="The meeting turn that triggered the escalation.")
    decision: PolicyDecision = Field(..., description="The ESCALATE decision, including why it fired.")
    proposed_action: str = Field(
        ..., min_length=1, description="What the delegate will say if the human approves."
    )
    timeout_seconds: float = Field(..., gt=0, description="How long to wait for a human before falling back.")
    created_at: datetime = Field(default_factory=_utc_now, description="When the request was created (UTC).")


class EscalationResult(BaseModel):
    """The resolution of one :class:`EscalationRequest`."""

    request_id: str = Field(..., description="Matches the request's correlation id.")
    outcome: EscalationOutcome = Field(..., description="How the escalation resolved.")
    delivered: bool = Field(..., description="Whether the alert reached the human's device.")
    responded_by: str | None = Field(default=None, description="Display name of the person who tapped a button.")
    response_seconds: float = Field(..., ge=0, description="Seconds from alert sent to resolution.")
    message_id: int | None = Field(default=None, description="Telegram message id of the alert, if delivered.")
    detail: str | None = Field(default=None, description="Extra diagnostics, e.g. a delivery error.")
    resolved_at: datetime = Field(default_factory=_utc_now, description="When the result was produced (UTC).")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def human_decided(self) -> bool:
        """True when a person, not a timeout or failure, produced the outcome."""
        return self.outcome.is_human_decision


class DeliveryReceipt(BaseModel):
    """Outcome of pushing a one-way message (e.g. a post-meeting briefing) to the human."""

    delivered: bool = Field(..., description="Whether the message reached the channel.")
    channel: str = Field(..., description="Which channel handled it, e.g. 'telegram' or 'disabled'.")
    message_id: int | None = Field(default=None, description="Channel message id, if delivered.")
    detail: str | None = Field(default=None, description="Error or hint when not delivered.")
    sent_at: datetime = Field(default_factory=_utc_now, description="When the attempt completed (UTC).")
