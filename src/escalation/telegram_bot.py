"""
Telegram escalation bridge.

Runs a python-telegram-bot ``Application`` on the same event loop as
FastAPI (started and stopped from the app lifespan) and turns an ESCALATE
decision into a phone notification with inline buttons.

Flow for one escalation::

    send_escalation_request()
        render MarkdownV2 alert + [Approve] [Reject] [Decline Politely]
        bot.send_message()
        register asyncio.Future keyed by request_id in self._pending
        await future, bounded by ESCALATION_TIMEOUT_SECONDS
                 |
                 |   _on_button()  (Telegram callback, same loop)
                 |       verify the tap came from the configured chat
                 |       resolve(request_id, outcome, who)
                 |           pop pending, set the Future, edit the alert
                 v
        on timeout: resolve(request_id, TIMEOUT) - edits the alert, drops buttons
        return EscalationResult

Only one process may poll a bot token at a time. Telegram answers a
second poller with HTTP 409 Conflict, which this module logs loudly.

Sprint 4 additions: :meth:`TelegramEscalationBridge.send_text` pushes a
one-way message (the post-meeting briefing) and
:meth:`TelegramEscalationBridge.register_command` lets the application bind
bot commands such as ``/conclude``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, Forbidden, InvalidToken, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from telegram.helpers import escape_markdown

from src.brain.models import MeetingContext, PolicyDecision, PolicyTier
from src.escalation.models import (
    DeliveryReceipt,
    EscalationOutcome,
    EscalationRequest,
    EscalationResult,
)

CommandCallback = Callable[[str], Awaitable[str]]
"""Handler for a bot command: receives the argument string, returns the reply text."""

TELEGRAM_MESSAGE_LIMIT: int = 4096
"""Telegram rejects text messages longer than this."""

logger = logging.getLogger("deleg8.escalation.telegram")

SETUP_INSTRUCTIONS: str = """\
How to configure the DELEG8 Telegram bridge
-------------------------------------------
1. Create a bot token (TELEGRAM_BOT_TOKEN)
   a. In Telegram, open a chat with @BotFather and send:  /newbot
   b. Give the bot a display name (e.g. "DELEG8 Escalations").
   c. Give it a username that ends in "bot" (e.g. deleg8_escalations_bot).
   d. BotFather replies with a token that looks like  123456789:AAF...xyz
      Copy it into .env as  TELEGRAM_BOT_TOKEN=<token>

2. Find your chat id (TELEGRAM_CHAT_ID)
   a. Open a chat with @userinfobot and press Start.
   b. It replies with your numeric "Id" (e.g. 987654321).
      Copy it into .env as  TELEGRAM_CHAT_ID=<id>
   (Alternative: send /start to your own bot while DELEG8 is running;
    it replies with the chat id it sees.)

3. Let the bot message you
   Open a chat with the bot you created in step 1 and press Start.
   Telegram bots cannot message a user who has never started them.

4. Choose the decision window
   ESCALATION_TIMEOUT_SECONDS=30   (seconds to wait before the delegate defers)

Then run:  cp .env.example .env   (if you have not already) and fill in the values.
"""


class EscalationConfigError(RuntimeError):
    """Raised when the Telegram bridge is misconfigured or cannot connect."""


@dataclass(frozen=True)
class TelegramSettings:
    """Runtime configuration for the bridge, normally read from the environment."""

    bot_token: str
    chat_id: int
    timeout_seconds: float

    @staticmethod
    def is_configured() -> bool:
        """True when both the token and chat id are present in the environment."""
        return bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip()) and bool(
            os.getenv("TELEGRAM_CHAT_ID", "").strip()
        )

    @classmethod
    def from_env(cls) -> TelegramSettings:
        """Read and validate settings from the environment.

        Raises:
            EscalationConfigError: with setup instructions when a value is missing or malformed.
        """
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id_raw = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        timeout_raw = os.getenv("ESCALATION_TIMEOUT_SECONDS", "30").strip() or "30"

        missing = [name for name, value in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id_raw)) if not value]
        if missing:
            raise EscalationConfigError(f"Missing environment variable(s): {', '.join(missing)}.\n\n{SETUP_INSTRUCTIONS}")
        try:
            chat_id = int(chat_id_raw)
        except ValueError as exc:
            raise EscalationConfigError(
                f"TELEGRAM_CHAT_ID must be an integer (got {chat_id_raw!r}). Ask @userinfobot for your numeric id."
            ) from exc
        try:
            timeout_seconds = float(timeout_raw)
        except ValueError as exc:
            raise EscalationConfigError(f"ESCALATION_TIMEOUT_SECONDS must be a number (got {timeout_raw!r}).") from exc
        if timeout_seconds <= 0:
            raise EscalationConfigError("ESCALATION_TIMEOUT_SECONDS must be greater than zero.")
        return cls(bot_token=token, chat_id=chat_id, timeout_seconds=timeout_seconds)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

_CALLBACK_PREFIX: str = "esc"

_ACTIONS: dict[str, tuple[str, EscalationOutcome]] = {
    "approve": ("✅ Approve", EscalationOutcome.APPROVED),
    "reject": ("❌ Reject", EscalationOutcome.REJECTED),
    "decline": ("🙏 Decline Politely", EscalationOutcome.DECLINED_POLITELY),
}

_OUTCOME_LABEL: dict[EscalationOutcome, str] = {
    EscalationOutcome.APPROVED: "Approved",
    EscalationOutcome.REJECTED: "Rejected",
    EscalationOutcome.DECLINED_POLITELY: "Declined politely",
    EscalationOutcome.TIMEOUT: "Timed out",
    EscalationOutcome.UNDELIVERED: "Undelivered",
}

_TIER_BADGE: dict[PolicyTier, str] = {
    PolicyTier.GREEN: "🟢 GREEN",
    PolicyTier.YELLOW: "🟡 YELLOW",
    PolicyTier.RED: "🔴 RED",
}


def _md(text: str) -> str:
    """Escape arbitrary text for MarkdownV2."""
    return escape_markdown(text, version=2)


def _code(text: str) -> str:
    """Escape text destined for a MarkdownV2 code span."""
    return escape_markdown(text, version=2, entity_type="code")


def _seconds(value: float) -> str:
    """Render a duration without a trailing '.0'."""
    return f"{value:g}"


def build_keyboard(request_id: str) -> InlineKeyboardMarkup:
    """Inline buttons whose callback data carries the request id and the action."""
    buttons = {
        action: InlineKeyboardButton(label, callback_data=f"{_CALLBACK_PREFIX}|{request_id}|{action}")
        for action, (label, _) in _ACTIONS.items()
    }
    return InlineKeyboardMarkup([[buttons["approve"], buttons["reject"]], [buttons["decline"]]])


def render_escalation_message(request: EscalationRequest) -> str:
    """Format the alert as MarkdownV2: context, question, why it was flagged, proposed reply."""
    ctx, decision = request.context, request.decision
    tier = _TIER_BADGE[decision.tier] if decision.tier is not None else "⚪ NO POLICY"
    topic = decision.topic or "uncovered_topic"
    triggers = ", ".join(f"`{_code(t)}`" for t in decision.matched_triggers) or "none"
    participants = ", ".join(ctx.participants) or "unknown"
    lines = [
        "🚨 *DELEG8 needs your decision*",
        "",
        f"*Meeting:* {_md(ctx.meeting_id)}",
        f"*Speaker:* {_md(ctx.speaker or 'unknown')}",
        f"*Participants:* {_md(participants)}",
        "",
        "*They asked:*",
        f"❝ {_md(ctx.transcript)} ❞",
        "",
        f"*Flagged:* {tier} · `{_code(topic)}`",
        f"*Triggers:* {triggers}",
        f"*Why:* {_md(decision.reason)}",
        "",
        "*Proposed reply if approved:*",
        f"❝ {_md(request.proposed_action)} ❞",
        "",
        f"⏳ Respond within {_md(_seconds(request.timeout_seconds))}s or the delegate will defer\\.",
    ]
    return "\n".join(lines)


def render_resolution_footer(
    outcome: EscalationOutcome,
    responded_by: str | None,
    elapsed_seconds: float,
    timeout_seconds: float,
    note: str | None = None,
) -> str:
    """The line appended to the alert once it is resolved."""
    who = _md(responded_by or "unknown")
    took = _md(f"{elapsed_seconds:.1f}")
    if outcome is EscalationOutcome.APPROVED:
        line = f"✅ *Approved by {who}* · {took}s"
    elif outcome is EscalationOutcome.REJECTED:
        line = f"❌ *Rejected by {who}* · {took}s"
    elif outcome is EscalationOutcome.DECLINED_POLITELY:
        line = f"🙏 *Declined politely by {who}* · {took}s"
    elif outcome is EscalationOutcome.TIMEOUT:
        line = f"⏰ *No response within {_md(_seconds(timeout_seconds))}s* · delegate gave the fallback reply"
    else:
        line = "⚠️ *Undelivered*"
    if note:
        line += f"\n_{_md(note)}_"
    return line


def _delivery_hint(error: TelegramError, bot_username: str | None) -> str:
    """Translate a Telegram send failure into an actionable hint."""
    handle = f"@{bot_username}" if bot_username else "the bot"
    if isinstance(error, Forbidden):
        return f"The user has blocked or never started {handle}. Open a chat with it and press Start."
    if isinstance(error, BadRequest) and "chat not found" in str(error).lower():
        return f"TELEGRAM_CHAT_ID does not match a chat {handle} can see. Check the id, then press Start in the bot chat."
    return "Check network connectivity and the bot token."


# --------------------------------------------------------------------------- #
# Bridge
# --------------------------------------------------------------------------- #

@dataclass
class _Resolution:
    """What a pending Future resolves to."""

    outcome: EscalationOutcome
    responded_by: str | None
    elapsed_seconds: float
    note: str | None = None


@dataclass
class _PendingEscalation:
    """Book-keeping for one alert that is waiting on a human."""

    request: EscalationRequest
    future: asyncio.Future[_Resolution]
    message_id: int
    rendered_text: str
    started_at: float = field(default_factory=time.monotonic)


class TelegramEscalationBridge:
    """Puts ESCALATE decisions in front of a human over Telegram and waits for the verdict.

    One instance serves the whole process. It is safe to have many
    escalations in flight at once: each is keyed by its own request id.
    """

    def __init__(self, settings: TelegramSettings, application: Application | None = None) -> None:
        """Bind to ``settings``. An ``application`` can be injected for testing."""
        self._settings = settings
        self._application: Application | None = application
        self._pending: dict[str, _PendingEscalation] = {}
        self._started = False
        self._bot_username: str | None = None
        self._commands: dict[str, CommandCallback] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Initialise the bot, validate the token, and begin long-polling for button taps.

        Raises:
            EscalationConfigError: if Telegram rejects the token or is unreachable.
        """
        if self._started:
            return
        app = self._application or Application.builder().token(self._settings.bot_token).build()
        app.add_handler(CommandHandler("start", self._on_start))
        app.add_handler(CallbackQueryHandler(self._on_button, pattern=rf"^{_CALLBACK_PREFIX}\|"))
        for name in self._commands:
            app.add_handler(CommandHandler(name, self._command_handler(name)))
        try:
            await app.initialize()  # performs getMe, so a bad token fails here
        except InvalidToken as exc:
            raise EscalationConfigError(
                "Telegram rejected TELEGRAM_BOT_TOKEN. Re-copy it from @BotFather.\n\n" + SETUP_INSTRUCTIONS
            ) from exc
        except TelegramError as exc:
            raise EscalationConfigError(f"Could not reach Telegram during startup: {exc}") from exc

        self._application = app
        self._bot_username = app.bot.username
        await app.start()
        if app.updater is None:
            await app.stop()
            await app.shutdown()
            raise EscalationConfigError("Telegram Application was built without an Updater; polling is required.")
        await app.updater.start_polling(
            allowed_updates=[Update.CALLBACK_QUERY, Update.MESSAGE],
            drop_pending_updates=True,
            error_callback=self._on_polling_error,
        )
        self._started = True
        logger.info("Telegram bridge connected as @%s, escalating to chat %s, timeout %.0fs",
                    self._bot_username, self._settings.chat_id, self._settings.timeout_seconds)
        await self._probe_chat()

    async def stop(self) -> None:
        """Resolve anything still pending, stop polling, and shut the bot down. Idempotent."""
        app = self._application
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
            logger.info("Telegram bridge stopped")

    def describe(self) -> str:
        """Status line for health checks."""
        if not self._started:
            return "telegram: not started"
        return f"telegram: @{self._bot_username} -> chat {self._settings.chat_id} (timeout {_seconds(self._settings.timeout_seconds)}s)"

    @property
    def pending_count(self) -> int:
        """Number of escalations currently waiting on a human."""
        return len(self._pending)

    async def _probe_chat(self) -> None:
        """Warn early if the configured chat cannot be reached (user never pressed Start)."""
        assert self._application is not None
        try:
            await self._application.bot.get_chat(self._settings.chat_id)
        except TelegramError as exc:
            logger.warning(
                "Chat %s is not reachable yet (%s). %s",
                self._settings.chat_id, exc, _delivery_hint(exc, self._bot_username),
            )

    # ------------------------------------------------------------------ #
    # Escalation
    # ------------------------------------------------------------------ #

    async def send_escalation_request(
        self, context: MeetingContext, proposed_action: str, decision: PolicyDecision
    ) -> EscalationResult:
        """Send the alert, wait for a button tap or the timeout, and return the outcome.

        Never raises for delivery problems: those come back as ``UNDELIVERED``
        so the delegate can fall back gracefully mid-meeting.
        """
        if not self._started or self._application is None:
            raise RuntimeError("TelegramEscalationBridge.start() must be awaited before sending escalations")

        request = EscalationRequest(
            request_id=uuid4().hex[:16],
            context=context,
            decision=decision,
            proposed_action=proposed_action,
            timeout_seconds=self._settings.timeout_seconds,
        )
        text = render_escalation_message(request)
        started_at = time.monotonic()
        try:
            message = await self._application.bot.send_message(
                chat_id=self._settings.chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=build_keyboard(request.request_id),
            )
        except TelegramError as exc:
            hint = _delivery_hint(exc, self._bot_username)
            logger.error("escalation %s could not be delivered: %s. %s", request.request_id, exc, hint)
            return EscalationResult(
                request_id=request.request_id,
                outcome=EscalationOutcome.UNDELIVERED,
                delivered=False,
                responded_by=None,
                response_seconds=0.0,
                message_id=None,
                detail=f"{exc}. {hint}",
            )

        pending = _PendingEscalation(
            request=request,
            future=asyncio.get_running_loop().create_future(),
            message_id=message.message_id,
            rendered_text=text,
            started_at=started_at,
        )
        self._pending[request.request_id] = pending
        logger.info(
            "escalation %s sent as message %s; waiting up to %ss (topic=%s tier=%s)",
            request.request_id, message.message_id, _seconds(request.timeout_seconds),
            decision.topic, decision.tier.value if decision.tier else None,
        )

        try:
            resolution = await asyncio.wait_for(asyncio.shield(pending.future), timeout=request.timeout_seconds)
        except asyncio.TimeoutError:
            # A tap may have won the race; resolve() is a no-op in that case.
            await self.resolve(request.request_id, EscalationOutcome.TIMEOUT, None)
            resolution = pending.future.result()

        logger.info(
            "escalation %s resolved %s by %s after %.1fs",
            request.request_id, resolution.outcome.value, resolution.responded_by, resolution.elapsed_seconds,
        )
        return EscalationResult(
            request_id=request.request_id,
            outcome=resolution.outcome,
            delivered=True,
            responded_by=resolution.responded_by,
            response_seconds=round(resolution.elapsed_seconds, 2),
            message_id=message.message_id,
            detail=resolution.note,
        )

    async def resolve(
        self,
        request_id: str,
        outcome: EscalationOutcome,
        responded_by: str | None,
        note: str | None = None,
    ) -> bool:
        """Settle a pending escalation: fulfil its Future and rewrite the alert.

        Returns ``False`` when nothing was pending under ``request_id`` (already
        resolved, timed out, or unknown), so callers can tell the user.
        """
        pending = self._pending.pop(request_id, None)
        if pending is None or pending.future.done():
            return False
        elapsed = time.monotonic() - pending.started_at
        pending.future.set_result(_Resolution(outcome, responded_by, elapsed, note))
        await self._annotate_message(pending, outcome, responded_by, elapsed, note)
        return True

    async def _annotate_message(
        self,
        pending: _PendingEscalation,
        outcome: EscalationOutcome,
        responded_by: str | None,
        elapsed_seconds: float,
        note: str | None,
    ) -> None:
        """Edit the alert in place to show the verdict and remove the buttons."""
        assert self._application is not None
        footer = render_resolution_footer(
            outcome, responded_by, elapsed_seconds, pending.request.timeout_seconds, note
        )
        try:
            await self._application.bot.edit_message_text(
                chat_id=self._settings.chat_id,
                message_id=pending.message_id,
                text=f"{pending.rendered_text}\n\n{footer}",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=None,
            )
        except TelegramError as exc:
            logger.warning("could not annotate escalation %s (message %s): %s",
                           pending.request.request_id, pending.message_id, exc)

    # ------------------------------------------------------------------ #
    # One-way messages and commands (Sprint 4)
    # ------------------------------------------------------------------ #

    async def send_text(
        self, text: str, *, markdown_v2: bool = False, plain_fallback: str | None = None
    ) -> DeliveryReceipt:
        """Push a message to the configured chat. Never raises for delivery problems.

        With ``markdown_v2`` the text must already be escaped for MarkdownV2.
        If Telegram rejects the formatting and ``plain_fallback`` is given, the
        fallback is sent as plain text instead.
        """
        if not self._started or self._application is None:
            raise RuntimeError("TelegramEscalationBridge.start() must be awaited before sending messages")
        bot = self._application.bot
        payload = text if len(text) <= TELEGRAM_MESSAGE_LIMIT else text[: TELEGRAM_MESSAGE_LIMIT - 1] + "…"
        try:
            message = await bot.send_message(
                chat_id=self._settings.chat_id,
                text=payload,
                parse_mode=ParseMode.MARKDOWN_V2 if markdown_v2 else None,
            )
        except BadRequest as exc:
            if not (markdown_v2 and plain_fallback):
                hint = _delivery_hint(exc, self._bot_username)
                logger.error("message could not be delivered: %s. %s", exc, hint)
                return DeliveryReceipt(delivered=False, channel="telegram", detail=f"{exc}. {hint}")
            logger.warning("Telegram rejected the MarkdownV2 briefing (%s); resending as plain text", exc)
            try:
                fallback = plain_fallback if len(plain_fallback) <= TELEGRAM_MESSAGE_LIMIT else plain_fallback[: TELEGRAM_MESSAGE_LIMIT - 1] + "…"
                message = await bot.send_message(chat_id=self._settings.chat_id, text=fallback)
            except TelegramError as retry_exc:
                hint = _delivery_hint(retry_exc, self._bot_username)
                logger.error("message could not be delivered: %s. %s", retry_exc, hint)
                return DeliveryReceipt(delivered=False, channel="telegram", detail=f"{retry_exc}. {hint}")
        except TelegramError as exc:
            hint = _delivery_hint(exc, self._bot_username)
            logger.error("message could not be delivered: %s. %s", exc, hint)
            return DeliveryReceipt(delivered=False, channel="telegram", detail=f"{exc}. {hint}")
        logger.info("message %s delivered to chat %s (%d chars)", message.message_id, self._settings.chat_id, len(payload))
        return DeliveryReceipt(delivered=True, channel="telegram", message_id=message.message_id)

    def register_command(self, name: str, callback: CommandCallback) -> None:
        """Bind ``/name`` in the configured chat to ``callback``; the returned text is sent as the reply.

        May be called before or after :meth:`start`.
        """
        name = name.strip().lstrip("/").lower()
        if not name.isalnum():
            raise ValueError(f"invalid command name {name!r}")
        already = name in self._commands
        self._commands[name] = callback
        if self._started and self._application is not None and not already:
            self._application.add_handler(CommandHandler(name, self._command_handler(name)))

    def _command_handler(self, name: str) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]:
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            message = update.effective_message
            chat = update.effective_chat
            if message is None or chat is None:
                return
            if chat.id != self._settings.chat_id:
                logger.warning("ignoring /%s from unauthorised chat %s", name, chat.id)
                await message.reply_text("This bot only accepts commands from its configured chat.")
                return
            args = " ".join(context.args or [])
            callback = self._commands[name]
            try:
                reply = await callback(args)
            except Exception as exc:  # noqa: BLE001 - never let a handler crash the poller
                logger.exception("/%s handler failed", name)
                reply = f"/{name} failed: {exc}"
            if reply:
                await message.reply_text(reply[:TELEGRAM_MESSAGE_LIMIT])

        return handler

    # ------------------------------------------------------------------ #
    # Telegram handlers
    # ------------------------------------------------------------------ #

    async def _on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle an inline-button tap: authorise, parse, resolve, acknowledge."""
        query = update.callback_query
        if query is None or not query.data:
            return
        chat = update.effective_chat
        if chat is None or chat.id != self._settings.chat_id:
            logger.warning("ignoring button tap from unauthorised chat %s", chat.id if chat else None)
            await query.answer("This bot only accepts decisions from its configured chat.", show_alert=True)
            return

        parts = query.data.split("|")
        if len(parts) != 3 or parts[0] != _CALLBACK_PREFIX or parts[2] not in _ACTIONS:
            await query.answer("Unrecognised action.", show_alert=True)
            return
        _, request_id, action = parts
        _, outcome = _ACTIONS[action]
        user = update.effective_user
        responded_by = user.full_name if user is not None else "unknown"

        if await self.resolve(request_id, outcome, responded_by):
            await query.answer(f"{_OUTCOME_LABEL[outcome]} recorded.")
        else:
            await query.answer("That escalation has already been resolved or has expired.", show_alert=True)

    async def _on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Reply to /start with the chat id so users can confirm their configuration."""
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        if chat.id == self._settings.chat_id:
            commands = "".join(f" /{name}" for name in self._commands)
            await message.reply_text(
                f"DELEG8 escalation bridge connected. This chat (id {chat.id}) will receive "
                "escalations with Approve / Reject / Decline Politely buttons"
                + (f" and accepts:{commands}." if commands else ".")
            )
        else:
            await message.reply_text(
                f"This DELEG8 bridge is bound to a different chat. Your chat id is {chat.id}; "
                "set TELEGRAM_CHAT_ID to that value if this is where alerts should go."
            )

    def _on_polling_error(self, error: TelegramError) -> None:
        """Log polling failures; a 409 Conflict means two processes are polling this token."""
        if isinstance(error, Conflict):
            logger.error(
                "Telegram refused getUpdates: another process is polling this bot token. "
                "Run either the FastAPI server or tests/test_escalation.py, not both at once."
            )
        else:
            logger.warning("Telegram polling error: %s", error)
