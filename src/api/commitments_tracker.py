"""
``/api/v1/commitments/tracker`` - a flat, sortable commitments table.

    GET   /api/v1/commitments/tracker?status=open&sort=due_date   one row per commitment
    PATCH /api/v1/commitments/tracker/{id}                        mark done, drop, or snooze

Separate from ``GET /api/v1/commitments`` in :mod:`src.api.routes`, which
groups open items by meeting for the dashboard card - this returns one row
per commitment regardless of meeting, which is what a Person / Commitment /
Due Date / Status table needs, and exists purely additively so the existing
dashboard contract is untouched.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel, Field

from src.auth.deps import CurrentContext
from src.db import repo
from src.db.base import CommittingRoute
from src.db.models import CommitmentRow

router = APIRouter(prefix="/api/v1/commitments/tracker", tags=["commitments"], route_class=CommittingRoute)


class CommitmentTrackerRow(BaseModel):
    """One commitment, as the tracker table renders it."""

    id: str
    meeting_id: str
    meeting_key: str | None = None
    owner: str
    requested_by: str
    description: str
    due_iso: str
    due_text: str
    status: str
    overdue: bool


class TrackerPatchRequest(BaseModel):
    """Mark a commitment done, drop it, or push its due date out."""

    action: Literal["done", "dropped", "snooze"]
    snooze_days: int = Field(default=1, ge=1, le=30)


def _overdue(row: CommitmentRow) -> bool:
    if row.status not in ("probable", "confirmed") or not row.due_iso:
        return False
    try:
        return date.fromisoformat(row.due_iso) < datetime.now(timezone.utc).date()
    except ValueError:
        return False


def _row_out(row: CommitmentRow, meeting_key: str | None) -> CommitmentTrackerRow:
    return CommitmentTrackerRow(
        id=str(row.id),
        meeting_id=str(row.meeting_id),
        meeting_key=meeting_key,
        owner=row.owner or "Unassigned",
        requested_by=row.requested_by,
        description=row.description,
        due_iso=row.due_iso,
        due_text=row.due_text,
        status=row.status,
        overdue=_overdue(row),
    )


@router.get("", response_model=list[CommitmentTrackerRow])
def tracker(
    context: CurrentContext,
    status_: str | None = Query(default="open", alias="status"),
    sort: str = Query(default="due_date"),
) -> list[CommitmentTrackerRow]:
    """This account's commitments as a flat, sortable list."""
    rows = repo.list_commitments_tracker(context.session, context.user_id, status=status_, sort=sort)
    keys = {m.id: m.meeting_key for m in repo.list_meetings(context.session, context.user_id, limit=500)}
    return [_row_out(row, keys.get(row.meeting_id)) for row in rows]


@router.patch("/{commitment_id}", response_model=CommitmentTrackerRow)
def patch_tracker(commitment_id: uuid.UUID, body: TrackerPatchRequest, context: CurrentContext) -> CommitmentTrackerRow:
    """Mark done, drop, or snooze one of this account's commitments."""
    if body.action == "done":
        row = repo.set_commitment_status(context.session, context.user_id, commitment_id, status="done")
    elif body.action == "dropped":
        row = repo.set_commitment_status(context.session, context.user_id, commitment_id, status="dropped")
    else:
        row = repo.snooze_commitment(context.session, context.user_id, commitment_id, days=body.snooze_days)
    if row is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, f"no commitment {commitment_id}")
    meeting = repo.get_meeting(context.session, context.user_id, row.meeting_id)
    return _row_out(row, meeting.meeting_key if meeting else None)
