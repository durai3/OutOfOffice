"""
``/api/v1/auth`` - signup, sign-in, token refresh, account maintenance.

    POST   /api/v1/auth/signup                     create an account (installs a principal, policy, legal record)
    POST   /api/v1/auth/demo                       one-click throwaway account for evaluators (DELEG8_DEMO_ENABLED)
    POST   /api/v1/auth/login                       exchange credentials for a token pair
    GET    /api/v1/auth/oauth/{provider}/start      begin "Continue with Google/Microsoft"; returns the consent URL
    GET    /api/v1/auth/oauth/{provider}/callback   the provider redirects the browser here; not session-authed
    POST   /api/v1/auth/refresh                     rotate the refresh token, get a new access token
    POST   /api/v1/auth/logout                      revoke one refresh token
    GET    /api/v1/auth/me                          the signed-in account
    PATCH  /api/v1/auth/me                          rename the account or its principal
    POST   /api/v1/auth/password                    change the password (revokes every session)
    DELETE /api/v1/auth/me                          erase the account and everything it owns

The OAuth callback is a plain browser redirect from the provider with no
``Authorization`` header, so unlike every other route here it cannot use
:data:`~src.auth.deps.CurrentContext`. It trusts only the signed ``state``
token minted by ``oauth/start`` (:func:`src.auth.security.create_oauth_login_state_token`)
- see that function's docstring for why it is deliberately not the same
token type as a connector's state token or a real session token.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.auth.deps import CurrentContext, TenantContext, get_context
from src.auth.models import (
    AccountResponse,
    LoginRequest,
    MessageResponse,
    PasswordChangeRequest,
    RefreshRequest,
    SignupRequest,
    TokenResponse,
)
from src.auth.oauth_identity import OAuthLoginError, get_login_provider
from src.auth.security import (
    AuthError,
    create_oauth_login_state_token,
    decode_oauth_login_state_token,
)
from src.auth.service import (
    AccountDisabledError,
    CredentialsError,
    OAuthLinkError,
    SignupError,
    account_response,
    authenticate,
    change_password,
    delete_account,
    issue_tokens,
    provision_account,
    provision_demo_account,
    provision_or_link_oauth_account,
    rotate_refresh_token,
    sign_out,
)
from src.brain.policy_templates import TEMPLATES
from src.db.base import CommittingRoute, db_session, run_db

logger = logging.getLogger("deleg8.auth.routes")

router = APIRouter(prefix="/api/v1/auth", tags=["auth"], route_class=CommittingRoute)


class ErrorResponse(BaseModel):
    """Uniform error body."""

    detail: str


class ProfileUpdate(BaseModel):
    """``PATCH /api/v1/auth/me``."""

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    principal_name: str | None = Field(
        default=None, min_length=1, max_length=200, description="The name the delegate uses in meetings."
    )
    aliases: list[str] | None = Field(
        default=None, description="Other names the principal is addressed by in meetings."
    )


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


@router.post(
    "/signup",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def signup(
    body: SignupRequest,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
) -> TokenResponse:
    """Create an account: user, principal, starter policy, and the legal record."""
    if body.policy_template not in TEMPLATES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown policy template {body.policy_template!r}; choose one of {', '.join(TEMPLATES)}",
        )
    try:
        user = provision_account(
            session,
            email=str(body.email),
            password=body.password,
            display_name=body.display_name,
            policy_template=body.policy_template,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            invite_token=body.invite_token,
        )
    except SignupError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    access, refresh, expires = issue_tokens(session, user, user_agent=request.headers.get("user-agent"))
    logger.info("new account: %s", user.email)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires,
        account=account_response(session, user),
    )


_DEMO_HITS: dict[str, list[float]] = {}
"""Recent demo-account creations per client address, for a crude hourly cap."""


def _demo_enabled() -> bool:
    return os.environ.get("DELEG8_DEMO_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def _demo_allowed(ip: str | None) -> bool:
    """At most ``DELEG8_DEMO_PER_IP_HOUR`` (default 30) guest accounts per address per hour.

    In-memory and per process, which is the right size for this: it stops a
    loop from filling the database, not a determined attacker, and a restart
    forgiving everyone is harmless.
    """
    import time

    limit = int(os.environ.get("DELEG8_DEMO_PER_IP_HOUR", "30") or 30)
    now = time.monotonic()
    key = ip or "unknown"
    hits = [t for t in _DEMO_HITS.get(key, []) if now - t < 3600]
    if len(hits) >= limit:
        _DEMO_HITS[key] = hits
        return False
    hits.append(now)
    _DEMO_HITS[key] = hits
    if len(_DEMO_HITS) > 5000:  # forget stale addresses
        for stale in [k for k, v in _DEMO_HITS.items() if not v or now - v[-1] > 3600]:
            _DEMO_HITS.pop(stale, None)
    return True


@router.post(
    "/demo",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    responses={403: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
def demo_account(request: Request, session: Annotated[Session, Depends(db_session)]) -> TokenResponse:
    """Create a throwaway, fully working account and sign it in. For people evaluating the product.

    Switch off with ``DELEG8_DEMO_ENABLED=0``. Ordinary signup is unchanged.
    """
    if not _demo_enabled():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "instant demo accounts are switched off on this deployment")
    ip = _client_ip(request)
    if not _demo_allowed(ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many demo accounts from this address; try again later")
    user = provision_demo_account(session, ip_address=ip, user_agent=request.headers.get("user-agent"))
    access, refresh, expires = issue_tokens(session, user, user_agent=request.headers.get("user-agent"))
    logger.info("demo account: %s", user.email)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires,
        account=account_response(session, user),
    )


@router.post("/login", response_model=TokenResponse, responses={401: {"model": ErrorResponse}})
def login(
    body: LoginRequest,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
) -> TokenResponse:
    """Exchange email and password for an access / refresh pair."""
    try:
        user = authenticate(session, email=str(body.email), password=body.password)
    except CredentialsError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    except AuthError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    access, refresh, expires = issue_tokens(session, user, user_agent=request.headers.get("user-agent"))
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires,
        account=account_response(session, user),
    )


def _base_url() -> str:
    """This server's own externally-reachable URL - identical convention to src/api/integrations.py."""
    return os.getenv("DELEG8_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def _login_redirect_uri(provider: str) -> str:
    return f"{_base_url()}/api/v1/auth/oauth/{provider}/callback"


class OAuthLoginStartResponse(BaseModel):
    """Where to send the browser to begin a "Continue with Google/Microsoft" login."""

    redirect_url: str


@router.get("/oauth/{provider}/start", response_model=OAuthLoginStartResponse)
def oauth_login_start(
    provider: str,
    origin: str = Query(..., description="window.location.origin of the tab that opened the popup."),
    accept_terms: bool = Query(False, description="Set true only from the signup page's accepted checkbox."),
) -> OAuthLoginStartResponse:
    """Begin an SSO login or signup. Public - there is no session yet, unlike the connector version."""
    identity_provider = get_login_provider(provider)
    if identity_provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown SSO provider {provider!r}")
    if not identity_provider.is_configured():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{provider.title()} sign-in is not configured on this server yet")
    state = create_oauth_login_state_token(provider=provider, frontend_origin=origin, accept_terms=accept_terms)
    url = identity_provider.build_authorize_url(redirect_uri=_login_redirect_uri(provider), state=state)
    return OAuthLoginStartResponse(redirect_url=url)


def _login_result_page(*, ok: bool, origin: str, message: str = "", tokens: TokenResponse | None = None) -> HTMLResponse:
    """Hand the login result - including a real token pair on success - to ``window.opener``, then close.

    Exactly the popup+postMessage+close mechanic of src/api/integrations.py's
    ``_oauth_result_page``, except this one must carry a full session, since
    there is no prior authenticated request to attach anything to instead.
    """
    safe_origin = origin if origin.startswith(("http://", "https://")) else "*"
    payload: dict[str, Any] = {"source": "deleg8-oauth-login", "ok": ok, "message": message}
    if ok and tokens is not None:
        payload["tokens"] = tokens.model_dump(mode="json")
    status_line = "Signed in. You can close this window." if ok else f"Could not sign in: {message}"
    body = f"""<!doctype html><html><body style="font-family:sans-serif;padding:2rem;color:#1a1a1a">
<p>{status_line}</p>
<script>
  if (window.opener) {{ window.opener.postMessage({json.dumps(payload)}, {json.dumps(safe_origin)}); }}
  window.close();
</script>
</body></html>"""
    return HTMLResponse(body, status_code=200 if ok else 400)


@router.get("/oauth/{provider}/callback", response_class=HTMLResponse, include_in_schema=False)
async def oauth_login_callback(
    provider: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> HTMLResponse:
    """The provider redirects the browser here after consent. Trusts only the signed ``state``."""
    identity_provider = get_login_provider(provider)
    if identity_provider is None:
        return _login_result_page(ok=False, origin="*", message="unknown provider")
    if state is None:
        return _login_result_page(ok=False, origin="*", message="missing state")
    try:
        claims = decode_oauth_login_state_token(state)
    except AuthError as exc:
        return _login_result_page(ok=False, origin="*", message=str(exc))
    origin = claims.get("origin", "*")
    if claims.get("provider") != provider:
        return _login_result_page(ok=False, origin=origin, message="state mismatch")
    if error or not code:
        return _login_result_page(
            ok=False, origin=origin, message=error_description or error or "sign-in was cancelled"
        )
    try:
        identity = await identity_provider.exchange_code_for_identity(
            code=code, redirect_uri=_login_redirect_uri(provider)
        )
    except OAuthLoginError as exc:
        return _login_result_page(ok=False, origin=origin, message=str(exc))

    ip_address = _client_ip(request)
    user_agent = request.headers.get("user-agent")
    accept_terms = bool(claims.get("accept_terms", False))

    def _resolve(session: Session) -> tuple[TokenResponse, bool, str]:
        user, created = provision_or_link_oauth_account(
            session,
            provider=provider,
            provider_subject=identity.subject,
            email=identity.email,
            email_verified=identity.email_verified,
            display_name=identity.display_name or identity.email.split("@")[0],
            accept_terms=accept_terms,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        access, refresh, expires = issue_tokens(session, user, user_agent=user_agent)
        tokens = TokenResponse(
            access_token=access, refresh_token=refresh, expires_at=expires, account=account_response(session, user)
        )
        return tokens, created, user.email

    try:
        tokens, created, email = await run_db(_resolve)
    except (OAuthLinkError, AccountDisabledError) as exc:
        return _login_result_page(ok=False, origin=origin, message=str(exc))
    logger.info("%s account %s via %s SSO", "new" if created else "existing", email, provider)
    return _login_result_page(ok=True, origin=origin, tokens=tokens)


@router.post("/refresh", response_model=TokenResponse, responses={401: {"model": ErrorResponse}})
def refresh(
    body: RefreshRequest,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
) -> TokenResponse:
    """Rotate the refresh token and issue a new access token."""
    try:
        user, access, new_refresh, expires = rotate_refresh_token(
            session, body.refresh_token, user_agent=request.headers.get("user-agent")
        )
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    return TokenResponse(
        access_token=access,
        refresh_token=new_refresh,
        expires_at=expires,
        account=account_response(session, user),
    )


@router.post("/logout", response_model=MessageResponse)
def logout(body: RefreshRequest | None, session: Annotated[Session, Depends(db_session)]) -> MessageResponse:
    """Revoke one refresh token. Always succeeds, so a stale client can still sign out."""
    revoked = sign_out(session, body.refresh_token if body else None)
    return MessageResponse(detail="signed out" if revoked else "no active session for that token")


@router.get("/me", response_model=AccountResponse)
def me(context: CurrentContext) -> AccountResponse:
    """The signed-in account, its principal, and what onboarding still needs."""
    return account_response(context.session, context.user)


@router.patch("/me", response_model=AccountResponse)
def update_me(body: ProfileUpdate, context: CurrentContext) -> AccountResponse:
    """Rename the account or the principal the delegate represents."""
    session, user = context.session, context.user
    if body.display_name:
        user.display_name = body.display_name.strip()
        session.add(user)
    principal = context.principal
    if principal is not None and (body.principal_name or body.aliases is not None):
        if body.principal_name:
            principal.display_name = body.principal_name.strip()
        if body.aliases is not None:
            principal.aliases = [a.strip() for a in body.aliases if a.strip()]
        session.add(principal)
        session.flush()
        if context.workspace is not None:
            context.workspace.invalidate_profile(context.session)
    return account_response(session, user)


@router.post("/password", response_model=MessageResponse, responses={400: {"model": ErrorResponse}})
def update_password(
    body: PasswordChangeRequest,
    context: Annotated[TenantContext, Depends(get_context)],
) -> MessageResponse:
    """Change the password. Every existing session is revoked, including this one's refresh token."""
    try:
        revoked = change_password(
            context.session, context.user, current=body.current_password, new=body.new_password
        )
    except CredentialsError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return MessageResponse(detail=f"password changed; {revoked} session(s) revoked. Sign in again.")


@router.delete("/me", response_model=MessageResponse)
async def delete_me(request: Request, context: CurrentContext) -> MessageResponse:
    """Erase this account: meetings, briefs, documents, commitments, keys, everything.

    The vector index and uploaded files are removed first, because those live
    outside the database and would otherwise be orphaned by the cascade.
    """
    user_id = context.user_id
    email = context.user.email
    workspaces = request.app.state.workspaces
    await workspaces.purge(user_id)
    delete_account(context.session, user_id)
    logger.warning("account deleted: %s", email)
    return MessageResponse(detail="account and all associated data deleted")
