"""
Account lifecycle: signup, sign-in, refresh, and what a new account comes with.

Signing up is not just a row in ``users``. A usable account needs, atomically:

* the user;
* a :class:`~src.db.models.Principal` - the person the delegate represents -
  with a globally unique slug, because that slug is what every downstream
  component means when it says ``user_id``;
* a starter Representation Policy, because a policy with no rules is invalid
  and a delegate with no RED rules is dangerous;
* a recorded acceptance of the current Terms of Service and Privacy Policy;
* an unverified Telegram channel holding a one-time link code, so onboarding
  has something to show.

All of it happens in one transaction: a half-provisioned account that can sign
in but has no principal would fail deep inside the brain on the first meeting.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.auth.models import AccountResponse, PrincipalSummary
from src.auth.security import (
    AuthError,
    create_access_token,
    hash_password,
    hash_token,
    new_refresh_token,
    refresh_ttl,
    verify_password,
)
from src.brain.policy_templates import DEFAULT_TEMPLATE, build_profile
from src.compliance import legal
from src.db import repo
from src.db.models import User
from src.db.stores import slugify

logger = logging.getLogger("deleg8.auth.service")

ONBOARDING_STEPS: tuple[str, ...] = ("legal", "policy", "escalation", "documents")
"""The steps :mod:`src.api.onboarding` walks a new account through."""


class SignupError(RuntimeError):
    """Signup was refused (duplicate email, invalid template)."""


class CredentialsError(AuthError):
    """Email or password did not match."""


class AccountDisabledError(AuthError):
    """The account exists but may not sign in."""


def account_response(session: Session, user: User) -> AccountResponse:
    """Project a user row onto the shape the console holds."""
    principal = repo.primary_principal(session, user.id)
    onboarding = {step: bool((user.onboarding or {}).get(step)) for step in ONBOARDING_STEPS}
    outstanding = legal.outstanding_documents(session, user.id)
    if outstanding:
        onboarding["legal"] = False
    return AccountResponse(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        principal=(
            PrincipalSummary(
                id=str(principal.id),
                slug=principal.slug,
                display_name=principal.display_name,
                aliases=list(principal.aliases or []),
            )
            if principal is not None
            else None
        ),
        onboarding=onboarding,
        onboarding_complete=all(onboarding.values()),
        outstanding_legal=outstanding,
        settings=dict(user.settings or {}),
    )


def provision_account(
    session: Session,
    *,
    email: str,
    password: str,
    display_name: str,
    policy_template: str = DEFAULT_TEMPLATE,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> User:
    """Create a fully usable account, or raise :class:`SignupError`.

    The very first account on a fresh deployment is made an admin, so somebody
    can see platform-wide usage without a manual database edit.
    """
    if repo.get_user_by_email(session, email) is not None:
        raise SignupError("an account with that email already exists")

    role = "admin" if repo.count_users(session) == 0 else "user"
    try:
        user = repo.create_user(
            session,
            email=email,
            password_hash=hash_password(password),
            display_name=display_name,
            role=role,
        )
    except IntegrityError as exc:  # pragma: no cover - a race on the unique index
        raise SignupError("an account with that email already exists") from exc

    slug = repo.unique_slug(session, slugify(display_name, fallback="principal"))
    principal = repo.create_principal(
        session, user_id=user.id, slug=slug, display_name=display_name, aliases=[]
    )

    profile = build_profile(slug=slug, display_name=display_name, template=policy_template)
    repo.replace_policy(
        session,
        principal,
        display_name=profile.display_name,
        aliases=profile.aliases,
        rules=[
            {
                "topic": rule.topic,
                "tier": rule.tier.value,
                "description": rule.description,
                "triggers": list(rule.triggers),
            }
            for rule in profile.policy
        ],
        knowledge=[
            {
                "topic": entry.topic,
                "subject": entry.subject,
                "keywords": list(entry.keywords),
                "answer": entry.answer,
            }
            for entry in profile.knowledge_base
        ],
    )

    legal.accept_all(session, user.id, ip_address=ip_address, user_agent=user_agent)
    # An empty channel row, with no link code yet: onboarding mints one with a
    # real expiry window when the user opens the "connect Telegram" step, so a
    # valid code never sits in the database for an account nobody finished.
    repo.upsert_channel(session, user_id=user.id, kind="telegram", verified=False)
    repo.set_onboarding(session, user, legal=True, policy=True, escalation=False, documents=False)
    logger.info("provisioned account %s (%s) with principal %s", user.email, role, slug)
    return user


def issue_tokens(
    session: Session, user: User, *, user_agent: str | None = None
) -> tuple[str, str, datetime]:
    """Mint an access token and a refresh token. Returns ``(access, refresh, expires_at)``."""
    principal = repo.primary_principal(session, user.id)
    access, expires = create_access_token(
        str(user.id),
        extra={
            "email": user.email,
            "role": user.role,
            "principal": principal.slug if principal is not None else None,
        },
    )
    refresh, refresh_hash = new_refresh_token()
    repo.create_auth_session(
        session, user_id=user.id, token_hash=refresh_hash, ttl=refresh_ttl(), user_agent=user_agent
    )
    return access, refresh, expires


def authenticate(session: Session, *, email: str, password: str) -> User:
    """Verify credentials and return the user.

    Raises:
        CredentialsError: wrong email or password (deliberately indistinguishable).
        AccountDisabledError: correct credentials, suspended account.
    """
    user = repo.get_user_by_email(session, email)
    if user is None:
        # Spend roughly the same time as a real verification so the response
        # time does not reveal whether an address is registered.
        verify_password(password, "$2b$12$" + "x" * 53)
        raise CredentialsError("incorrect email or password")
    if not verify_password(password, user.password_hash):
        raise CredentialsError("incorrect email or password")
    if not user.is_active:
        raise AccountDisabledError("this account is suspended; contact support")
    repo.touch_login(session, user)
    return user


def rotate_refresh_token(
    session: Session, refresh_token: str, *, user_agent: str | None = None
) -> tuple[User, str, str, datetime]:
    """Exchange a refresh token for a new pair, revoking the old one.

    Rotation on every use means a stolen refresh token is usable at most once,
    and the legitimate client's next refresh fails loudly rather than silently
    sharing a session with an attacker.

    Raises:
        AuthError: the token is unknown, revoked, or expired.
    """
    token_hash = hash_token(refresh_token)
    row = repo.get_auth_session(session, token_hash)
    if row is None:
        raise AuthError("refresh token is invalid or has expired; sign in again")
    user = repo.get_user(session, row.user_id)
    if user is None or not user.is_active:
        raise AuthError("account is no longer active")
    repo.revoke_auth_session(session, token_hash)
    access, refresh, expires = issue_tokens(session, user, user_agent=user_agent)
    return user, access, refresh, expires


def change_password(session: Session, user: User, *, current: str, new: str) -> int:
    """Change a password and revoke every session. Returns how many were revoked.

    Raises:
        CredentialsError: the current password did not match.
    """
    if not verify_password(current, user.password_hash):
        raise CredentialsError("current password is incorrect")
    user.password_hash = hash_password(new)
    session.add(user)
    return repo.revoke_all_auth_sessions(session, user.id)


def sign_out(session: Session, refresh_token: str | None) -> bool:
    """Revoke one refresh token, if it is still live."""
    if not refresh_token:
        return False
    return repo.revoke_auth_session(session, hash_token(refresh_token))


def delete_account(session: Session, user_id: uuid.UUID) -> bool:
    """Erase an account and every row that cascades from it."""
    return repo.delete_user(session, user_id)
