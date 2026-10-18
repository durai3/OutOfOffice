"""The background loop that makes calendar auto-detection actually happen.

Three things on one timer, mirroring the pattern
:meth:`~src.tenancy.workspace.WorkspaceManager._sweep` already uses for
evicting idle workspaces - one ``asyncio`` task, started from the application
lifespan, woken on an interval, and never allowed to die from one tenant's
failure:

1. **Sync** - pull each connected mailbox's upcoming events (throttled to
   once every :data:`SYNC_INTERVAL`, not every tick) into
   :class:`~src.db.models.CalendarEvent` rows.
2. **Remind** - fifteen minutes before a delegable event starts, send one
   Telegram message, with a "Deleg8 it" button when the user has not already
   flagged it.
3. **Dispatch** - once a flagged event's start time arrives, launch the
   delegate exactly the way a manual "Join" from the console would, through
   the same :class:`~src.api.delegate.DelegateManager.launch`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from src.auth.security import AuthError, decrypt_secret, encrypt_secret
from src.calendar.providers import CalendarOAuthError, get_calendar_provider
from src.db import repo
from src.db.base import session_scope
from src.db.models import CalendarAccount
from src.escalation.hub import TelegramHub
from src.tenancy.workspace import WorkspaceManager

logger = logging.getLogger("deleg8.calendar.scheduler")

TICK_SECONDS: float = 60.0
SYNC_INTERVAL: timedelta = timedelta(minutes=5)
REMINDER_LEAD: timedelta = timedelta(minutes=15)


def _decode_config(account: CalendarAccount) -> dict:
    if not account.config_encrypted:
        return {}
    try:
        return json.loads(decrypt_secret(account.config_encrypted))
    except (AuthError, json.JSONDecodeError):
        return {}


async def sync_account(account_id: uuid.UUID) -> None:
    """Refresh one account's token if needed, then pull its upcoming events."""
    with session_scope() as session:
        account = session.get(CalendarAccount, account_id)
        if account is None or not account.auth_token_encrypted or not account.is_enabled:
            return
        provider = get_calendar_provider(account.provider)
        if provider is None:
            return
        try:
            token = decrypt_secret(account.auth_token_encrypted)
        except AuthError as exc:
            repo.upsert_calendar_account(
                session, user_id=account.user_id, provider=account.provider, email=account.email,
                last_error=f"stored credential could not be decrypted: {exc}",
            )
            return
        config = _decode_config(account)
        user_id, acct_provider, acct_email = account.user_id, account.provider, account.email

    try:
        token, config = await provider.refresh_if_needed(token, config)
    except CalendarOAuthError as exc:
        with session_scope() as session:
            repo.upsert_calendar_account(
                session, user_id=user_id, provider=acct_provider, email=acct_email, last_error=str(exc)[:2000]
            )
        logger.warning("could not refresh %s calendar token for %s: %s", acct_provider, acct_email, exc)
        return

    with session_scope() as session:
        repo.upsert_calendar_account(
            session, user_id=user_id, provider=acct_provider, email=acct_email,
            auth_token_encrypted=encrypt_secret(token),
            config_encrypted=encrypt_secret(json.dumps(config)) if config else None,
        )

    try:
        events = await provider.list_upcoming_events(token)
    except CalendarOAuthError as exc:
        with session_scope() as session:
            repo.upsert_calendar_account(
                session, user_id=user_id, provider=acct_provider, email=acct_email, last_error=str(exc)[:2000]
            )
        logger.warning("calendar sync failed for %s %s: %s", acct_provider, acct_email, exc)
        return

    with session_scope() as session:
        for raw in events:
            if not raw.provider_event_id:
                continue
            repo.upsert_calendar_event(
                session, user_id=user_id, calendar_account_id=account_id, provider_event_id=raw.provider_event_id,
                title=raw.title[:500], join_url=raw.join_url, html_link=raw.html_link,
                start_at=raw.start_at, end_at=raw.end_at,
            )
        repo.upsert_calendar_account(
            session, user_id=user_id, provider=acct_provider, email=acct_email,
            last_synced_at=datetime.now(timezone.utc), last_error=None,
        )
    logger.info("synced %d upcoming event(s) for %s %s", len(events), acct_provider, acct_email)


class CalendarScheduler:
    """Owns the one background task that drives sync, reminders and auto-dispatch."""

    def __init__(self, workspaces: WorkspaceManager, hub: TelegramHub) -> None:
        """Bind to the process-wide workspace manager and Telegram hub."""
        self._workspaces = workspaces
        self._hub = hub
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Begin ticking in the background. Idempotent."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="deleg8-calendar-scheduler")

    async def stop(self) -> None:
        """Cancel the background task. Idempotent."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - shutting down regardless
            logger.exception("calendar scheduler did not stop cleanly")
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(TICK_SECONDS)
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
                logger.exception("calendar scheduler tick failed")

    async def tick(self) -> None:
        """One pass: sync what's due, remind what's imminent, dispatch what's flagged."""
        await self._sync_due_accounts()
        await self._send_reminders()
        await self._dispatch_due()

    # ------------------------------------------------------------------ #
    # Sync
    # ------------------------------------------------------------------ #

    async def _sync_due_accounts(self) -> None:
        cutoff = datetime.now(timezone.utc) - SYNC_INTERVAL
        with session_scope() as session:
            due = [
                account.id
                for account in repo.list_enabled_calendar_accounts(session)
                if account.last_synced_at is None or account.last_synced_at <= cutoff
            ]
        for account_id in due:
            await sync_account(account_id)

    # ------------------------------------------------------------------ #
    # Reminders
    # ------------------------------------------------------------------ #

    async def _send_reminders(self) -> None:
        now = datetime.now(timezone.utc)
        window_start = now + REMINDER_LEAD - timedelta(seconds=TICK_SECONDS)
        window_end = now + REMINDER_LEAD
        with session_scope() as session:
            due = [
                (event.id, event.user_id, event.title, event.start_at, event.deleg8_requested)
                for event in repo.events_due_for_reminder(session, window_start=window_start, window_end=window_end)
            ]
            for event_id, *_rest in due:
                repo.mark_calendar_event_reminded(session, event_id)
        for event_id, user_id, title, start_at, already_requested in due:
            try:
                await self._notify(user_id, event_id=event_id, title=title, start_at=start_at, already_requested=already_requested)
            except Exception:  # noqa: BLE001 - one tenant's delivery problem must not block the rest
                logger.exception("could not send calendar reminder for event %s", event_id)

    async def _notify(
        self, user_id: uuid.UUID, *, event_id: uuid.UUID, title: str, start_at: datetime, already_requested: bool
    ) -> None:
        with session_scope() as session:
            channel = repo.get_channel(session, user_id, "telegram")
        if channel is None or not channel.verified or not channel.address:
            return
        when = start_at.astimezone().strftime("%-I:%M %p") if hasattr(start_at, "astimezone") else str(start_at)
        if already_requested:
            text = f"Reminder: “{title}” starts at {when}. Your delegate will join automatically."
            await self._send_plain(user_id, int(channel.address), channel.bot_token_encrypted is not None, text)
            return
        if channel.bot_token_encrypted is not None:
            # A user on their own bot has no inline keyboard wired through the
            # generic bridge - point them at the console instead of degrading silently.
            text = f"Reminder: “{title}” starts at {when}. Open the Calendar tab to Deleg8 it."
            await self._send_plain(user_id, int(channel.address), True, text)
            return
        await self._hub.send_calendar_reminder(int(channel.address), event_id=event_id, title=title, when=when)

    async def _send_plain(self, user_id: uuid.UUID, chat_id: int, own_bot: bool, text: str) -> None:
        if own_bot:
            workspace = await self._workspaces.open(user_id)
            await workspace.bridge.send_text(text)
        else:
            await self._hub.send_text(chat_id, text)

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #

    async def _dispatch_due(self) -> None:
        now = datetime.now(timezone.utc)
        with session_scope() as session:
            due = [
                (event.id, event.user_id, event.title, event.join_url)
                for event in repo.events_due_for_dispatch(session, now=now)
            ]
        for event_id, user_id, title, join_url in due:
            await self._dispatch(event_id, user_id, title, join_url)

    async def _dispatch(self, event_id: uuid.UUID, user_id: uuid.UUID, title: str, join_url: str) -> None:
        from src.api.delegate import JoinRequest

        meeting_row_id: uuid.UUID | None = None
        try:
            workspace = await self._workspaces.open(user_id)
            status = await workspace.delegate.launch(
                JoinRequest(url=join_url, speaker_label=(title or "Meeting")[:40], briefing=True)
            )
            if status.meeting_row_id:
                meeting_row_id = uuid.UUID(status.meeting_row_id)
            logger.info("auto-dispatched calendar event %s (%s) for %s", event_id, title, user_id)
        except Exception as exc:  # noqa: BLE001 - a dispatch failure must not stop the loop or retry forever
            logger.warning("could not auto-dispatch calendar event %s for %s: %s", event_id, user_id, exc)
        with session_scope() as session:
            repo.mark_calendar_event_dispatched(session, event_id, meeting_id=meeting_row_id)


__all__ = ["CalendarScheduler", "sync_account"]
