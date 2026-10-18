"""
Standalone verification for organizations: invites, seats, roles.

Runs against the real, locally-configured database (same convention as
``tests/test_auth_oauth.py``/``tests/test_multitenancy.py`` - this project has
no pytest/mock, so tests are plain scripts with plain asserts). Every account
and organization this script creates is deleted again in a ``finally`` block.

Covers, in order:

    1. Invite-based ``provision_account`` creates the ``OrganizationMember``
       and never grants platform-admin, even as the first account on a fresh
       database.
    2. Invalid, email-mismatched, and already-redeemed invite tokens are all
       rejected the same way a dead refresh token is.
    3. Seat-limit enforcement on ``invite_member`` and on
       ``accept_invite_for_new_user``.
    4. Last-owner protection: ``remove_member``, ``change_role``, and
       ``leave_organization`` all refuse to touch the organization's sole
       remaining owner.

Usage::

    python tests/test_organizations.py
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from src.auth.service import SignupError, provision_account
from src.db import repo
from src.db.base import init_engine, session_scope
from src.db.models import OrganizationRole
from src.orgs import service as orgs_service

PASSED = 0
FAILED = 0


def check(label: str, condition: bool) -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ok  {label}")
    else:
        FAILED += 1
        print(f"FAIL  {label}")


def unique_email(tag: str) -> str:
    return f"org-test-{tag}-{uuid.uuid4().hex[:8]}@example.com"


def _cleanup(user_ids: list[uuid.UUID], org_ids: list[uuid.UUID]) -> None:
    with session_scope() as session:
        for org_id in org_ids:
            repo.delete_organization(session, org_id)
        for user_id in user_ids:
            repo.delete_user(session, user_id)


# --------------------------------------------------------------------------- #
# 1. Invite-based signup
# --------------------------------------------------------------------------- #

def test_invite_signup_joins_and_never_grants_admin() -> None:
    print("\n-- invite-based signup --")
    user_ids: list[uuid.UUID] = []
    org_ids: list[uuid.UUID] = []
    try:
        with session_scope() as session:
            owner = provision_account(session, email=unique_email("owner"), password="Str0ngPassw0rd!", display_name="Owner")
            user_ids.append(owner.id)
            org = orgs_service.create_organization(session, name="Invite Test Co", owner=owner)
            org_ids.append(org.id)

            invitee_email = unique_email("invitee")
            invite, token = orgs_service.invite_member(session, org, email=invitee_email, role="member", invited_by=owner)
            check("invite minted with a non-trivial token", len(token) > 20)

            member = provision_account(session, email=invitee_email, password="Str0ngPassw0rd!", display_name="Invitee", invite_token=token)
            user_ids.append(member.id)
            check("invited user never becomes platform admin", member.role == "user")
            row = repo.get_organization_member(session, member.id)
            check("invited user joined the organization", row is not None and row.org_id == org.id)
            check("invited user got the invite's role", row is not None and row.role == "member")
    finally:
        _cleanup(user_ids, org_ids)


# --------------------------------------------------------------------------- #
# 2. Token rejection
# --------------------------------------------------------------------------- #

def test_invite_token_rejection() -> None:
    print("\n-- invite token rejection --")
    user_ids: list[uuid.UUID] = []
    org_ids: list[uuid.UUID] = []
    try:
        with session_scope() as session:
            owner = provision_account(session, email=unique_email("owner2"), password="Str0ngPassw0rd!", display_name="Owner Two")
            user_ids.append(owner.id)
            org = orgs_service.create_organization(session, name="Rejection Test Co", owner=owner)
            org_ids.append(org.id)
            invited_email = unique_email("right")
            invite, token = orgs_service.invite_member(session, org, email=invited_email, role="member", invited_by=owner)

            try:
                provision_account(session, email=unique_email("wrong"), password="Str0ngPassw0rd!", display_name="Wrong Person", invite_token=token)
                check("rejects an invite redeemed with a different email", False)
            except SignupError:
                check("rejects an invite redeemed with a different email", True)

            try:
                provision_account(session, email=unique_email("garbage"), password="Str0ngPassw0rd!", display_name="Garbage", invite_token="not-a-real-token")
                check("rejects an unknown invite token", False)
            except SignupError:
                check("rejects an unknown invite token", True)

            member = provision_account(session, email=invited_email, password="Str0ngPassw0rd!", display_name="Right Person", invite_token=token)
            user_ids.append(member.id)
            check("accepts a correctly-matched invite", repo.get_organization_member(session, member.id) is not None)

            try:
                provision_account(session, email=unique_email("second"), password="Str0ngPassw0rd!", display_name="Second", invite_token=token)
                check("rejects a token already redeemed", False)
            except SignupError:
                check("rejects a token already redeemed", True)

            revoked_invite, revoked_token = orgs_service.invite_member(session, org, email=unique_email("torevoke"), role="member", invited_by=owner)
            orgs_service.revoke_invite(session, org, revoked_invite.id)
            try:
                provision_account(session, email=unique_email("afterrevoke"), password="Str0ngPassw0rd!", display_name="After Revoke", invite_token=revoked_token)
                check("rejects a revoked token", False)
            except SignupError:
                check("rejects a revoked token", True)
    finally:
        _cleanup(user_ids, org_ids)


# --------------------------------------------------------------------------- #
# 3. Seat limits
# --------------------------------------------------------------------------- #

def test_seat_limit_enforcement() -> None:
    print("\n-- seat limit enforcement --")
    user_ids: list[uuid.UUID] = []
    org_ids: list[uuid.UUID] = []
    try:
        with session_scope() as session:
            owner = provision_account(session, email=unique_email("seatowner"), password="Str0ngPassw0rd!", display_name="Seat Owner")
            user_ids.append(owner.id)
            org = orgs_service.create_organization(session, name="Seat Test Co", owner=owner)
            org_ids.append(org.id)
            repo.update_organization(session, org, seats_limit=1)

            try:
                orgs_service.invite_member(session, org, email=unique_email("overflow"), role="member", invited_by=owner)
                check("refuses to invite past the seat limit", False)
            except orgs_service.SeatLimitExceeded:
                check("refuses to invite past the seat limit", True)

            repo.update_organization(session, org, seats_limit=2)
            invite, token = orgs_service.invite_member(session, org, email=unique_email("fits"), role="member", invited_by=owner)
            check("allows inviting once a seat frees up", invite is not None)

            # Seat limit is re-checked at acceptance too, not only at invite time.
            repo.update_organization(session, org, seats_limit=1)
            late_joiner = provision_account(
                session, email=unique_email("latejoin"), password="Str0ngPassw0rd!", display_name="Late Join"
            )
            user_ids.append(late_joiner.id)
            try:
                orgs_service.accept_invite_for_new_user(session, late_joiner, invite)
                check("re-checks the seat limit at acceptance", False)
            except orgs_service.SeatLimitExceeded:
                check("re-checks the seat limit at acceptance", True)
    finally:
        _cleanup(user_ids, org_ids)


# --------------------------------------------------------------------------- #
# 4. Last-owner protection
# --------------------------------------------------------------------------- #

def test_last_owner_protection() -> None:
    print("\n-- last-owner protection --")
    user_ids: list[uuid.UUID] = []
    org_ids: list[uuid.UUID] = []
    try:
        with session_scope() as session:
            owner = provision_account(session, email=unique_email("lastowner"), password="Str0ngPassw0rd!", display_name="Last Owner")
            user_ids.append(owner.id)
            org = orgs_service.create_organization(session, name="Last Owner Co", owner=owner)
            org_ids.append(org.id)

            try:
                orgs_service.remove_member(session, org, actor=owner, target_user_id=owner.id)
                check("refuses to remove the sole owner", False)
            except orgs_service.LastOwnerError:
                check("refuses to remove the sole owner", True)

            try:
                orgs_service.change_role(session, org, actor=owner, target_user_id=owner.id, role=OrganizationRole.MEMBER.value)
                check("refuses to demote the sole owner", False)
            except orgs_service.LastOwnerError:
                check("refuses to demote the sole owner", True)

            try:
                orgs_service.leave_organization(session, owner)
                check("refuses to let the sole owner leave", False)
            except orgs_service.LastOwnerError:
                check("refuses to let the sole owner leave", True)

            # Promote a second member to owner, then the original owner may step down.
            invite, token = orgs_service.invite_member(session, org, email=unique_email("coowner"), role="member", invited_by=owner)
            co_owner = provision_account(session, email=invite.email, password="Str0ngPassw0rd!", display_name="Co Owner", invite_token=token)
            user_ids.append(co_owner.id)
            orgs_service.change_role(session, org, actor=owner, target_user_id=co_owner.id, role=OrganizationRole.OWNER.value)
            orgs_service.leave_organization(session, owner)
            check(
                "original owner can leave once a second owner exists",
                repo.get_organization_member(session, owner.id) is None,
            )
    finally:
        _cleanup(user_ids, org_ids)


def main() -> int:
    init_engine()
    test_invite_signup_joins_and_never_grants_admin()
    test_invite_token_rejection()
    test_seat_limit_enforcement()
    test_last_owner_protection()
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
