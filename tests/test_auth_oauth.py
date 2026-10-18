"""
Standalone verification for "Continue with Google / Microsoft" SSO login.

Runs against the real, locally-configured database (same convention as
``tests/test_escalation.py``/``tests/test_multitenancy.py`` - this project has
no pytest/mock, so tests are plain scripts with plain asserts). Every account
this script creates is deleted again in a ``finally`` block, so a run leaves
no trace even on a shared dev database.

Covers, in order:

    1. Login-state token round-trip, including cross-type rejection against
       a real access token and a real connector oauth_state token.
    2. ``provision_or_link_oauth_account``'s resolution order: brand-new
       account, missing-consent refusal, auto-link onto an existing password
       account, unverified-email refusal, suspended-account refusal, and
       repeat sign-in resolving to the same user without a duplicate.
    3. The ``authenticate()`` guard: a password-login attempt against an
       SSO-only account (``password_hash is None``) fails cleanly rather
       than crashing.
    4. ``GoogleLoginProvider``/``MicrosoftLoginProvider._fetch_identity``
       against a hand-rolled fake httpx client - no real network call.

Usage::

    python tests/test_auth_oauth.py
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any, ClassVar, Self

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

import httpx

from src.auth.oauth_identity import (
    GoogleLoginProvider,
    MicrosoftLoginProvider,
)
from src.auth.security import (
    AuthError,
    create_access_token,
    create_oauth_login_state_token,
    create_oauth_state_token,
    decode_oauth_login_state_token,
)
from src.auth.service import (
    AccountDisabledError,
    CredentialsError,
    OAuthLinkError,
    authenticate,
    provision_account,
    provision_or_link_oauth_account,
)
from src.db import repo
from src.db.base import init_engine, session_scope
from src.db.models import UserStatus

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
    return f"sso-test-{tag}-{uuid.uuid4().hex[:8]}@example.com"


# --------------------------------------------------------------------------- #
# 1. Login-state token round-trip
# --------------------------------------------------------------------------- #

def test_login_state_token() -> None:
    print("\n-- login-state token round-trip --")
    token = create_oauth_login_state_token(provider="google", frontend_origin="http://localhost:3000", accept_terms=True)
    claims = decode_oauth_login_state_token(token)
    check("round-trips provider/origin/accept_terms", claims["provider"] == "google" and claims["origin"] == "http://localhost:3000" and claims["accept_terms"] is True)
    check("subject is a nonce, not a real id", claims["sub"] != "" and len(claims["sub"]) > 10)

    # Cross-type rejection: a real access token or a connector's oauth_state
    # token must never be accepted as a login-state token, and vice versa.
    access, _ = create_access_token("some-user-id")
    try:
        decode_oauth_login_state_token(access)
        check("rejects a real access token", False)
    except AuthError:
        check("rejects a real access token", True)

    connector_state = create_oauth_state_token(uuid.uuid4(), "google_calendar", "http://localhost:3000")
    try:
        decode_oauth_login_state_token(connector_state)
        check("rejects a connector oauth_state token", False)
    except AuthError:
        check("rejects a connector oauth_state token", True)

    from src.auth.security import decode_access_token, decode_oauth_state_token
    try:
        decode_access_token(token)
        check("a login-state token is rejected as an access token", False)
    except AuthError:
        check("a login-state token is rejected as an access token", True)
    try:
        decode_oauth_state_token(token)
        check("a login-state token is rejected as a connector state token", False)
    except AuthError:
        check("a login-state token is rejected as a connector state token", True)


# --------------------------------------------------------------------------- #
# 2. provision_or_link_oauth_account
# --------------------------------------------------------------------------- #

def test_provision_or_link() -> None:
    print("\n-- provision_or_link_oauth_account --")
    created_user_ids: list[uuid.UUID] = []
    try:
        with session_scope() as session:
            # Brand-new account, verified email, consent given -> created.
            email = unique_email("new")
            subject = f"sub-{uuid.uuid4().hex}"
            user, created = provision_or_link_oauth_account(
                session,
                provider="google",
                provider_subject=subject,
                email=email,
                email_verified=True,
                display_name="New SSO User",
                accept_terms=True,
            )
            created_user_ids.append(user.id)
            check("brand-new SSO signup is created", created is True)
            check("auth_provider is the SSO provider", user.auth_provider == "google")
            check("password_hash is None for an SSO-only account", user.password_hash is None)
            check("onboarding provisioning ran (principal exists)", repo.primary_principal(session, user.id) is not None)

            # Same identity signs in again -> same user, not a duplicate.
            user2, created2 = provision_or_link_oauth_account(
                session,
                provider="google",
                provider_subject=subject,
                email=email,
                email_verified=True,
                display_name="New SSO User",
                accept_terms=False,  # irrelevant on a repeat sign-in
            )
            check("repeat sign-in resolves to the same user", user2.id == user.id)
            check("repeat sign-in does not re-create", created2 is False)

            # Missing consent on a genuinely new email -> refused, nothing created.
            email2 = unique_email("noconsent")
            try:
                provision_or_link_oauth_account(
                    session,
                    provider="google",
                    provider_subject=f"sub-{uuid.uuid4().hex}",
                    email=email2,
                    email_verified=True,
                    display_name="Should Not Be Created",
                    accept_terms=False,
                )
                check("refuses a new account without accept_terms", False)
            except OAuthLinkError:
                check("refuses a new account without accept_terms", True)
            check("no account was created for the refused email", repo.get_user_by_email(session, email2) is None)

            # Unverified email with an existing user at that address -> refused.
            existing_email = unique_email("existing-password")
            existing = provision_account(session, email=existing_email, password="a-strong-password-1", display_name="Existing Password User")
            created_user_ids.append(existing.id)
            unverified_subject = f"sub-{uuid.uuid4().hex}"
            try:
                provision_or_link_oauth_account(
                    session,
                    provider="microsoft",
                    provider_subject=unverified_subject,
                    email=existing_email,
                    email_verified=False,
                    display_name="Attacker",
                    accept_terms=True,
                )
                check("refuses an unverified email even if an account already exists", False)
            except OAuthLinkError:
                check("refuses an unverified email even if an account already exists", True)
            check(
                "no identity was linked for the unverified attempt",
                repo.get_oauth_identity(session, provider="microsoft", provider_subject=unverified_subject) is None,
            )

            # Verified email matching an existing password account -> auto-link.
            linked, link_created = provision_or_link_oauth_account(
                session,
                provider="microsoft",
                provider_subject=f"sub-{uuid.uuid4().hex}",
                email=existing_email,
                email_verified=True,
                display_name="Existing Password User",
                accept_terms=True,
            )
            check("auto-links onto the existing password account", linked.id == existing.id)
            check("auto-link does not mark it as created", link_created is False)
            check("auto-link leaves auth_provider as local", linked.auth_provider == "local")
            check("auto-link leaves the original password intact", linked.password_hash is not None)

            # Suspended account resolved via identity -> refused.
            suspended = provision_account(session, email=unique_email("suspended"), password=None, display_name="Suspended SSO User", auth_provider="google")
            created_user_ids.append(suspended.id)
            subject = f"sub-{uuid.uuid4().hex}"
            repo.create_oauth_identity(session, user_id=suspended.id, provider="google", provider_subject=subject, provider_email=suspended.email)
            suspended.status = UserStatus.SUSPENDED.value
            session.add(suspended)
            session.flush()
            try:
                provision_or_link_oauth_account(
                    session, provider="google", provider_subject=subject, email=suspended.email, email_verified=True, display_name="Suspended SSO User",
                )
                check("refuses a suspended account", False)
            except AccountDisabledError:
                check("refuses a suspended account", True)
    finally:
        _cleanup(created_user_ids)


# --------------------------------------------------------------------------- #
# 3. authenticate() guard against a null password_hash
# --------------------------------------------------------------------------- #

def test_authenticate_sso_only_account() -> None:
    print("\n-- authenticate() guard for SSO-only accounts --")
    created_user_ids: list[uuid.UUID] = []
    try:
        with session_scope() as session:
            email = unique_email("ssoonly")
            user = provision_account(session, email=email, password=None, display_name="SSO Only", auth_provider="google")
            created_user_ids.append(user.id)
            try:
                authenticate(session, email=email, password="whatever-password-123")
                check("password login against an SSO-only account fails cleanly", False)
            except CredentialsError:
                check("password login against an SSO-only account fails cleanly", True)
            except Exception as exc:  # noqa: BLE001 - this is exactly the crash we're checking does NOT happen
                check(f"password login against an SSO-only account fails cleanly (got {type(exc).__name__}, not CredentialsError)", False)
    finally:
        _cleanup(created_user_ids)


def _cleanup(user_ids: list[uuid.UUID]) -> None:
    if not user_ids:
        return
    with session_scope() as session:
        for user_id in user_ids:
            repo.delete_user(session, user_id)


# --------------------------------------------------------------------------- #
# 4. Login identity providers against a fake httpx client
# --------------------------------------------------------------------------- #

class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeAsyncClient:
    """Routes .post()/.get() by URL substring to a canned response. No network I/O."""

    _routes: ClassVar[dict[str, dict[str, Any]]] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._respond(url)

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._respond(url)

    def _respond(self, url: str) -> FakeResponse:
        for substring, payload in self._routes.items():
            if substring in url:
                return FakeResponse(200, payload)
        return FakeResponse(404, {"error": "no fake route configured"})


async def _run_fetch_identity_tests() -> None:
    print("\n-- login identity providers (fake httpx, no network) --")
    original_client = httpx.AsyncClient
    httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment,misc]
    original_env = {k: os.environ.get(k) for k in ("GOOGLE_SSO_CLIENT_ID", "GOOGLE_SSO_CLIENT_SECRET", "MICROSOFT_SSO_CLIENT_ID", "MICROSOFT_SSO_CLIENT_SECRET")}
    os.environ["GOOGLE_SSO_CLIENT_ID"] = "fake-google-client"
    os.environ["GOOGLE_SSO_CLIENT_SECRET"] = "fake-google-secret"
    os.environ["MICROSOFT_SSO_CLIENT_ID"] = "fake-ms-client"
    os.environ["MICROSOFT_SSO_CLIENT_SECRET"] = "fake-ms-secret"
    try:
        google = GoogleLoginProvider()
        FakeAsyncClient._routes = {
            "openidconnect.googleapis.com/v1/userinfo": {
                "sub": "google-sub-123", "email": "Person@Example.com", "email_verified": True, "name": "Example Person",
            }
        }
        identity = await google._fetch_identity("fake-access-token")
        check("Google identity: subject parsed", identity.subject == "google-sub-123")
        check("Google identity: email lower-cased", identity.email == "person@example.com")
        check("Google identity: email_verified True honoured", identity.email_verified is True)

        FakeAsyncClient._routes = {
            "openidconnect.googleapis.com/v1/userinfo": {"sub": "google-sub-456", "email": "unverified@example.com", "email_verified": False, "name": "Nobody"}
        }
        identity2 = await google._fetch_identity("fake-access-token")
        check("Google identity: email_verified False honoured", identity2.email_verified is False)

        microsoft = MicrosoftLoginProvider()
        FakeAsyncClient._routes = {
            "graph.microsoft.com/v1.0/me": {"id": "ms-id-123", "mail": "Work@Company.com", "userPrincipalName": "work@company.onmicrosoft.com", "displayName": "Work Person"}
        }
        ms_identity = await microsoft._fetch_identity("fake-access-token")
        check("Microsoft identity: prefers `mail` over userPrincipalName", ms_identity.email == "work@company.com")
        check("Microsoft identity: treated as verified by construction", ms_identity.email_verified is True)

        FakeAsyncClient._routes = {
            "graph.microsoft.com/v1.0/me": {"id": "ms-id-456", "mail": None, "userPrincipalName": "fallback@company.onmicrosoft.com", "displayName": "Fallback Person"}
        }
        ms_identity2 = await microsoft._fetch_identity("fake-access-token")
        check("Microsoft identity: falls back to userPrincipalName when mail is absent", ms_identity2.email == "fallback@company.onmicrosoft.com")

        FakeAsyncClient._routes = {}
        token_payload = {"access_token": "exchanged-token"}
        FakeAsyncClient._routes = {
            "oauth2.googleapis.com/token": token_payload,
            "openidconnect.googleapis.com/v1/userinfo": {"sub": "full-flow-sub", "email": "fullflow@example.com", "email_verified": True, "name": "Full Flow"},
        }
        full = await google.exchange_code_for_identity(code="fake-code", redirect_uri="http://localhost/callback")
        check("exchange_code_for_identity runs the full code->identity flow", full.subject == "full-flow-sub" and full.email == "fullflow@example.com")
    finally:
        httpx.AsyncClient = original_client
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_login_identity_providers() -> None:
    import asyncio

    asyncio.run(_run_fetch_identity_tests())


def main() -> int:
    init_engine()
    test_login_state_token()
    test_provision_or_link()
    test_authenticate_sso_only_account()
    test_login_identity_providers()
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
