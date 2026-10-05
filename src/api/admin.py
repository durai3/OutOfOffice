"""
``/api/v1/admin`` - the operator's view across tenants.

    GET  /api/v1/admin/accounts     every account, with its usage and activity
    GET  /api/v1/admin/meetings     every meeting running right now, across tenants
    GET  /api/v1/admin/workspaces   which tenant runtimes are open on this server
    POST /api/v1/admin/accounts/{id}/suspend   stop an account signing in or dispatching
    POST /api/v1/admin/accounts/{id}/activate  undo that

Restricted to ``role = admin``; the first account created on a fresh
deployment gets it, and ``python -m src.main createadmin`` can grant it.

There is deliberately no endpoint here that reads a tenant's transcripts,
briefings, documents or dossier. An operator needs to see *that* an account is
running meetings and burning quota, not what was said in them; anything more
would make the Privacy Policy's promises untrue.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from src.auth.deps import AdminContext
from src.db import repo
from src.db.base import CommittingRoute
from src.db.models import UserStatus
from src.usage.meter import month_start

logger = logging.getLogger("deleg8.api.admin")

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], route_class=CommittingRoute)


class AccountRow(BaseModel):
    """One account, as the operator sees it. No content, only activity."""

    id: str
    email: str
    display_name: str
    role: str
    status: str
    created_at: datetime
    last_login_at: datetime | None = None
    principal: str | None = None
    meetings_this_month: int = 0
    briefs: int = 0
    documents: int = 0
    llm_tokens: float = 0.0
    stt_seconds: float = 0.0
    tts_characters: float = 0.0
    estimated_cost_usd: float = 0.0
    channel_linked: bool = False
    own_keys: list[str] = Field(default_factory=list)


class RunningMeetingRow(BaseModel):
    """One meeting running right now, anywhere on this server."""

    meeting_key: str
    user_id: str
    principal: str
    platform: str
    runtime: str
    phase: str
    activity: str
    consent_state: str
    retaining: bool
    started_at: datetime | None = None
    container: str | None = None


class WorkspaceRow(BaseModel):
    """One open tenant runtime."""

    user_id: str
    principal: str
    open: bool
    collection: str
    escalation: str
    credentials: str
    plan: str
    active_meetings: int
    live_subscribers: int
    opened_at: str | None = None
    last_used_at: str


@router.get("/accounts", response_model=list[AccountRow])
def accounts(context: AdminContext) -> list[AccountRow]:
    """Every account with its activity and consumption this month."""
    session = context.session
    since = month_start()
    rows: list[AccountRow] = []
    for user in repo.list_users(session):
        principal = repo.primary_principal(session, user.id)
        totals = repo.usage_totals(session, user.id, since=since, platform_only=False)
        channel = repo.get_channel(session, user.id, "telegram")
        rows.append(
            AccountRow(
                id=str(user.id),
                email=user.email,
                display_name=user.display_name,
                role=user.role,
                status=user.status,
                created_at=user.created_at,
                last_login_at=user.last_login_at,
                principal=principal.slug if principal is not None else None,
                meetings_this_month=repo.count_meetings_since(session, user.id, since),
                briefs=repo.count_reports(session, user.id),
                documents=len(repo.list_documents(session, user.id)),
                llm_tokens=totals.get("llm", {}).get("units", 0.0),
                stt_seconds=totals.get("stt", {}).get("units", 0.0),
                tts_characters=totals.get("tts", {}).get("units", 0.0),
                estimated_cost_usd=round(sum(t.get("cost_usd", 0.0) for t in totals.values()), 4),
                channel_linked=bool(channel is not None and channel.verified),
                own_keys=[row.provider for row in repo.list_credentials(session, user.id)],
            )
        )
    return rows


@router.get("/meetings", response_model=list[RunningMeetingRow])
def running_meetings(request: Request, context: AdminContext) -> list[RunningMeetingRow]:
    """Every meeting running right now, across every tenant on this server."""
    workspaces = request.app.state.workspaces
    rows: list[RunningMeetingRow] = []
    for workspace in workspaces.opened():
        if workspace.delegate is None:
            continue
        for status_row in workspace.delegate.statuses():
            rows.append(
                RunningMeetingRow(
                    meeting_key=status_row.meeting_id or "-",
                    user_id=str(workspace.user_id),
                    principal=workspace.principal_slug,
                    platform=status_row.platform or "-",
                    runtime=status_row.runtime or "-",
                    phase=status_row.phase,
                    activity=status_row.activity.value,
                    consent_state=status_row.consent_state,
                    retaining=status_row.retaining,
                    started_at=status_row.started_at,
                    container=status_row.container,
                )
            )
    return rows


@router.get("/workspaces", response_model=list[WorkspaceRow])
def workspaces(request: Request, context: AdminContext) -> list[WorkspaceRow]:
    """Which tenant runtimes this server currently holds open."""
    manager = request.app.state.workspaces
    return [WorkspaceRow(**workspace.status()) for workspace in manager.opened()]


@router.post("/accounts/{account_id}/suspend", response_model=AccountRow)
async def suspend(account_id: str, request: Request, context: AdminContext) -> AccountRow:
    """Stop an account signing in or dispatching, and close its workspace."""
    user = _target(context, account_id)
    if user.id == context.user_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "you cannot suspend your own account")
    user.status = UserStatus.SUSPENDED.value
    context.session.add(user)
    repo.revoke_all_auth_sessions(context.session, user.id)
    await request.app.state.workspaces.close(user.id)
    logger.warning("admin %s suspended %s", context.user.email, user.email)
    return _row(context, user)


@router.post("/accounts/{account_id}/activate", response_model=AccountRow)
def activate(account_id: str, context: AdminContext) -> AccountRow:
    """Let a suspended account sign in again."""
    user = _target(context, account_id)
    user.status = UserStatus.ACTIVE.value
    context.session.add(user)
    logger.warning("admin %s reactivated %s", context.user.email, user.email)
    return _row(context, user)


def _target(context: AdminContext, account_id: str):  # noqa: ANN202 - a User row
    try:
        user_id = uuid.UUID(account_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "malformed account id") from exc
    user = repo.get_user(context.session, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such account")
    return user


def _row(context: AdminContext, user) -> AccountRow:  # noqa: ANN001 - a User row
    principal = repo.primary_principal(context.session, user.id)
    return AccountRow(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        principal=principal.slug if principal is not None else None,
    )
