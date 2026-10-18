"""Calendar OAuth providers: Google Calendar and Microsoft Outlook Calendar.

Deliberately NOT a :class:`src.integrations.oauth.OAuthConnector`, for the
same reason :mod:`src.auth.oauth_identity` is not one: those connectors (and
the registry they live in) assume one row per ``(user, connector_type)`` in
:class:`~src.db.models.Integration`, but a user may connect several mailboxes
of the same provider - a work and a personal Google account, say - and each
is discovered only once the token is exchanged and the owning mailbox's
address is known. This reuses only the generic Authorization Code mechanics
(:mod:`src.integrations.oauth`) every OAuth flow in this codebase is built on,
plus one extra step neither a connector nor a login provider needs: listing
the mailbox's own upcoming events and picking out which ones have a joinable
video link - the only kind this product can send a delegate to.

Google Calendar reuses the same ``GOOGLE_CALENDAR_CLIENT_ID`` / ``_SECRET``
app as :class:`src.integrations.google_calendar.GoogleCalendarConnector`
(same Google Cloud project, just a couple more scopes). Microsoft gets its
own app, distinct from both the Google Calendar app and the "Continue with
Microsoft" sign-in app in :mod:`src.auth.oauth_identity`, because its scopes
differ from both.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.integrations.oauth import (
    build_oauth_authorize_url,
    expiry_config_from_payload,
    post_oauth_token_request,
    refresh_oauth_token_if_needed,
)

logger = logging.getLogger("deleg8.calendar.providers")


class CalendarOAuthError(RuntimeError):
    """A calendar connect/sync step failed, or the provider is not configured."""


@dataclass(frozen=True)
class RawEvent:
    """One upcoming event, normalised across providers."""

    provider_event_id: str
    title: str
    join_url: str
    """The video-call link, or empty when this event has none and cannot be delegated."""
    html_link: str
    start_at: datetime
    end_at: datetime | None


class CalendarOAuthProvider:
    """Base for one provider's "connect a mailbox for calendar sync" flow."""

    provider: str
    authorize_url: str
    token_url: str
    scope: str
    client_id_env: str
    client_secret_env: str
    extra_authorize_params: dict[str, str] = {}

    def client_credentials(self) -> tuple[str, str] | None:
        """``(client_id, client_secret)`` from the operator's environment, or ``None`` if not configured."""
        client_id = os.getenv(self.client_id_env, "").strip()
        client_secret = os.getenv(self.client_secret_env, "").strip()
        return (client_id, client_secret) if client_id and client_secret else None

    def is_configured(self) -> bool:
        """True once the operator has registered an app for this provider."""
        return self.client_credentials() is not None

    def build_authorize_url(self, *, redirect_uri: str, state: str) -> str:
        """The URL to send the browser to for this provider's consent screen."""
        creds = self.client_credentials()
        if creds is None:
            raise CalendarOAuthError(f"{self.provider.title()} Calendar is not configured on this server yet")
        client_id, _secret = creds
        return build_oauth_authorize_url(
            authorize_url=self.authorize_url,
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=self.scope,
            state=state,
            extra_params=self.extra_authorize_params,
        )

    async def exchange_code(self, *, code: str, redirect_uri: str) -> tuple[str, dict[str, Any]]:
        """Exchange an authorization code for an access token. Returns ``(access_token, config)``."""
        creds = self.client_credentials()
        if creds is None:
            raise CalendarOAuthError(f"{self.provider.title()} Calendar is not configured on this server yet")
        client_id, client_secret = creds
        payload = await post_oauth_token_request(
            token_url=self.token_url,
            client_id=client_id,
            client_secret=client_secret,
            auth_style="body",
            extra={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
            label=f"{self.provider.title()} Calendar",
        )
        token = payload.get("access_token")
        if not token:
            raise CalendarOAuthError(f"{self.provider.title()} did not return an access token")
        return token, expiry_config_from_payload(payload)

    async def refresh_if_needed(self, token: str, config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Refresh the access token if it is expired or about to be. No-op when there is nothing to refresh."""
        creds = self.client_credentials()
        if creds is None:
            raise CalendarOAuthError(f"{self.provider.title()} Calendar is not configured on this server yet")
        client_id, client_secret = creds
        return await refresh_oauth_token_if_needed(
            token, config,
            token_url=self.token_url,
            client_id=client_id,
            client_secret=client_secret,
            auth_style="body",
            label=f"{self.provider.title()} Calendar",
        )

    async def fetch_account_email(self, access_token: str) -> str:
        """The address of the mailbox this token belongs to."""
        raise NotImplementedError

    async def list_upcoming_events(self, access_token: str, *, days_ahead: int = 7) -> list[RawEvent]:
        """This mailbox's events over the next ``days_ahead`` days."""
        raise NotImplementedError


class GoogleCalendarProvider(CalendarOAuthProvider):
    """``calendar.readonly`` plus ``openid email`` so the connected mailbox's address is known."""

    provider = "google"
    authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    scope = "openid email https://www.googleapis.com/auth/calendar.readonly"
    client_id_env = "GOOGLE_CALENDAR_CLIENT_ID"
    client_secret_env = "GOOGLE_CALENDAR_CLIENT_SECRET"
    extra_authorize_params = {"access_type": "offline", "prompt": "consent"}
    _userinfo_url = "https://openidconnect.googleapis.com/v1/userinfo"
    _events_url = "https://www.googleapis.com/calendar/v3/calendars/primary/events"

    async def fetch_account_email(self, access_token: str) -> str:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(self._userinfo_url, headers={"Authorization": f"Bearer {access_token}"})
        if response.status_code >= 400:
            raise CalendarOAuthError(f"Google rejected the userinfo request (HTTP {response.status_code})")
        email = str(response.json().get("email", "")).strip().lower()
        if not email:
            raise CalendarOAuthError("Google did not report an email address for this account")
        return email

    async def list_upcoming_events(self, access_token: str, *, days_ahead: int = 7) -> list[RawEvent]:
        now = datetime.now(timezone.utc)
        params = {
            "timeMin": now.isoformat(),
            "timeMax": (now + timedelta(days=days_ahead)).isoformat(),
            "maxResults": "50",
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                self._events_url, params=params, headers={"Authorization": f"Bearer {access_token}"}
            )
        if response.status_code != 200:
            raise CalendarOAuthError(f"Google Calendar list failed (HTTP {response.status_code}): {response.text[:200]}")
        events = response.json().get("items", [])
        return [raw for raw in (_from_google_event(event) for event in events) if raw is not None]


class MicrosoftCalendarProvider(CalendarOAuthProvider):
    """``Calendars.Read`` over the Microsoft Graph ``/me/calendarView``.

    ``common`` so both personal and work/school Microsoft accounts can
    connect - the same reasoning as :class:`src.auth.oauth_identity.MicrosoftLoginProvider`.
    """

    provider = "microsoft"
    authorize_url = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    scope = "openid email offline_access Calendars.Read"
    client_id_env = "MICROSOFT_CALENDAR_CLIENT_ID"
    client_secret_env = "MICROSOFT_CALENDAR_CLIENT_SECRET"
    _graph_me_url = "https://graph.microsoft.com/v1.0/me"
    _calendar_view_url = "https://graph.microsoft.com/v1.0/me/calendarView"

    async def fetch_account_email(self, access_token: str) -> str:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(self._graph_me_url, headers={"Authorization": f"Bearer {access_token}"})
        if response.status_code >= 400:
            raise CalendarOAuthError(f"Microsoft rejected the Graph /me request (HTTP {response.status_code})")
        data = response.json()
        email = str(data.get("mail") or data.get("userPrincipalName") or "").strip().lower()
        if not email:
            raise CalendarOAuthError("Microsoft did not report a mailbox address for this account")
        return email

    async def list_upcoming_events(self, access_token: str, *, days_ahead: int = 7) -> list[RawEvent]:
        now = datetime.now(timezone.utc)
        params = {
            "startDateTime": now.isoformat(),
            "endDateTime": (now + timedelta(days=days_ahead)).isoformat(),
            "$orderby": "start/dateTime",
            "$top": "50",
        }
        headers = {"Authorization": f"Bearer {access_token}", "Prefer": 'outlook.timezone="UTC"'}
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(self._calendar_view_url, params=params, headers=headers)
        if response.status_code != 200:
            raise CalendarOAuthError(f"Outlook Calendar list failed (HTTP {response.status_code}): {response.text[:200]}")
        events = response.json().get("value", [])
        return [raw for raw in (_from_microsoft_event(event) for event in events) if raw is not None]


def _parse_google_datetime(value: dict[str, Any] | None) -> datetime | None:
    if not value:
        return None
    raw = value.get("dateTime") or value.get("date")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _from_google_event(event: dict[str, Any]) -> RawEvent | None:
    start_at = _parse_google_datetime(event.get("start"))
    if start_at is None or event.get("status") == "cancelled":
        return None
    join_url = event.get("hangoutLink") or ""
    if not join_url:
        for entry_point in ((event.get("conferenceData") or {}).get("entryPoints") or []):
            if entry_point.get("entryPointType") == "video" and entry_point.get("uri"):
                join_url = entry_point["uri"]
                break
    return RawEvent(
        provider_event_id=str(event.get("id") or ""),
        title=event.get("summary", "(no title)"),
        join_url=join_url,
        html_link=event.get("htmlLink", ""),
        start_at=start_at,
        end_at=_parse_google_datetime(event.get("end")),
    )


def _parse_microsoft_datetime(value: dict[str, Any] | None) -> datetime | None:
    if not value:
        return None
    raw = value.get("dateTime")
    if not raw:
        return None
    try:
        # Graph returns a naive timestamp in whatever zone the `Prefer` header
        # requested (UTC here), with no offset - unlike Google's RFC 3339.
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _from_microsoft_event(event: dict[str, Any]) -> RawEvent | None:
    start_at = _parse_microsoft_datetime(event.get("start"))
    if start_at is None or event.get("isCancelled"):
        return None
    online_meeting = event.get("onlineMeeting") or {}
    return RawEvent(
        provider_event_id=str(event.get("id") or ""),
        title=event.get("subject", "(no title)"),
        join_url=online_meeting.get("joinUrl") or "",
        html_link=event.get("webLink", ""),
        start_at=start_at,
        end_at=_parse_microsoft_datetime(event.get("end")),
    )


_PROVIDERS: dict[str, CalendarOAuthProvider] = {
    "google": GoogleCalendarProvider(),
    "microsoft": MicrosoftCalendarProvider(),
}


def get_calendar_provider(name: str) -> CalendarOAuthProvider | None:
    """One of the registered calendar providers, or ``None`` if unknown."""
    return _PROVIDERS.get(name)


def all_calendar_providers() -> list[CalendarOAuthProvider]:
    """Every known provider, in a stable order - configured or not."""
    return [_PROVIDERS["google"], _PROVIDERS["microsoft"]]
