"""
``/api/v1/auth`` - signup, sign-in, token refresh, account maintenance.

    POST   /api/v1/auth/signup      create an account (installs a principal, policy, legal record)
    POST   /api/v1/auth/login       exchange credentials for a token pair
    POST   /api/v1/auth/refresh     rotate the refresh token, get a new access token
    POST   /api/v1/auth/logout      revoke one refresh token
    GET    /api/v1/auth/me          the signed-in account
    PATCH  /api/v1/auth/me          rename the account or its principal
    POST   /api/v1/auth/password    change the password (revokes every session)
    DELETE /api/v1/auth/me          erase the account and everything it owns
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
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
from src.auth.security import AuthError
from src.auth.service import (
    CredentialsError,
    SignupError,
    account_response,
    authenticate,
    change_password,
    delete_account,
    issue_tokens,
    provision_account,
    rotate_refresh_token,
    sign_out,
)
from src.brain.policy_templates import TEMPLATES
from src.db.base import CommittingRoute, db_session

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
