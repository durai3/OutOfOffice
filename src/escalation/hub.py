"""
One Telegram bot, many tenants.

The single-tenant bridge polled one bot token and sent everything to one
chat id read from ``TELEGRAM_CHAT_ID`` - which is to say, to the operator's
phone. With more than one user that is not a configuration detail, it is a
data leak: every customer's escalations, transcript excerpts and briefings
would arrive in the same inbox.

:class:`TelegramHub` fixes it without asking every user to create a bot.
One :class:`~telegram.ext.Application` polls the platform token (Telegram
allows exactly one poller per token, so this *must* be a singleton), and
every inbound update is resolved to a tenant by its chat id before anything
is done with it:

    update -> chat id -> notification_channels row -> user -> that user's handlers

Each tenant then gets a :class:`TenantTelegramBridge`, which satisfies the
same :class:`~src.escalation.bridge.EscalationBridge` and
:class:`~src.escalation.bridge.MessageDispatcher` protocols the rest of the
system already speaks, but is permanently bound to that tenant's chat id.
There is no method on it that can address another chat.

A user who prefers their own bot still can: the workspace gives them a plain
:class:`~src.escalation.telegram_bot.TelegramEscalationBridge` on their own
token instead, and the hub never sees them.

Linking is a one-time code. The console shows it, the user sends
``/link ABCD2345`` to the bot, the hub verifies it has not expired and binds
the chat. Until then the user has no channel, and escalations degrade to the
null bridge rather than going anywhere wrong.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import Conflict, InvalidToken, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from src.auth.security import new_link_code
from src.brain.models import MeetingContext, PolicyDecision
from src.db import repo
from src.db.base import session_scope
from src.escalation.models import (
    DeliveryReceipt,
    EscalationOutcome,
    EscalationRequest,
    EscalationResult,
)
from src.escalation.telegram_bot import (
    _ACTIONS,
    _CALLBACK_PREFIX,
    _OUTCOME_LABEL,
    TELEGRAM_MESSAGE_LIMIT,
    EscalationConfigError,
    _delivery_hint,
    build_keyboard,
    render_escalation_message,
    render_resolution_footer,
)

logger = logging.getLogger("deleg8.escalation.hub")

TenantCommand = Callable[[str], Awaitable[str]]
"""A per-tenant bot command handler: receives the argument string, returns the reply."""

LINK_CODE_TTL = timedelta(hours=24)

SETUP_INSTRUCTIONS: str = """\
How to connect your phone to DELEG8
-----------------------------------
1. Open Telegram and start a chat with the DELEG8 bot (the console shows its @name).
2. Press Start, then send:   /link <your code>
   The console shows your code on the Settings page and during onboarding.
3. The bot confirms, and from then on your approvals and briefings arrive there.

Nothing is sent to your chat until you link it, and only your own meetings
ever will be.
"""


def platform_token() -> str | None:
    """The shared bot token every unlinked tenant is offered."""
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None


@dataclass
class _Pending:
    """One escalation waiting on a human, with the tenant it belongs to."""

    request: EscalationRequest
    user_id: uuid.UUID
    chat_id: int
    future: asyncio.Future
    message_id: int
    rendered_text: str
    started_at: float = field(default_factory=time.monotonic)


class TelegramHub:
    """The single polling Application, shared by every tenant on the platform bot."""

    def __init__(self, token: str | None = None) -> None:
        """Bind to the platform bot token; nothing connects until :meth:`start`."""
        self._token = token or platform_token()
        self._app: Application | None = None
        self._started = False
        self.bot_username: str | None = None
        self._pending: dict[str, _Pending] = {}
        self._commands: dict[uuid.UUID, dict[str, TenantCommand]] = {}
        self._routes: dict[int, uuid.UUID] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    @property
    def available(self) -> bool:
        """True when a platform bot token is configured."""
        return bool(self._token)

    @property
    def started(self) -> bool:
        """True once polling has begun."""
        return self._started

    def describe(self) -> str:
        """Status line for the health page."""
        if not self.available:
            return "telegram hub: no platform bot token (users must bring their own bot)"
        if not self._started:
            return "telegram hub: not started"
        return f"telegram hub: @{self.bot_username}, {len(self._routes)} linked chat(s)"

    async def start(self) -> None:
        """Connect and begin long-polling. Safe to call when no token is configured.

        Raises:
            EscalationConfigError: Telegram rejected the token.
        """
        if self._started or not self._token:
            if not self._token:
                logger.warning(
                    "TELEGRAM_BOT_TOKEN is not set; tenants can only escalate with their own bot.\n%s",
                    SETUP_INSTRUCTIONS,
                )
            return
        app = Application.builder().token(self._token).build()
        app.add_handler(CommandHandler("start", self._on_start))
        app.add_handler(CommandHandler("link", self._on_link))
        app.add_handler(CommandHandler("unlink", self._on_unlink))
        app.add_handler(CommandHandler("help", self._on_help))
        for name in ("status", "leave", "conclude", "consent", "commitments"):
            app.add_handler(CommandHandler(name, self._tenant_command(name)))
        app.add_handler(CallbackQueryHandler(self._on_button, pattern=rf"^{_CALLBACK_PREFIX}\|"))
        try:
            await app.initialize()
        except InvalidToken as exc:
            raise EscalationConfigError(
                "Telegram rejected TELEGRAM_BOT_TOKEN. Re-copy it from @BotFather."
            ) from exc
        except TelegramError as exc:
            raise EscalationConfigError(f"Could not reach Telegram during startup: {exc}") from exc

        self._app = app
        self.bot_username = app.bot.username
        await app.start()
        if app.updater is None:  # pragma: no cover - the builder always makes one
            await app.stop()
            await app.shutdown()
            raise EscalationConfigError("Telegram Application was built without an Updater.")
        await app.updater.start_polling(
            allowed_updates=[Update.CALLBACK_QUERY, Update.MESSAGE],
            drop_pending_updates=True,
            error_callback=self._on_polling_error,
        )
        self._started = True
        self.reload_routes()
        logger.info(
            "telegram hub connected as @%s with %d linked chat(s)", self.bot_username, len(self._routes)
        )

    async def stop(self) -> None:
        """Resolve anything pending, stop polling, shut down. Idempotent."""
        app = self._app
        if app is None or not self._started:
            return
        for request_id in list(self._pending):
            await self.resolve(
                request_id, EscalationOutcome.TIMEOUT, None, note="DELEG8 shut down before a response arrived"
            )
        try:
            if app.updater is not None and app.updater.running:
                await app.updater.stop()
            if app.running:
                await app.stop()
        finally:
            await app.shutdown()
            self._started = False
            logger.info("telegram hub stopped")

    # ------------------------------------------------------------------ #
    # Routing
    # ------------------------------------------------------------------ #

    def reload_routes(self) -> None:
        """Rebuild the chat-id to tenant table from the database."""
        try:
            with session_scope() as session:
                routes: dict[int, uuid.UUID] = {}
                for channel in repo.list_verified_channels(session, "telegram"):
                    if channel.address and channel.bot_token_encrypted is None:
                        try:
                            routes[int(channel.address)] = channel.user_id
                        except ValueError:
                            continue
                self._routes = routes
        except Exception:  # noqa: BLE001 - a routing refresh must not take the bot down
            logger.exception("could not reload telegram routes")

    def route(self, chat_id: int) -> uuid.UUID | None:
        """Which tenant a chat belongs to, refreshing the table on a miss."""
        user_id = self._routes.get(chat_id)
        if user_id is None:
            self.reload_routes()
            user_id = self._routes.get(chat_id)
        return user_id

    def register_commands(self, user_id: uuid.UUID, commands: dict[str, TenantCommand]) -> None:
        """Bind this tenant's ``/status``, ``/leave``, ``/conclude`` handlers."""
        self._commands.setdefault(user_id, {}).update(commands)

    def unregister_commands(self, user_id: uuid.UUID) -> None:
        """Forget a tenant's handlers (their workspace was closed)."""
        self._commands.pop(user_id, None)

    def bridge_for(self, user_id: uuid.UUID, chat_id: int, *, timeout_seconds: float) -> TenantTelegramBridge:
        """A bridge bound to one tenant's chat."""
        return TenantTelegramBridge(self, user_id, chat_id, timeout_seconds=timeout_seconds)

    # ------------------------------------------------------------------ #
    # Sending
    # ------------------------------------------------------------------ #

    async def send_text(
        self, chat_id: int, text: str, *, markdown_v2: bool = False, plain_fallback: str | None = None
    ) -> DeliveryReceipt:
        """Push a message to one chat. Never raises for delivery problems."""
        if self._app is None or not self._started:
            return DeliveryReceipt(
                delivered=False, channel="telegram", detail="the telegram hub is not running"
            )
        payload = text if len(text) <= TELEGRAM_MESSAGE_LIMIT else text[: TELEGRAM_MESSAGE_LIMIT - 1] + "…"
        try:
            message = await self._app.bot.send_message(
                chat_id=chat_id,
                text=payload,
                parse_mode=ParseMode.MARKDOWN_V2 if markdown_v2 else None,
            )
        except TelegramError as exc:
            if markdown_v2 and plain_fallback:
                logger.warning("Telegram rejected MarkdownV2 (%s); resending as plain text", exc)
                try:
                    clipped = (
                        plain_fallback
                        if len(plain_fallback) <= TELEGRAM_MESSAGE_LIMIT
                        else plain_fallback[: TELEGRAM_MESSAGE_LIMIT - 1] + "…"
                    )
                    message = await self._app.bot.send_message(chat_id=chat_id, text=clipped)
                except TelegramError as retry_exc:
                    hint = _delivery_hint(retry_exc, self.bot_username)
                    return DeliveryReceipt(
                        delivered=False, channel="telegram", detail=f"{retry_exc}. {hint}"
                    )
            else:
                hint = _delivery_hint(exc, self.bot_username)
                logger.error("message to chat %s not delivered: %s. %s", chat_id, exc, hint)
                return DeliveryReceipt(delivered=False, channel="telegram", detail=f"{exc}. {hint}")
        return DeliveryReceipt(delivered=True, channel="telegram", message_id=message.message_id)

    async def send_escalation(
        self,
        *,
        user_id: uuid.UUID,
        chat_id: int,
        context: MeetingContext,
        proposed_action: str,
        decision: PolicyDecision,
        timeout_seconds: float,
    ) -> EscalationResult:
        """Send one tenant's escalation and wait for their answer."""
        if self._app is None or not self._started:
            return EscalationResult(
                request_id=uuid.uuid4().hex[:16],
                outcome=EscalationOutcome.UNDELIVERED,
                delivered=False,
                responded_by=None,
                response_seconds=0.0,
                message_id=None,
                detail="the telegram hub is not running",
            )
        request = EscalationRequest(
            request_id=uuid.uuid4().hex[:16],
            context=context,
            decision=decision,
            proposed_action=proposed_action,
            timeout_seconds=timeout_seconds,
        )
        text = render_escalation_message(request)
        started = time.monotonic()
        try:
            message = await self._app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=build_keyboard(request.request_id),
            )
        except TelegramError as exc:
            hint = _delivery_hint(exc, self.bot_username)
            logger.error("escalation %s not delivered to %s: %s", request.request_id, chat_id, exc)
            return EscalationResult(
                request_id=request.request_id,
                outcome=EscalationOutcome.UNDELIVERED,
                delivered=False,
                responded_by=None,
                response_seconds=0.0,
                message_id=None,
                detail=f"{exc}. {hint}",
            )
        pending = _Pending(
            request=request,
            user_id=user_id,
            chat_id=chat_id,
            future=asyncio.get_running_loop().create_future(),
            message_id=message.message_id,
            rendered_text=text,
            started_at=started,
        )
        self._pending[request.request_id] = pending
        logger.info(
            "escalation %s sent to chat %s for user %s (topic=%s)",
            request.request_id, chat_id, user_id, decision.topic,
        )
        try:
            resolution = await asyncio.wait_for(
                asyncio.shield(pending.future), timeout=request.timeout_seconds
            )
        except asyncio.TimeoutError:
            await self.resolve(request.request_id, EscalationOutcome.TIMEOUT, None)
            resolution = pending.future.result()
        outcome, responded_by, elapsed, note = resolution
        return EscalationResult(
            request_id=request.request_id,
            outcome=outcome,
            delivered=True,
            responded_by=responded_by,
            response_seconds=round(elapsed, 2),
            message_id=message.message_id,
            detail=note,
        )

    async def resolve(
        self,
        request_id: str,
        outcome: EscalationOutcome,
        responded_by: str | None,
        note: str | None = None,
        *,
        by_user: uuid.UUID | None = None,
    ) -> bool:
        """Settle a pending escalation and rewrite its alert.

        ``by_user`` is checked against the escalation's owner: a button tap from
        a chat that does not own the request is refused even if the request id
        is known, so one tenant can never answer another's escalation.
        """
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            return False
        if by_user is not None and pending.user_id != by_user:
            logger.warning(
                "refusing to resolve escalation %s: it belongs to %s, not %s",
                request_id, pending.user_id, by_user,
            )
            return False
        self._pending.pop(request_id, None)
        elapsed = time.monotonic() - pending.started_at
        pending.future.set_result((outcome, responded_by, elapsed, note))
        await self._annotate(pending, outcome, responded_by, elapsed, note)
        return True

    async def _annotate(
        self,
        pending: _Pending,
        outcome: EscalationOutcome,
        responded_by: str | None,
        elapsed: float,
        note: str | None,
    ) -> None:
        if self._app is None:
            return
        footer = render_resolution_footer(
            outcome, responded_by, elapsed, pending.request.timeout_seconds, note
        )
        try:
            await self._app.bot.edit_message_text(
                chat_id=pending.chat_id,
                message_id=pending.message_id,
                text=f"{pending.rendered_text}\n\n{footer}",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=None,
            )
        except TelegramError as exc:
            logger.warning("could not annotate escalation %s: %s", pending.request.request_id, exc)

    def pending_for(self, user_id: uuid.UUID) -> int:
        """How many escalations this tenant has waiting."""
        return sum(1 for p in self._pending.values() if p.user_id == user_id)

    # ------------------------------------------------------------------ #
    # Handlers
    # ------------------------------------------------------------------ #

    async def _on_start(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        message, chat = update.effective_message, update.effective_chat
        if message is None or chat is None:
            return
        user_id = self.route(chat.id)
        if user_id is not None:
            await message.reply_text(
                "This chat is already linked to your DELEG8 account. "
                "You will get approval requests and meeting briefings here.\n"
                "Commands: /status /leave /conclude /consent /commitments /unlink"
            )
            return
        await message.reply_text(
            "Welcome to DELEG8.\n\n"
            "This chat is not linked to an account yet. Open the DELEG8 console, "
            "go to Settings (or finish onboarding), copy your link code, and send:\n\n"
            "    /link YOURCODE\n\n"
            f"(For reference, this chat's id is {chat.id}.)"
        )

    async def _on_help(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is not None:
            await message.reply_text(
                "DELEG8 commands:\n"
                "/link <code>   connect this chat to your account\n"
                "/status        what your delegate is doing right now\n"
                "/leave         recall your delegate from its meeting\n"
                "/conclude      end a meeting and send the briefing\n"
                "/consent       grant or refuse recording consent for a live meeting\n"
                "/commitments   open action items\n"
                "/unlink        disconnect this chat"
            )

    async def _on_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Bind this chat to the account whose one-time code was supplied."""
        message, chat, sender = update.effective_message, update.effective_chat, update.effective_user
        if message is None or chat is None:
            return
        code = (" ".join(context.args or [])).strip().upper()
        if not code:
            await message.reply_text("Send your code like this:  /link ABCD2345")
            return
        with session_scope() as session:
            channel = repo.find_channel_by_link_code(session, code)
            if channel is None:
                await message.reply_text(
                    "That code is not valid or has expired. Generate a new one in the DELEG8 console."
                )
                return
            existing = repo.find_channel_by_address(session, str(chat.id))
            if existing is not None and existing.user_id != channel.user_id:
                await message.reply_text(
                    "This chat is already linked to a different DELEG8 account. Send /unlink first."
                )
                return
            repo.upsert_channel(
                session,
                user_id=channel.user_id,
                kind="telegram",
                address=str(chat.id),
                verified=True,
                link_code=None,
                link_code_expires_at=None,
                label=(sender.full_name if sender else None),
            )
            user = repo.get_user(session, channel.user_id)
            if user is not None:
                repo.set_onboarding(session, user, escalation=True)
            display = user.display_name if user is not None else "your account"
        self.reload_routes()
        logger.info("linked telegram chat %s to user %s", chat.id, channel.user_id)
        await message.reply_text(
            f"Linked to {display}. Approval requests and meeting briefings will arrive here.\n"
            "Commands: /status /leave /conclude /consent /commitments"
        )

    async def _on_unlink(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        message, chat = update.effective_message, update.effective_chat
        if message is None or chat is None:
            return
        user_id = self.route(chat.id)
        if user_id is None:
            await message.reply_text("This chat is not linked to a DELEG8 account.")
            return
        with session_scope() as session:
            repo.upsert_channel(
                session, user_id=user_id, kind="telegram", address=None, verified=False, link_code=None
            )
            user = repo.get_user(session, user_id)
            if user is not None:
                repo.set_onboarding(session, user, escalation=False)
        self.reload_routes()
        await message.reply_text(
            "Unlinked. Your delegate can no longer reach you here, so escalations will "
            "fall back to its safe holding reply."
        )

    def _tenant_command(self, name: str) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]:
        """A handler that resolves the tenant from the chat and calls their own callback."""

        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            message, chat = update.effective_message, update.effective_chat
            if message is None or chat is None:
                return
            user_id = self.route(chat.id)
            if user_id is None:
                await message.reply_text(
                    "This chat is not linked to a DELEG8 account yet. Send /link <code> first."
                )
                return
            callback = self._commands.get(user_id, {}).get(name)
            if callback is None:
                await message.reply_text(
                    f"/{name} is not available right now - your delegate is not running on this server."
                )
                return
            args = " ".join(context.args or [])
            try:
                reply = await callback(args)
            except Exception as exc:  # noqa: BLE001 - never let a handler kill the poller
                logger.exception("/%s failed for user %s", name, user_id)
                reply = f"/{name} failed: {exc}"
            if reply:
                await message.reply_text(reply[:TELEGRAM_MESSAGE_LIMIT])

        return handler

    async def _on_button(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        """Resolve an escalation from a button tap, checking the chat owns it."""
        query = update.callback_query
        chat = update.effective_chat
        if query is None or not query.data or chat is None:
            return
        user_id = self.route(chat.id)
        if user_id is None:
            await query.answer("This chat is not linked to a DELEG8 account.", show_alert=True)
            return
        parts = query.data.split("|")
        if len(parts) != 3 or parts[0] != _CALLBACK_PREFIX or parts[2] not in _ACTIONS:
            await query.answer("Unrecognised action.", show_alert=True)
            return
        _, request_id, action = parts
        _, outcome = _ACTIONS[action]
        sender = update.effective_user
        responded_by = sender.full_name if sender is not None else "unknown"
        if await self.resolve(request_id, outcome, responded_by, by_user=user_id):
            await query.answer(f"{_OUTCOME_LABEL[outcome]} recorded.")
        else:
            await query.answer("That escalation has already been resolved or has expired.", show_alert=True)

    def _on_polling_error(self, error: TelegramError) -> None:
        if isinstance(error, Conflict):
            logger.error(
                "Telegram refused getUpdates: another process is polling this bot token. "
                "Only one DELEG8 server may run against a given TELEGRAM_BOT_TOKEN."
            )
        else:
            logger.warning("Telegram polling error: %s", error)


class TenantTelegramBridge:
    """An :class:`~src.escalation.bridge.EscalationBridge` permanently bound to one chat.

    It holds a chat id and a user id and has no method that accepts either, so
    there is no code path by which one tenant's delegate can message another
    tenant's phone.
    """

    def __init__(
        self, hub: TelegramHub, user_id: uuid.UUID, chat_id: int, *, timeout_seconds: float = 30.0
    ) -> None:
        """Bind to a tenant's chat on an already-running hub."""
        self._hub = hub
        self._user_id = user_id
        self._chat_id = chat_id
        self._timeout = timeout_seconds

    async def start(self) -> None:
        """Nothing to start: the hub owns the connection."""

    async def stop(self) -> None:
        """Nothing to stop: the hub owns the connection."""

    def describe(self) -> str:
        """Status line for health checks."""
        if not self._hub.started:
            return "telegram: hub not running"
        return f"telegram: @{self._hub.bot_username} -> your chat (timeout {self._timeout:g}s)"

    @property
    def pending_count(self) -> int:
        """Escalations this tenant has waiting."""
        return self._hub.pending_for(self._user_id)

    async def send_escalation_request(
        self, context: MeetingContext, proposed_action: str, decision: PolicyDecision
    ) -> EscalationResult:
        """Alert this tenant and wait for their verdict."""
        return await self._hub.send_escalation(
            user_id=self._user_id,
            chat_id=self._chat_id,
            context=context,
            proposed_action=proposed_action,
            decision=decision,
            timeout_seconds=self._timeout,
        )

    async def send_text(
        self, text: str, *, markdown_v2: bool = False, plain_fallback: str | None = None
    ) -> DeliveryReceipt:
        """Push a one-way message to this tenant."""
        return await self._hub.send_text(
            self._chat_id, text, markdown_v2=markdown_v2, plain_fallback=plain_fallback
        )

    def register_command(self, name: str, callback: TenantCommand) -> None:
        """Bind ``/name`` for this tenant only."""
        self._hub.register_commands(self._user_id, {name: callback})


def issue_link_code(session: Session, user_id: uuid.UUID) -> tuple[str, datetime]:
    """Mint a fresh one-time link code for a tenant. Returns ``(code, expires_at)``."""
    code = new_link_code()
    expires = datetime.now(timezone.utc) + LINK_CODE_TTL
    repo.upsert_channel(
        session,
        user_id=user_id,
        kind="telegram",
        link_code=code,
        link_code_expires_at=expires,
    )
    return code, expires
