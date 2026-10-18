"""
``/api/v1/calendar`` - connected mailboxes, synced upcoming meetings, and the
"Deleg8 it" flag.

    GET    /api/v1/calendar/providers                 which calendar providers this server supports, and whether each is configured
    GET    /api/v1/calendar/accounts                   this account's connected mailboxes
    GET    /api/v1/calendar/accounts/{provider}/oauth/start     begin connecting one: returns the provider's consent URL
    GET    /api/v1/calendar/accounts/{provider}/oauth/callback  the provider redirects the browser here; not session-authed
    PATCH  /api/v1/calendar/accounts/{account_id}       enable/disable a connected mailbox
    POST   /api/v1/calendar/accounts/{account_id}/sync  pull this mailbox's events right now, rather than waiting for the next tick
    DELETE /api/v1/calendar/accounts/{account_id}       disconnect a mailbox
    GET    /api/v1/calendar/events                      upcoming events across every connected mailbox
    PATCH  /api/v1/calendar/events/{event_id}            set or clear "Deleg8 it" for one event

A mailbox may be connected on top of however the user signed in - this is
deliberately a separate table from :class:`~src.db.models.Integration`
(:mod:`src.api.integrations`) because a user may connect several mailboxes of
the same provider, which that one-row-per-connector-type model cannot
express. See :mod:`src.calendar.providers` for why the OAuth flow itself is
not built on :class:`~src.integrations.oauth.OAuthConnector` either.

The actual sync, the T-15 Telegram reminder, and the auto-dispatch at a
flagged event's start time all happen on a timer in
:class:`~src.calendar.scheduler.CalendarScheduler`, not here - this router
only manages the account/event rows and lets the console (or an immediate
post-connect sync) read and write them.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from src.auth.deps import CurrentContext
from src.auth.security import (
    AuthError,
    create_oauth_state_token,
    decode_oauth_state_token,
    encrypt_secret,
)
from src.calendar.providers import (
    CalendarOAuthError,
    CalendarOAuthProvider,
    all_calendar_providers,
    get_calendar_provider,
)
from src.calendar.scheduler import sync_account
from src.db import repo
from src.db.base import CommittingRoute, run_db
from src.db.models import CalendarAccount, CalendarEvent

logger = logging.getLogger("deleg8.api.calendar")

router = APIRouter(prefix="/api/v1/calendar", tags=["calendar"], route_class=CommittingRoute)

_LABELS: dict[str, str] = {"google": "Google Calendar", "microsoft": "Outlook Calendar"}


def _label(provider: str) -> str:
    return _LABELS.get(provider, provider.title())


def _base_url() -> str:
    """This server's own externally-reachable URL, for building an OAuth redirect_uri."""
    return os.getenv("DELEG8_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def _redirect_uri(provider: str) -> str:
    return f"{_base_url()}/api/v1/calendar/accounts/{provider}/oauth/callback"


def _provider_or_404(provider: str) -> CalendarOAuthProvider:
    connector = get_calendar_provider(provider)
    if connector is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown calendar provider {provider!r}")
    return connector


class CalendarProviderOut(BaseModel):
    """One provider this server supports connecting, and whether it is configured yet."""

    provider: str
    label: str
    available: bool
    unavailable_reason: str | None = None


class CalendarAccountOut(BaseModel):
    """One connected mailbox."""

    id: str
    provider: str
    email: str
    label: str
    is_enabled: bool
    last_synced_at: datetime | None = None
    last_error: str | None = None


class CalendarEventOut(BaseModel):
    """One synced upcoming meeting."""

    id: str
    calendar_account_id: str
    account_email: str
    provider: str
    title: str
    join_url: str
    html_link: str
    start_at: datetime
    end_at: datetime | None = None
    delegable: bool = Field(..., description="Whether this event has a video link the delegate can join.")
    deleg8_requested: bool
    dispatched: bool
    meeting_id: str | None = None


class OAuthStartResponse(BaseModel):
    """Where to send the browser to begin connecting a mailbox."""

    redirect_url: str


class PatchAccountRequest(BaseModel):
    """Enable or disable a connected mailbox."""

    is_enabled: bool


class PatchEventRequest(BaseModel):
    """Set or clear "Deleg8 it" for one event."""

    deleg8_requested: bool


def _account_out(account: CalendarAccount) -> CalendarAccountOut:
    return CalendarAccountOut(
        id=str(account.id),
        provider=account.provider,
        email=account.email,
        label=account.label or _label(account.provider),
        is_enabled=account.is_enabled,
        last_synced_at=account.last_synced_at,
        last_error=account.last_error,
    )


def _event_out(event: CalendarEvent, account: CalendarAccount | None) -> CalendarEventOut:
    return CalendarEventOut(
        id=str(event.id),
        calendar_account_id=str(event.calendar_account_id),
        account_email=account.email if account else "",
        provider=account.provider if account else "",
        title=event.title,
        join_url=event.join_url,
        html_link=event.html_link,
        start_at=event.start_at,
        end_at=event.end_at,
        delegable=bool(event.join_url),
        deleg8_requested=event.deleg8_requested,
        dispatched=event.dispatched_at is not None,
        meeting_id=str(event.meeting_id) if event.meeting_id else None,
    )


def _uuid_or_404(raw: str, *, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown {what}") from exc


@router.get("/providers", response_model=list[CalendarProviderOut])
def list_providers(_context: CurrentContext) -> list[CalendarProviderOut]:
    """Every calendar provider this server knows how to connect, configured or not."""
    out = []
    for connector in all_calendar_providers():
        configured = connector.is_configured()
        out.append(
            CalendarProviderOut(
                provider=connector.provider,
                label=_label(connector.provider),
                available=configured,
                unavailable_reason=(
                    None
                    if configured
                    else f"requires {connector.client_id_env} / {connector.client_secret_env} configured on this server"
                ),
            )
        )
    return out


@router.get("/accounts", response_model=list[CalendarAccountOut])
def list_accounts(context: CurrentContext) -> list[CalendarAccountOut]:
    """Every mailbox this account has connected."""
    return [_account_out(a) for a in repo.list_calendar_accounts(context.session, context.user_id)]


@router.get("/accounts/{provider}/oauth/start", response_model=OAuthStartResponse)
def oauth_start(
    provider: str,
    context: CurrentContext,
    origin: str = Query(..., description="window.location.origin of the console tab that opened the popup."),
) -> OAuthStartResponse:
    """Begin connecting a mailbox: mint a state token and return the provider's consent URL."""
    connector = _provider_or_404(provider)
    if not connector.is_configured():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{_label(provider)} is not configured on this server yet")
    state = create_oauth_state_token(context.user_id, f"calendar_{provider}", origin)
    url = connector.build_authorize_url(redirect_uri=_redirect_uri(provider), state=state)
    return OAuthStartResponse(redirect_url=url)


def _oauth_result_page(*, ok: bool, origin: str, message: str = "") -> HTMLResponse:
    """A minimal page that hands the result back to the window that opened the popup, then closes."""
    safe_origin = origin if origin.startswith(("http://", "https://")) else "*"
    payload = json.dumps({"source": "deleg8-calendar-oauth", "ok": ok, "message": message})
    status_line = "Connected. You can close this window." if ok else f"Could not connect: {message}"
    body = f"""<!doctype html><html><body style="font-family:sans-serif;padding:2rem;color:#1a1a1a">
<p>{status_line}</p>
<script>
  if (window.opener) {{ window.opener.postMessage({payload}, {json.dumps(safe_origin)}); }}
  window.close();
</script>
</body></html>"""
    return HTMLResponse(body, status_code=200 if ok else 400)


@router.get("/accounts/{provider}/oauth/callback", response_class=HTMLResponse, include_in_schema=False)
async def oauth_callback(
    provider: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> HTMLResponse:
    """The provider redirects the browser here after consent. Trusts only the signed ``state``."""
    connector = get_calendar_provider(provider)
    if connector is None:
        return _oauth_result_page(ok=False, origin="*", message="unknown provider")
    if state is None:
        return _oauth_result_page(ok=False, origin="*", message="missing state")
    try:
        claims = decode_oauth_state_token(state)
    except AuthError as exc:
        return _oauth_result_page(ok=False, origin="*", message=str(exc))
    origin = claims.get("origin", "*")
    if claims.get("connector_type") != f"calendar_{provider}":
        return _oauth_result_page(ok=False, origin=origin, message="state mismatch")
    if error or not code:
        return _oauth_result_page(ok=False, origin=origin, message=error_description or error or "denied")
    try:
        token, config = await connector.exchange_code(code=code, redirect_uri=_redirect_uri(provider))
        email = await connector.fetch_account_email(token)
    except CalendarOAuthError as exc:
        return _oauth_result_page(ok=False, origin=origin, message=str(exc))
    user_id = uuid.UUID(claims["sub"])

    def _store(session: Any) -> uuid.UUID:
        account = repo.upsert_calendar_account(
            session,
            user_id=user_id,
            provider=provider,
            email=email,
            auth_token_encrypted=encrypt_secret(token),
            config_encrypted=encrypt_secret(json.dumps(config)) if config else None,
            is_enabled=True,
            last_error=None,
        )
        return account.id

    account_id = await run_db(_store)
    logger.info("user %s connected %s calendar account %s", user_id, provider, email)
    await sync_account(account_id)
    return _oauth_result_page(ok=True, origin=origin)


@router.patch("/accounts/{account_id}", response_model=CalendarAccountOut)
def patch_account(account_id: str, body: PatchAccountRequest, context: CurrentContext) -> CalendarAccountOut:
    """Enable or disable a connected mailbox without disconnecting it."""
    account = repo.get_calendar_account(context.session, context.user_id, _uuid_or_404(account_id, what="account"))
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown account")
    account.is_enabled = body.is_enabled
    context.session.add(account)
    context.session.flush()
    return _account_out(account)


@router.post("/accounts/{account_id}/sync", response_model=CalendarAccountOut)
async def trigger_sync(account_id: str, context: CurrentContext) -> CalendarAccountOut:
    """Pull this mailbox's upcoming events right now, rather than waiting for the next tick."""
    aid = _uuid_or_404(account_id, what="account")
    account = repo.get_calendar_account(context.session, context.user_id, aid)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown account")
    await sync_account(aid)
    refreshed = repo.get_calendar_account(context.session, context.user_id, aid)
    return _account_out(refreshed or account)


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(account_id: str, context: CurrentContext) -> None:
    """Disconnect a mailbox. Its synced events are removed along with it."""
    repo.delete_calendar_account(context.session, context.user_id, _uuid_or_404(account_id, what="account"))
    logger.info("user %s disconnected calendar account %s", context.user.email, account_id)


@router.get("/events", response_model=list[CalendarEventOut])
def list_events(context: CurrentContext) -> list[CalendarEventOut]:
    """Upcoming events across every connected mailbox, soonest first."""
    accounts = {a.id: a for a in repo.list_calendar_accounts(context.session, context.user_id)}
    return [
        _event_out(event, accounts.get(event.calendar_account_id))
        for event in repo.list_upcoming_calendar_events(context.session, context.user_id)
    ]


@router.patch("/events/{event_id}", response_model=CalendarEventOut)
def patch_event(event_id: str, body: PatchEventRequest, context: CurrentContext) -> CalendarEventOut:
    """Set or clear "Deleg8 it" for one event - the same flag a Telegram reminder's button sets."""
    eid = _uuid_or_404(event_id, what="event")
    event = repo.get_calendar_event(context.session, context.user_id, eid)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown event")
    if body.deleg8_requested and not event.join_url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "this meeting has no video link to join")
    event = repo.set_calendar_event_deleg8(context.session, context.user_id, eid, requested=body.deleg8_requested)
    assert event is not None
    account = repo.get_calendar_account(context.session, context.user_id, event.calendar_account_id)
    return _event_out(event, account)
