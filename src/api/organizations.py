"""
``/api/v1/organizations`` - teams: seats, invites, member roles.

    POST   /api/v1/organizations                       create one, caller becomes its owner
    GET    /api/v1/organizations/me                     the caller's organization
    GET    /api/v1/organizations/members                its members (usage columns: admins only)
    POST   /api/v1/organizations/invites                invite by email (owner/admin only)
    GET    /api/v1/organizations/invites                pending invites (owner/admin only)
    POST   /api/v1/organizations/invites/{id}/resend     mint a fresh link (owner/admin only)
    DELETE /api/v1/organizations/invites/{id}            revoke (owner/admin only)
    PATCH  /api/v1/organizations/members/{user_id}       change a member's role (owner/admin only)
    DELETE /api/v1/organizations/members/{user_id}       remove a member (owner/admin only)
    POST   /api/v1/organizations/leave                   the caller leaves their own organization
    GET    /api/v1/organizations/invites/by-token/{tok}  preview an invite; not session-authed
    POST   /api/v1/organizations/invites/by-token/{tok}/accept  an existing, signed-in user joins

There is no payment integration here: ``seats_limit`` is a plain number an
owner can see but only ever changes by someone editing the organization
directly - nothing in this router charges anyone. Invites are link-only: the
``invite_url`` this router returns is a path, not an absolute URL (the
frontend knows its own origin; the backend does not need to), shown once like
a freshly generated API key, for the inviter to copy and send however they
like.

As with ``admin.py``, there is deliberately no endpoint here that reads a
teammate's meeting content, transcripts, knowledge or dossier - an org
admin sees that a teammate is active and what they cost, never what was said.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.auth.deps import CurrentContext, OrgAdminContext, TenantContext
from src.auth.security import hash_token
from src.db import repo
from src.db.base import CommittingRoute, db_session
from src.db.models import Organization, OrganizationRole
from src.orgs import service as orgs_service
from src.orgs.models import (
    CreateOrganizationRequest,
    InvitePreview,
    InviteRequest,
    InviteRow,
    MemberRow,
    OrganizationSummary,
    RoleUpdateRequest,
)
from src.usage.meter import month_start, plan_for

router = APIRouter(prefix="/api/v1/organizations", tags=["organizations"], route_class=CommittingRoute)


def _invite_path(token: str) -> str:
    """The frontend route an invite link resolves to, relative to its own origin."""
    return f"/invite/{token}"


def _invite_row(invite, *, token: str | None = None) -> InviteRow:
    return InviteRow(
        id=str(invite.id),
        email=invite.email,
        role=invite.role,
        invite_url=_invite_path(token) if token is not None else None,
        expires_at=invite.expires_at,
        accepted_at=invite.accepted_at,
        revoked_at=invite.revoked_at,
    )


def _org_summary(session: Session, org: Organization, role: str) -> OrganizationSummary:
    return OrganizationSummary(
        id=str(org.id),
        name=org.name,
        role=role,
        plan=plan_for(org.settings).name,
        seats_used=repo.count_organization_members(session, org.id),
        seats_limit=org.seats_limit,
    )


def _require_org(context: TenantContext) -> Organization:
    if context.org is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you are not a member of an organization")
    return context.org


@router.post("", response_model=OrganizationSummary)
def create(body: CreateOrganizationRequest, context: CurrentContext) -> OrganizationSummary:
    """A logged-in standalone user creates an organization and becomes its owner."""
    if context.org is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "you already belong to an organization")
    org = orgs_service.create_organization(context.session, name=body.name, owner=context.user)
    return _org_summary(context.session, org, OrganizationRole.OWNER.value)


@router.get("/me", response_model=OrganizationSummary)
def my_organization(context: CurrentContext) -> OrganizationSummary:
    """The caller's own organization."""
    org = _require_org(context)
    return _org_summary(context.session, org, context.org_role or OrganizationRole.MEMBER.value)


@router.get("/members", response_model=list[MemberRow])
def members(context: CurrentContext) -> list[MemberRow]:
    """Every member of the caller's organization. Usage columns are admin-only."""
    org = _require_org(context)
    session = context.session
    rows = repo.list_organization_members(session, org.id)
    usage = (
        repo.org_usage_totals(session, [row.user_id for row in rows], since=month_start())
        if context.is_org_admin
        else {}
    )
    out: list[MemberRow] = []
    for row in rows:
        user = repo.get_user(session, row.user_id)
        if user is None:
            continue
        member_usage = usage.get(row.user_id, {})
        out.append(
            MemberRow(
                user_id=str(row.user_id),
                email=user.email,
                display_name=user.display_name,
                role=row.role,
                joined_at=row.created_at,
                meetings_this_month=(
                    repo.count_meetings_since(session, row.user_id, month_start())
                    if context.is_org_admin
                    else None
                ),
                llm_tokens=member_usage.get("llm", {}).get("units") if context.is_org_admin else None,
                estimated_cost_usd=(
                    round(sum(service.get("cost_usd", 0.0) for service in member_usage.values()), 4)
                    if context.is_org_admin
                    else None
                ),
            )
        )
    return out


@router.post("/invites", response_model=InviteRow)
def invite(body: InviteRequest, context: OrgAdminContext) -> InviteRow:
    """Mint a one-time invite link. Shown once - the caller must copy it now."""
    try:
        row, token = orgs_service.invite_member(
            context.session, context.org, email=str(body.email), role=body.role.value, invited_by=context.user
        )
    except orgs_service.SeatLimitExceeded as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _invite_row(row, token=token)


@router.get("/invites", response_model=list[InviteRow])
def list_invites(context: OrgAdminContext) -> list[InviteRow]:
    """Pending invites for the caller's organization."""
    rows = repo.list_pending_invites(context.session, context.org.id)
    return [_invite_row(row) for row in rows]


@router.post("/invites/{invite_id}/resend", response_model=InviteRow)
def resend(invite_id: str, context: OrgAdminContext) -> InviteRow:
    """Revoke the old link and mint a fresh one for the same invite."""
    try:
        target_id = uuid.UUID(invite_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "malformed invite id") from exc
    try:
        row, token = orgs_service.resend_invite(context.session, context.org, target_id, invited_by=context.user)
    except orgs_service.InviteError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except orgs_service.SeatLimitExceeded as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _invite_row(row, token=token)


@router.delete("/invites/{invite_id}")
def revoke(invite_id: str, context: OrgAdminContext) -> dict[str, str]:
    """Revoke a pending invite."""
    try:
        target_id = uuid.UUID(invite_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "malformed invite id") from exc
    try:
        orgs_service.revoke_invite(context.session, context.org, target_id)
    except orgs_service.InviteError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"detail": "invite revoked"}


@router.patch("/members/{user_id}", response_model=MemberRow)
def set_role(user_id: str, body: RoleUpdateRequest, context: OrgAdminContext) -> MemberRow:
    """Change a member's role within the organization."""
    target_id = _parse_user_id(user_id)
    try:
        member = orgs_service.change_role(
            context.session, context.org, actor=context.user, target_user_id=target_id, role=body.role.value
        )
    except orgs_service.LastOwnerError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except orgs_service.OrgError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    user = repo.get_user(context.session, target_id)
    assert user is not None
    return MemberRow(
        user_id=str(member.user_id), email=user.email, display_name=user.display_name,
        role=member.role, joined_at=member.created_at,
    )


@router.delete("/members/{user_id}")
def remove(user_id: str, context: OrgAdminContext) -> dict[str, str]:
    """Remove a member. Their account and data are untouched - they revert to solo."""
    target_id = _parse_user_id(user_id)
    try:
        orgs_service.remove_member(context.session, context.org, actor=context.user, target_user_id=target_id)
    except orgs_service.LastOwnerError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except orgs_service.OrgError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"detail": "member removed"}


@router.post("/leave")
def leave(context: CurrentContext) -> dict[str, str]:
    """The caller leaves their own organization."""
    try:
        orgs_service.leave_organization(context.session, context.user)
    except orgs_service.LastOwnerError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except orgs_service.OrgError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"detail": "left organization"}


@router.get("/invites/by-token/{token}", response_model=InvitePreview)
def preview_invite(token: str, session: Annotated[Session, Depends(db_session)]) -> InvitePreview:
    """What an invite-landing page shows before anyone has signed in. Not session-authed."""
    invite = repo.get_invite_by_token_hash(session, hash_token(token))
    if invite is None:
        return InvitePreview(org_name="", email="", role=OrganizationRole.MEMBER, valid=False)
    org = repo.get_organization(session, invite.org_id)
    return InvitePreview(
        org_name=org.name if org is not None else "",
        email=invite.email,
        role=invite.role,
        valid=org is not None,
    )


@router.post("/invites/by-token/{token}/accept", response_model=MemberRow)
def accept(token: str, context: CurrentContext) -> MemberRow:
    """An already-registered, already-authenticated user redeems an invite link."""
    try:
        member = orgs_service.accept_invite_for_existing_user(context.session, context.user, token)
    except orgs_service.SeatLimitExceeded as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (orgs_service.InviteError, orgs_service.OrgError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return MemberRow(
        user_id=str(member.user_id),
        email=context.user.email,
        display_name=context.user.display_name,
        role=member.role,
        joined_at=member.created_at,
    )


def _parse_user_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "malformed user id") from exc
