"""
``/api/v1/worker`` - the endpoint dispatched containers call home on.

    GET  /api/v1/worker/bundle    the tenant's policy, dossier and consent mode
    POST /api/v1/worker/events    a batch of pipeline events from a live meeting
    POST /api/v1/worker/report    the finished post-meeting report, for archiving

Authentication is the job token the container was launched with
(:class:`~src.auth.deps.JobContext`). It authorises exactly one meeting, for
exactly one user, and :meth:`~src.api.delegate.DelegateManager._finish` clears
it the moment that meeting reaches a terminal state - so a container that
outlives its call has nothing it can do.

This is what makes a containerised meeting indistinguishable from an
in-process one in the console: every event posted here is replayed through
:meth:`~src.api.delegate.DelegateManager.ingest_event`, the same method the
in-process session calls directly.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from src.auth.deps import WorkerContext
from src.db import repo
from src.db.base import CommittingRoute
from src.db.stores import DbDossierStore, DbHistoryStore, profile_from_principal
from src.dispatch.bundle import MeetingBundle

logger = logging.getLogger("deleg8.api.worker")

router = APIRouter(prefix="/api/v1/worker", tags=["worker"], route_class=CommittingRoute)


class WorkerEvent(BaseModel):
    """One pipeline event as a worker reports it."""

    type: str = Field(..., min_length=1, max_length=80)
    at: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class EventBatch(BaseModel):
    """``POST /api/v1/worker/events``."""

    meeting_id: str | None = Field(default=None, description="The meeting row id, for cross-checking.")
    events: list[WorkerEvent] = Field(..., max_length=100)


class EventAck(BaseModel):
    """How many events were accepted."""

    accepted: int
    tracked: bool = Field(
        ..., description="False when this control plane is not tracking the meeting (e.g. after a restart)."
    )


class ReportSubmission(BaseModel):
    """``POST /api/v1/worker/report`` - a finished post-meeting report."""

    report: dict[str, Any] = Field(..., description="A serialised PostMeetingReport.")


@router.get("/bundle", response_model=MeetingBundle)
def bundle(context: WorkerContext) -> MeetingBundle:
    """Everything this worker needs about its tenant, fetched once at startup.

    Deliberately narrow: one principal's policy, one dossier, one consent mode.
    A worker never receives a database connection, another tenant's anything,
    or its own tenant's credentials beyond the provider keys it was launched
    with.
    """
    meeting = context.meeting
    from src.db.base import session_scope

    with session_scope() as db:
        principal = (
            repo.get_principal(db, meeting.principal_id)
            if meeting.principal_id
            else repo.primary_principal(db, meeting.user_id)
        )
        if principal is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "this meeting's account has no principal to represent"
            )
        profile = profile_from_principal(principal)
        dossier = DbDossierStore(meeting.user_id).load(principal.slug)
        channel = repo.get_channel(db, meeting.user_id, "telegram")
    return MeetingBundle(
        meeting_key=meeting.meeting_key,
        user_id=str(meeting.user_id),
        profile=profile,
        dossier_markdown=dossier.markdown,
        dossier_built_by=(dossier.meta.built_by if dossier.meta else ""),
        consent_mode=meeting.consent_mode,
        principal_name=principal.display_name,
        escalation_available=bool(channel is not None and channel.verified),
    )


@router.post("/events", response_model=EventAck)
async def post_events(body: EventBatch, request: Request, context: WorkerContext) -> EventAck:
    """Replay a worker's events through the tenant's delegate manager and live feed."""
    meeting = context.meeting
    workspaces = request.app.state.workspaces
    workspace = workspaces.get(meeting.user_id)
    if workspace is None or workspace.delegate is None:
        # The control plane restarted, or the workspace was evicted. Opening it
        # here would resurrect an index for a user who is not looking at their
        # console; the events are acknowledged and dropped, and the database row
        # is still updated below from the terminal event.
        logger.debug("no open workspace for %s; dropping %d event(s)", meeting.user_id, len(body.events))
        _apply_terminal(meeting, body)
        return EventAck(accepted=len(body.events), tracked=False)

    delegate = workspace.delegate
    for event in body.events:
        payload = dict(event.payload)
        payload.setdefault("meeting_id", meeting.meeting_key)
        try:
            delegate.ingest_event(meeting.meeting_key, event.type, payload)
        except Exception:  # noqa: BLE001 - one bad event must not reject the batch
            logger.exception("could not ingest %s for %s", event.type, meeting.meeting_key)
    return EventAck(accepted=len(body.events), tracked=True)


def _apply_terminal(meeting: Any, body: EventBatch) -> None:
    """Fold a terminal event into the meeting row when nothing is tracking it.

    Without this a control-plane restart mid-meeting would leave that meeting
    showing as live forever, because the only thing that would have closed it
    was an in-memory object that no longer exists.
    """
    from datetime import datetime, timezone

    from src.db.base import session_scope
    from src.db.models import MeetingStatus

    finished = next((e for e in body.events if e.type == "session.finished"), None)
    if finished is None:
        return
    payload = finished.payload
    exit_code = payload.get("exit_code")
    with session_scope() as db:
        row = repo.get_meeting(db, meeting.user_id, meeting.id)
        if row is None or MeetingStatus(row.status).is_terminal:
            return
        repo.update_meeting(
            db, row,
            status=MeetingStatus.FAILED.value if exit_code not in (None, 0) else MeetingStatus.FINISHED.value,
            ended_at=datetime.now(timezone.utc),
            exit_code=exit_code,
            error=str(payload.get("error") or "")[:2000] or None,
            stop_reason=str(payload.get("reason") or "")[:2000] or None,
            phase=str(payload.get("phase") or row.phase),
            stats=payload.get("stats") if isinstance(payload.get("stats"), dict) else row.stats,
            job_token_hash=None,
        )


class EscalationRelay(BaseModel):
    """``POST /api/v1/worker/escalate`` - a worker asking for a human decision."""

    context: dict[str, Any] = Field(..., description="The serialised MeetingContext.")
    decision: dict[str, Any] = Field(..., description="The serialised PolicyDecision.")
    proposed_action: str = Field(default="", max_length=4000)


class MessageRelay(BaseModel):
    """``POST /api/v1/worker/message`` - a worker pushing a one-way message home."""

    text: str = Field(..., min_length=1, max_length=20000)
    markdown_v2: bool = False
    plain_fallback: str | None = Field(default=None, max_length=20000)


async def _bridge_for(request: Request, context: WorkerContext) -> Any:
    """The tenant's escalation channel, opening their workspace if needed.

    Unlike the event firehose, this one *is* worth opening a workspace for: the
    delegate is standing in a meeting waiting for an answer, and "the console
    was not open" is not a reason to leave it standing there.
    """
    workspace = await request.app.state.workspaces.open(context.meeting.user_id)
    return workspace.bridge


@router.post("/escalate")
async def escalate(body: EscalationRelay, request: Request, context: WorkerContext) -> dict[str, Any]:
    """Put a worker's escalation in front of the right human and return the verdict.

    The worker cannot do this itself: it has no bot token, and giving it one
    would put every tenant's notification channel inside a container that also
    runs a browser. The job token names the meeting; the meeting names the
    user; the user names the chat. There is no field on the wire for "whose
    phone", so a worker cannot address anyone but its own principal.
    """
    from src.brain.models import MeetingContext, PolicyDecision

    try:
        meeting_context = MeetingContext.model_validate(body.context)
        decision = PolicyDecision.model_validate(body.decision)
    except Exception as exc:  # noqa: BLE001 - a malformed escalation is the worker's bug
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unusable escalation: {exc}") from exc

    bridge = await _bridge_for(request, context)
    logger.info(
        "relaying escalation for %s (topic=%s) to %s",
        context.meeting.meeting_key, decision.topic, bridge.describe(),
    )
    result = await bridge.send_escalation_request(meeting_context, body.proposed_action, decision)
    return result.model_dump(mode="json")


@router.post("/message")
async def relay_message(body: MessageRelay, request: Request, context: WorkerContext) -> dict[str, Any]:
    """Deliver a worker's one-way message (the briefing) on the tenant's own channel."""
    bridge = await _bridge_for(request, context)
    receipt = await bridge.send_text(
        body.text, markdown_v2=body.markdown_v2, plain_fallback=body.plain_fallback
    )
    return receipt.model_dump(mode="json")


@router.post("/report", status_code=status.HTTP_201_CREATED)
def post_report(body: ReportSubmission, context: WorkerContext) -> dict[str, str]:
    """Archive a finished report from a worker.

    A container has no database, so the briefing it composed and delivered has
    to come home to be archived - otherwise a containerised meeting would send
    its principal a briefing and then leave no trace of it in the console.
    """
    from src.memory.post_meeting import PostMeetingReport

    meeting = context.meeting
    try:
        report = PostMeetingReport.model_validate(body.report)
    except Exception as exc:  # noqa: BLE001 - a malformed report is the worker's bug, not a 500
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unusable report: {exc}") from exc
    archive = DbHistoryStore(meeting.user_id, meeting.id)
    brief_id = archive.save(report)
    logger.info("archived %s from worker for meeting %s", brief_id, meeting.meeting_key)
    return {"brief_id": brief_id}
