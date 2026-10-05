"""
Response playbook for escalated turns.

Two questions are answered here, and nowhere else:

1. :func:`propose_response` - what the delegate *would* say if the human
   approves. This is what appears on the human's phone.
2. :func:`final_response`   - what the delegate actually says once the
   human (or the timeout) has spoken.

The wording is intentionally template-driven for Sprint 2: the delegate
never invents commitments, figures, or terms; it only relays a decision.
"""

from __future__ import annotations

from src.brain.models import MeetingContext, PolicyDecision, PolicyTier, UserProfile
from src.escalation.models import EscalationOutcome

FALLBACK_RESPONSE: str = (
    "I don't have authorization to commit to that right now, "
    "but I'll follow up after checking."
)
"""Spoken when no human decision arrives (timeout or undelivered alert)."""

REJECTED_RESPONSE: str = (
    "I've checked on that, and unfortunately it's not something we can agree to right now."
)
"""Spoken when the human taps Reject."""

DECLINED_POLITELY_RESPONSE: str = (
    "I'd rather not commit to that in this meeting. "
    "Let me take it back to the team and follow up with you by email."
)
"""Spoken when the human taps Decline Politely."""


def propose_response(context: MeetingContext, decision: PolicyDecision, profile: UserProfile) -> str:
    """Draft what the delegate will say if the human approves this escalation.

    The draft depends on *why* the turn escalated:

    * RED    - relay approval in principle; terms always go in writing.
    * YELLOW - make the commitment and promise written confirmation.
    * GREEN  - the topic is allowed but no grounded fact exists; promise an update.
    * None   - a direct request on an uncovered topic; promise to take it back.
    """
    principal = profile.display_name
    if decision.tier is PolicyTier.RED:
        return (
            f"{principal} has reviewed this and is comfortable moving forward in principle. "
            "We'll put the exact terms in writing rather than settle them here."
        )
    if decision.tier is PolicyTier.YELLOW:
        return (
            "Yes, we can commit to that. I'll send written confirmation of the details "
            "right after this meeting."
        )
    if decision.tier is PolicyTier.GREEN:
        return (
            "I don't have the latest on that in front of me. I'll get an update from "
            f"{principal} and share it right after this meeting."
        )
    return f"I'll take that to {principal} and come back to you with an answer today."


def final_response(outcome: EscalationOutcome, proposed_response: str) -> str:
    """Turn a human outcome into the sentence the delegate speaks in the meeting."""
    if outcome is EscalationOutcome.APPROVED:
        return proposed_response
    if outcome is EscalationOutcome.REJECTED:
        return REJECTED_RESPONSE
    if outcome is EscalationOutcome.DECLINED_POLITELY:
        return DECLINED_POLITELY_RESPONSE
    return FALLBACK_RESPONSE
