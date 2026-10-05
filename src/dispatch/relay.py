"""
How a worker container escalates to its principal, and files its briefing.

A container has no Telegram token and no database, which is the whole point -
but a delegate that cannot reach its principal is a delegate with no YELLOW
tier, and a briefing that is composed and then discarded is worse than no
briefing at all. Both have to go home.

:class:`RelayEscalationBridge` satisfies the same
:class:`~src.escalation.bridge.EscalationBridge` and
:class:`~src.escalation.bridge.MessageDispatcher` protocols the in-process
bridges do. It just does the work somewhere else: the control plane holds the
tenant's chat, so the worker POSTs the escalation there, the control plane
puts it in front of the right human on the right bridge, and the verdict comes
back on the same request.

Two properties this must have, and does:

* **It never hangs a meeting.** The request timeout is the escalation timeout
  plus a small margin, and any failure returns ``UNDELIVERED`` rather than
  raising - exactly what the null bridge does - so the delegate falls through
  to its holding reply instead of standing silent in a call.
* **It cannot address another tenant.** The job token identifies the meeting,
  and the control plane derives the user and the chat from that. There is no
  field on the wire for "whose phone".
"""

from __future__ import annotations

import logging
from typing import Any

from src.brain.models import MeetingContext, PolicyDecision
from src.escalation.models import DeliveryReceipt, EscalationOutcome, EscalationResult

logger = logging.getLogger("deleg8.dispatch.relay")

_MARGIN_SECONDS: float = 15.0
"""Added to the escalation timeout so the control plane's own wait finishes first."""


class RelayEscalationBridge:
    """An escalation channel that runs on the control plane, driven from a worker."""

    def __init__(self, url: str, token: str, *, timeout_seconds: float = 30.0) -> None:
        """Bind to the callback endpoint and this meeting's job token."""
        self._url = url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._available: bool | None = None
        self._pending = 0

    async def start(self) -> None:
        """Nothing to connect: the channel lives on the control plane."""

    async def stop(self) -> None:
        """Nothing to disconnect."""

    def describe(self) -> str:
        """Status line for the session banner."""
        if self._available is False:
            return "relay: the control plane has no channel linked for this account"
        return f"relay: escalations go through the control plane (timeout {self._timeout:g}s)"

    @property
    def pending_count(self) -> int:
        """Escalations currently waiting on a human."""
        return self._pending

    def note_availability(self, available: bool) -> None:
        """Record what the bundle said about the tenant's channel, for :meth:`describe`."""
        self._available = available

    async def _post(self, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any] | None:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self._url}{path}",
                    headers={"Authorization": f"Bearer {self._token}"},
                    json=payload,
                )
        except Exception as exc:  # noqa: BLE001 - a dead control plane must not hang the meeting
            logger.error("relay call to %s failed: %s", path, exc)
            return None
        if response.status_code >= 400:
            logger.error("relay call to %s rejected: HTTP %s %s", path, response.status_code, response.text[:200])
            return None
        return response.json()

    async def send_escalation_request(
        self, context: MeetingContext, proposed_action: str, decision: PolicyDecision
    ) -> EscalationResult:
        """Ask the control plane to put this in front of the principal and wait."""
        self._pending += 1
        try:
            payload = await self._post(
                "/api/v1/worker/escalate",
                {
                    "context": context.model_dump(mode="json"),
                    "decision": decision.model_dump(mode="json"),
                    "proposed_action": proposed_action,
                },
                timeout=self._timeout + _MARGIN_SECONDS,
            )
        finally:
            self._pending -= 1
        if payload is None:
            return EscalationResult(
                request_id="relay-failed",
                outcome=EscalationOutcome.UNDELIVERED,
                delivered=False,
                responded_by=None,
                response_seconds=0.0,
                message_id=None,
                detail="could not reach the control plane to escalate",
            )
        try:
            return EscalationResult.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - a malformed verdict is an undelivered one
            logger.error("relay returned an unusable escalation result: %s", exc)
            return EscalationResult(
                request_id="relay-malformed",
                outcome=EscalationOutcome.UNDELIVERED,
                delivered=False,
                responded_by=None,
                response_seconds=0.0,
                message_id=None,
                detail=str(exc),
            )

    async def send_text(
        self, text: str, *, markdown_v2: bool = False, plain_fallback: str | None = None
    ) -> DeliveryReceipt:
        """Push a one-way message (the briefing) through the control plane."""
        payload = await self._post(
            "/api/v1/worker/message",
            {"text": text, "markdown_v2": markdown_v2, "plain_fallback": plain_fallback},
            timeout=30.0,
        )
        if payload is None:
            return DeliveryReceipt(
                delivered=False, channel="relay", detail="could not reach the control plane"
            )
        try:
            return DeliveryReceipt.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            return DeliveryReceipt(delivered=False, channel="relay", detail=str(exc))


class RelayReportArchive:
    """A :class:`~src.memory.post_meeting.ReportArchive` that files the brief with the control plane.

    Synchronous, because the reporter's ``archive.save`` is - so the POST is
    run on a worker thread from here. A failure is logged and swallowed: the
    briefing has already been delivered to the principal by the time this runs,
    and losing the archive copy must not turn a successful meeting into a crash
    during teardown.
    """

    def __init__(self, url: str, token: str, *, timeout: float = 30.0) -> None:
        """Bind to the callback endpoint and this meeting's job token."""
        self._url = url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def describe(self) -> str:
        """Status line."""
        return "history: archived by the control plane"

    def save(self, report: Any) -> str:
        """POST the report home; returns the brief id, or an empty string on failure."""
        import httpx

        try:
            response = httpx.post(
                f"{self._url}/api/v1/worker/report",
                headers={"Authorization": f"Bearer {self._token}"},
                json={"report": report.model_dump(mode="json")},
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 - teardown must not raise
            logger.error("could not file the briefing with the control plane: %s", exc)
            return ""
        if response.status_code >= 400:
            logger.error("control plane refused the briefing: HTTP %s %s", response.status_code, response.text[:200])
            return ""
        brief_id = str(response.json().get("brief_id", ""))
        logger.info("briefing archived by the control plane as %s", brief_id)
        return brief_id
