"""
FastAPI dependencies that turn a bearer token into a tenant context.

:class:`TenantContext` is what every authenticated route receives. It carries
the user row, their principal, and - crucially - the
:class:`~src.tenancy.workspace.Workspace` holding *that user's* components:
their knowledge index, their escalation channel, their memory registry, their
meeting sessions. A route that only ever touches ``ctx.workspace`` cannot
accidentally read another tenant's data, because there is no path from one
workspace to another.

``require_onboarded`` additionally refuses requests from accounts that have not
accepted the current legal documents. That check lives here rather than in
each route because "the delegate joined a meeting for someone who never
accepted the terms" is exactly the failure this must not have.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, WebSocket, status
from sqlalchemy.orm import Session

from src.auth.security import AuthError, decode_access_token, hash_token
from src.compliance import legal
from src.db import repo
from src.db.base import db_session, session_scope
from src.db.models import Meeting, Organization, OrganizationRole, Principal, User

logger = logging.getLogger("deleg8.auth.deps")

CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


@dataclass
class TenantContext:
    """Everything a request needs about who is asking and what they own."""

    user: User
    principal: Principal | None
    session: Session
    workspace: Any
    """The caller's :class:`~src.tenancy.workspace.Workspace`."""
    org: Organization | None = None
    org_role: str | None = None
    """The caller's role within :attr:`org` (owner/admin/member); ``None`` when solo."""

    @property
    def user_id(self) -> uuid.UUID:
        """The account id every query filters on."""
        return self.user.id

    @property
    def principal_slug(self) -> str:
        """The principal id the brain uses. Falls back to the account id when unprovisioned."""
        return self.principal.slug if self.principal is not None else str(self.user.id)

    @property
    def display_name(self) -> str:
        """The represented person's name."""
        return self.principal.display_name if self.principal is not None else self.user.display_name

    @property
    def is_admin(self) -> bool:
        """True for platform administrators. Distinct from :attr:`is_org_admin` - never conflate the two."""
        return self.user.role == "admin"

    @property
    def is_org_admin(self) -> bool:
        """True for an owner or admin of :attr:`org` (not the platform role)."""
        return self.org_role in (OrganizationRole.OWNER.value, OrganizationRole.ADMIN.value)


def bearer_token(
    authorization: Annotated[str | None, Header()] = None,
    x_deleg8_token: Annotated[str | None, Header()] = None,
) -> str:
    """Extract the bearer token from ``Authorization`` (or ``X-Deleg8-Token`` for the CLI)."""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    if x_deleg8_token:
        return x_deleg8_token.strip()
    raise CREDENTIALS_EXCEPTION


def token_from_websocket(websocket: WebSocket) -> str:
    """The bearer token on a WebSocket handshake.

    Browsers cannot set headers on a WebSocket, so the console passes the token
    as a query parameter; a header is still accepted for non-browser clients.
    """
    header = websocket.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    token = websocket.query_params.get("token", "").strip()
    if token:
        return token
    raise CREDENTIALS_EXCEPTION


def _load_user(session: Session, token: str) -> User:
    try:
        payload = decode_access_token(token)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc), headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    try:
        user_id = uuid.UUID(str(payload.get("sub")))
    except (ValueError, TypeError) as exc:
        raise CREDENTIALS_EXCEPTION from exc
    user = repo.get_user(session, user_id)
    if user is None:
        raise CREDENTIALS_EXCEPTION
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this account is suspended")
    return user


def _load_org(session: Session, user_id: uuid.UUID) -> tuple[Organization | None, str | None]:
    """The caller's organization and their role in it, or ``(None, None)`` when solo."""
    member = repo.get_organization_member(session, user_id)
    if member is None:
        return None, None
    return repo.get_organization(session, member.org_id), member.role


def get_context(
    request: Request,
    token: Annotated[str, Depends(bearer_token)],
    session: Annotated[Session, Depends(db_session)],
) -> TenantContext:
    """The authenticated tenant context for this request."""
    user = _load_user(session, token)
    principal = repo.primary_principal(session, user.id)
    workspaces = getattr(request.app.state, "workspaces", None)
    workspace = workspaces.get(user.id) if workspaces is not None else None
    org, org_role = _load_org(session, user.id)
    return TenantContext(
        user=user, principal=principal, session=session, workspace=workspace, org=org, org_role=org_role
    )


async def get_context_async(
    request: Request,
    token: Annotated[str, Depends(bearer_token)],
    session: Annotated[Session, Depends(db_session)],
) -> TenantContext:
    """:func:`get_context`, but the workspace is opened (and its index loaded) if needed.

    Routes that actually use the workspace depend on this one; routes that only
    read the database use :func:`get_context` and never pay for opening an index.
    """
    user = _load_user(session, token)
    principal = repo.primary_principal(session, user.id)
    workspaces = request.app.state.workspaces
    workspace = await workspaces.open(user.id)
    org, org_role = _load_org(session, user.id)
    return TenantContext(
        user=user, principal=principal, session=session, workspace=workspace, org=org, org_role=org_role
    )


def require_onboarded(
    context: Annotated[TenantContext, Depends(get_context_async)],
) -> TenantContext:
    """Reject requests from accounts with outstanding legal acceptance."""
    outstanding = legal.outstanding_documents(context.session, context.user_id)
    if outstanding:
        raise HTTPException(
            status.HTTP_451_UNAVAILABLE_FOR_LEGAL_REASONS,
            f"accept the updated {', '.join(outstanding)} before continuing",
        )
    return context


def require_admin(context: Annotated[TenantContext, Depends(get_context)]) -> TenantContext:
    """Reject requests from non-administrators."""
    if not context.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "administrator access required")
    return context


def require_org_admin(context: Annotated[TenantContext, Depends(get_context)]) -> TenantContext:
    """Reject requests from anyone not an owner or admin of their organization.

    Distinct from :func:`require_admin`, which gates the platform console -
    these protect different things and must never be conflated.
    """
    if context.org is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you are not a member of an organization")
    if not context.is_org_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "organization owner or admin access required")
    return context


async def resolve_websocket_context(websocket: WebSocket) -> tuple[User, Any] | None:
    """Authenticate a WebSocket handshake. Returns ``(user, workspace)`` or ``None``.

    Returning ``None`` rather than raising lets the caller close the socket with
    a policy-violation code, which is what a browser client can actually see.
    """
    try:
        token = token_from_websocket(websocket)
    except HTTPException:
        return None
    try:
        payload = decode_access_token(token)
        user_id = uuid.UUID(str(payload.get("sub")))
    except (AuthError, ValueError, TypeError):
        return None
    with session_scope() as session:
        user = repo.get_user(session, user_id)
        if user is None or not user.is_active:
            return None
        session.expunge(user)
    workspace = await websocket.app.state.workspaces.open(user_id)
    return user, workspace


@dataclass
class JobContext:
    """A worker container calling home, authenticated by its one-time job token."""

    meeting: Meeting
    user: User
    token: str


def get_job_context(
    token: Annotated[str, Depends(bearer_token)],
    session: Annotated[Session, Depends(db_session)],
) -> JobContext:
    """Authenticate a meeting worker by the job token it was launched with.

    A job token authorises exactly one meeting, for exactly one user, and is
    dead the moment that meeting reaches a terminal state - so a container that
    outlives its meeting cannot post events against it.
    """
    meeting = repo.get_meeting_by_job_token(session, hash_token(token))
    if meeting is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown or expired job token")
    user = repo.get_user(session, meeting.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "the account this job belongs to is not active")
    return JobContext(meeting=meeting, user=user, token=token)


CurrentContext = Annotated[TenantContext, Depends(get_context)]
ActiveContext = Annotated[TenantContext, Depends(get_context_async)]
OnboardedContext = Annotated[TenantContext, Depends(require_onboarded)]
AdminContext = Annotated[TenantContext, Depends(require_admin)]
OrgAdminContext = Annotated[TenantContext, Depends(require_org_admin)]
WorkerContext = Annotated[JobContext, Depends(get_job_context)]
