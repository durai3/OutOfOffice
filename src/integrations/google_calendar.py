"""
Google Calendar: read-only, to surface which upcoming meetings look
delegable.

Scoped deliberately narrow for this pass: OAuth connect plus an on-demand
"upcoming meetings" list the console can show, flagged by a simple keyword
match against the title. The spec's other ask for this connector - an
automatic briefing sent five minutes before a flagged meeting starts - needs
a real time-triggered scheduler, which nothing in this codebase has yet;
that is a separate, larger piece of work and is not implemented here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.db.models import ConnectorType
from src.integrations.base import AuthMode, ConnectorMeta
from src.integrations.oauth import OAuthConnector, OAuthProviderConfig

logger = logging.getLogger("deleg8.integrations.google_calendar")

CALENDAR_API = "https://www.googleapis.com/calendar/v3"

DELEGABLE_KEYWORDS: tuple[str, ...] = (
    "standup", "stand-up", "daily sync", "status update", "status sync",
    "planning", "sprint planning", "retro", "retrospective", "check-in", "check in",
)
"""Meeting-title keywords the spec names as typically safe to delegate."""


class GoogleCalendarConnector(OAuthConnector):
    """Lists upcoming events and flags the ones that look delegable by title."""

    meta = ConnectorMeta(
        connector_type=ConnectorType.GOOGLE_CALENDAR.value,
        label="Google Calendar",
        auth_mode=AuthMode.OAUTH,
        available=True,
        unavailable_reason="requires GOOGLE_CALENDAR_CLIENT_ID / GOOGLE_CALENDAR_CLIENT_SECRET configured on this server",
        docs_url="https://console.cloud.google.com/apis/credentials",
    )
    provider = OAuthProviderConfig(
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scope="https://www.googleapis.com/auth/calendar.readonly",
        client_id_env="GOOGLE_CALENDAR_CLIENT_ID",
        client_secret_env="GOOGLE_CALENDAR_CLIENT_SECRET",
        extra_authorize_params={"access_type": "offline", "prompt": "consent"},
    )

    async def list_upcoming_events(self, token: str, *, max_results: int = 15) -> list[dict[str, Any]]:
        """This account's next ``max_results`` calendar events, flagged for delegability by title."""
        now = datetime.now(timezone.utc)
        params = {
            "timeMin": now.isoformat(),
            "timeMax": (now + timedelta(days=7)).isoformat(),
            "maxResults": str(max_results),
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{CALENDAR_API}/calendars/primary/events",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code != 200:
            logger.warning("Google Calendar list failed: HTTP %s %s", response.status_code, response.text[:200])
            return []
        events = response.json().get("items", [])
        return [
            {
                "id": event.get("id"),
                "summary": event.get("summary", "(no title)"),
                "start": (event.get("start") or {}).get("dateTime") or (event.get("start") or {}).get("date"),
                "html_link": event.get("htmlLink"),
                "delegable": _looks_delegable(event.get("summary", "")),
            }
            for event in events
        ]


def _looks_delegable(title: str) -> bool:
    lowered = title.lower()
    return any(keyword in lowered for keyword in DELEGABLE_KEYWORDS)
