"""Organization lifecycle: creation, invites, seats, roles.

Kept out of the API routes for the same reason ``src.auth.service`` exists:
seat-limit and last-owner enforcement must hold no matter which caller
triggers it - the HTTP router in :mod:`src.api.organizations`, or
``provision_account``'s invite branch during signup.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy.orm import Session

from src.auth.security import hash_token, new_invite_token
from src.db import repo
from src.db.models import Organization, OrganizationInvite, OrganizationMember, OrganizationRole, User

logger = logging.getLogger("deleg8.orgs.service")

INVITE_TTL = timedelta(days=7)
"""How long a shared invite link stays redeemable before it must be resent."""


class OrgError(RuntimeError):
    """An organization operation was refused."""


class SeatLimitExceeded(OrgError):
    """The organization has no seats left to invite into."""


class LastOwnerError(OrgError):
    """The sole remaining owner cannot be demoted, removed, or leave."""


class InviteError(RuntimeError):
    """An invite token could not be redeemed."""


def create_organization(session: Session, *, name: str, owner: User) -> Organization:
    """A new organization, with ``owner`` as its first and only member."""
    if repo.get_organization_member(session, owner.id) is not None:
        raise OrgError("you already belong to an organization")
    org = repo.create_organization(session, name=name, created_by=owner.id)
    repo.create_organization_member(session, org_id=org.id, user_id=owner.id, role=OrganizationRole.OWNER.value)
    logger.info("organization %s (%s) created by %s", org.id, org.name, owner.email)
    return org


def invite_member(
    session: Session, org: Organization, *, email: str, role: str, invited_by: User
) -> tuple[OrganizationInvite, str]:
    """Mint a shareable invite link. Returns ``(invite_row, raw_token)``.

    Only the token's hash is ever persisted - the raw value exists in memory
    only long enough for the caller to build the link.
    """
    if repo.count_organization_members(session, org.id) >= org.seats_limit:
        raise SeatLimitExceeded(f"{org.name} has no seats left (limit {org.seats_limit})")
    token, token_hash = new_invite_token()
    invite = repo.create_invite(
        session,
        org_id=org.id,
        email=email,
        role=role,
        token_hash=token_hash,
        invited_by=invited_by.id,
        ttl=INVITE_TTL,
    )
    logger.info("%s invited %s to organization %s as %s", invited_by.email, email, org.id, role)
    return invite, token


def resend_invite(
    session: Session, org: Organization, invite_id: uuid.UUID, *, invited_by: User
) -> tuple[OrganizationInvite, str]:
    """Revoke the old link and mint a fresh one for the same email and role."""
    invite = repo.get_invite(session, invite_id)
    if invite is None or invite.org_id != org.id or not invite.is_pending:
        raise InviteError("that invite no longer exists or has already been used")
    repo.revoke_invite(session, invite.id)
    return invite_member(session, org, email=invite.email, role=invite.role, invited_by=invited_by)


def revoke_invite(session: Session, org: Organization, invite_id: uuid.UUID) -> None:
    """Revoke one of this organization's pending invites."""
    invite = repo.get_invite(session, invite_id)
    if invite is None or invite.org_id != org.id:
        raise InviteError("invite not found")
    if not repo.revoke_invite(session, invite.id):
        raise InviteError("that invite has already been used or revoked")


def accept_invite_for_new_user(session: Session, user: User, invite: OrganizationInvite) -> OrganizationMember:
    """Join the just-signed-up ``user`` to the organization that invited them."""
    org = repo.get_organization(session, invite.org_id)
    if org is None:
        raise InviteError("this organization no longer exists")
    if repo.count_organization_members(session, org.id) >= org.seats_limit:
        raise SeatLimitExceeded(f"{org.name} has no seats left (limit {org.seats_limit})")
    member = repo.create_organization_member(session, org_id=org.id, user_id=user.id, role=invite.role)
    repo.mark_invite_accepted(session, invite)
    logger.info("%s joined organization %s as %s via invite", user.email, org.id, invite.role)
    return member


def accept_invite_for_existing_user(session: Session, user: User, token: str) -> OrganizationMember:
    """An already-registered, already-authenticated user redeeming an invite link."""
    if repo.get_organization_member(session, user.id) is not None:
        raise OrgError("you already belong to an organization")
    invite = repo.get_invite_by_token_hash(session, hash_token(token))
    if invite is None:
        raise InviteError("this invite link is invalid or has expired")
    if invite.email.strip().lower() != user.email.strip().lower():
        raise InviteError("this invite was sent to a different email address")
    return accept_invite_for_new_user(session, user, invite)


def change_role(
    session: Session, org: Organization, *, actor: User, target_user_id: uuid.UUID, role: str
) -> OrganizationMember:
    """Change a member's role, refusing to strip the organization's last owner."""
    member = repo.get_organization_member_by_ids(session, org.id, target_user_id)
    if member is None:
        raise OrgError("that user is not a member of this organization")
    if member.role == OrganizationRole.OWNER.value and role != OrganizationRole.OWNER.value:
        _require_not_last_owner(session, org, target_user_id)
    updated = repo.set_member_role(session, org.id, target_user_id, role=role)
    assert updated is not None  # the lookup above already confirmed membership
    logger.info("%s set %s's role to %s in organization %s", actor.email, target_user_id, role, org.id)
    return updated


def remove_member(session: Session, org: Organization, *, actor: User, target_user_id: uuid.UUID) -> None:
    """Remove a membership row. The member's own account and data are untouched."""
    member = repo.get_organization_member_by_ids(session, org.id, target_user_id)
    if member is None:
        raise OrgError("that user is not a member of this organization")
    if member.role == OrganizationRole.OWNER.value:
        _require_not_last_owner(session, org, target_user_id)
    repo.delete_organization_member(session, org.id, target_user_id)
    logger.info("%s removed %s from organization %s", actor.email, target_user_id, org.id)


def leave_organization(session: Session, user: User) -> None:
    """The caller removes themself, refusing if they are the sole remaining owner."""
    member = repo.get_organization_member(session, user.id)
    if member is None:
        raise OrgError("you are not a member of an organization")
    if member.role == OrganizationRole.OWNER.value:
        org = repo.get_organization(session, member.org_id)
        if org is not None:
            _require_not_last_owner(session, org, user.id)
    repo.delete_organization_member(session, member.org_id, user.id)
    logger.info("%s left organization %s", user.email, member.org_id)


def _require_not_last_owner(session: Session, org: Organization, user_id: uuid.UUID) -> None:
    """Raise :class:`LastOwnerError` when ``user_id`` is the organization's only owner."""
    owners = {
        member.user_id
        for member in repo.list_organization_members(session, org.id)
        if member.role == OrganizationRole.OWNER.value
    }
    if owners == {user_id}:
        raise LastOwnerError("an organization must keep at least one owner")
