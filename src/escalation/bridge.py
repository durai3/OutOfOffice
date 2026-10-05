"""
The escalation bridge seam.

:class:`EscalationBridge` is the only thing the orchestration layer knows
about. :class:`TelegramEscalationBridge` implements it for real;
:class:`NullEscalationBridge` stands in when Telegram is not configured so
the API still boots and AUTO_REPLY / SILENT turns keep working.

Sprint 4 adds :class:`MessageDispatcher`, the one-way half of the channel:
the post-meeting reporter pushes the executive briefing through it. Both
bridges implement both protocols.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable
from uuid import uuid4

from src.brain.models import MeetingContext, PolicyDecision
from src.escalation.models import DeliveryReceipt, EscalationOutcome, EscalationResult

logger = logging.getLogger("deleg8.escalation")


@runtime_checkable
class EscalationBridge(Protocol):
    """A channel that can put an ESCALATE decision in front of a human and wait."""

    async def start(self) -> None:
        """Connect to the channel. Called once from the application lifespan."""
        ...

    async def stop(self) -> None:
        """Disconnect and resolve anything still pending. Idempotent."""
        ...

    def describe(self) -> str:
        """One-line, human-readable status for health checks and logs."""
        ...

    async def send_escalation_request(
        self, context: MeetingContext, proposed_action: str, decision: PolicyDecision
    ) -> EscalationResult:
        """Alert the human, wait for their choice (bounded by a timeout), and report it."""
        ...


@runtime_checkable
class MessageDispatcher(Protocol):
    """A channel that can push a one-way message to the human (no reply expected)."""

    def describe(self) -> str:
        """One-line, human-readable status."""
        ...

    async def send_text(
        self, text: str, *, markdown_v2: bool = False, plain_fallback: str | None = None
    ) -> DeliveryReceipt:
        """Deliver ``text``. With ``markdown_v2`` the text is pre-escaped MarkdownV2;
        ``plain_fallback`` is sent instead if the channel rejects the formatting."""
        ...


class NullEscalationBridge:
    """Bridge used when no channel is configured: every escalation is UNDELIVERED at once."""

    def __init__(self, reason: str = "no escalation channel configured") -> None:
        """Remember why the real bridge is absent so it can be surfaced in results."""
        self._reason = reason

    async def start(self) -> None:
        """Nothing to connect."""
        logger.warning("Escalation bridge disabled: %s. ESCALATE turns will use the fallback reply.", self._reason)

    async def stop(self) -> None:
        """Nothing to disconnect."""

    def describe(self) -> str:
        """Report the disabled state."""
        return f"disabled ({self._reason})"

    async def send_escalation_request(
        self, context: MeetingContext, proposed_action: str, decision: PolicyDecision
    ) -> EscalationResult:
        """Resolve immediately as UNDELIVERED so the caller falls back safely."""
        request_id = uuid4().hex[:16]
        logger.warning(
            "escalation %s not delivered (meeting=%s, topic=%s): %s",
            request_id, context.meeting_id, decision.topic, self._reason,
        )
        return EscalationResult(
            request_id=request_id,
            outcome=EscalationOutcome.UNDELIVERED,
            delivered=False,
            responded_by=None,
            response_seconds=0.0,
            message_id=None,
            detail=self._reason,
        )

    async def send_text(
        self, text: str, *, markdown_v2: bool = False, plain_fallback: str | None = None
    ) -> DeliveryReceipt:
        """Log the first line and report the message as undelivered."""
        preview = (plain_fallback or text).strip().splitlines()[0] if (plain_fallback or text).strip() else ""
        logger.warning("message not delivered (%s): %s", self._reason, preview[:120])
        return DeliveryReceipt(delivered=False, channel="disabled", message_id=None, detail=self._reason)
