"""
Barge-in: stop the bot the instant the human starts talking over it.

:class:`BargeInHandler` is the process-wide cancellation manager for the
bot's voice output. TTS registers a :class:`CancellationToken` when it
starts speaking; STT reports user speech as it arrives. When both are true
at once the handler:

1. flushes the speaker queue (synchronous, takes effect within one frame),
2. cancels the token, which runs the callbacks TTS registered (cancel the
   Cartesia context and stop receiving chunks),
3. prints ``[INTERRUPTED]``.

Everything runs on the event-loop thread; no locks are needed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from src.audio.audio_io import AudioIO

logger = logging.getLogger("deleg8.audio.bargein")

CancelCallback = Callable[[], Awaitable[None]]


class CancellationToken:
    """A one-shot cancellation signal with async callbacks."""

    def __init__(self, label: str) -> None:
        """Create an un-cancelled token described by ``label`` (for logs)."""
        self.label = label
        self.created_at = time.monotonic()
        self.cancelled_at: float | None = None
        self._event = asyncio.Event()
        self._callbacks: list[CancelCallback] = []

    @property
    def cancelled(self) -> bool:
        """True once :meth:`cancel` has run."""
        return self._event.is_set()

    def on_cancel(self, callback: CancelCallback) -> None:
        """Register an async callback to run when the token is cancelled."""
        self._callbacks.append(callback)

    async def cancel(self) -> None:
        """Mark cancelled and run every callback; errors are logged, never raised."""
        if self.cancelled:
            return
        self.cancelled_at = time.monotonic()
        self._event.set()
        for callback in self._callbacks:
            try:
                await callback()
            except Exception:  # noqa: BLE001 - a failing callback must not block the others
                logger.exception("cancel callback failed for %s", self.label)

    async def wait(self) -> None:
        """Block until cancelled."""
        await self._event.wait()


class BargeInHandler:
    """Global cancellation manager: interrupts bot speech when the user talks."""

    def __init__(self, audio_io: AudioIO, min_words: int = 1) -> None:
        """Bind to the speaker (for flushing) and set the interruption threshold."""
        self._audio_io = audio_io
        self._min_words = max(1, min_words)
        self._active: CancellationToken | None = None
        self.interruptions = 0
        self.last_interruption_ms: float | None = None
        self.on_interrupt: Callable[[str], None] | None = None
        """Optional observer called with the reason after every interruption."""

    # ------------------------------------------------------------------ #
    # Called by TTS
    # ------------------------------------------------------------------ #

    def begin_speaking(self, label: str) -> CancellationToken:
        """Register that the bot is about to speak; returns the token TTS should watch."""
        token = CancellationToken(label)
        self._active = token
        return token

    def end_speaking(self, token: CancellationToken) -> None:
        """Clear the active token once its utterance has finished or been interrupted."""
        if self._active is token:
            self._active = None

    @property
    def is_bot_speaking(self) -> bool:
        """True while an un-cancelled token is registered."""
        return self._active is not None and not self._active.cancelled

    @property
    def active_token(self) -> CancellationToken | None:
        """The token for the utterance in progress, if any."""
        return self._active

    # ------------------------------------------------------------------ #
    # Called by STT
    # ------------------------------------------------------------------ #

    async def on_user_speech(self, transcript: str, *, word_count: int, source: str) -> bool:
        """Report user speech. Returns True when it interrupted the bot."""
        if not self.is_bot_speaking or word_count < self._min_words:
            return False
        await self.interrupt(f"user said {transcript!r} ({source})")
        return True

    async def interrupt(self, reason: str) -> None:
        """Stop bot audio now: flush the speaker, cancel the token, announce it."""
        token = self._active
        if token is None or token.cancelled:
            return
        started = time.monotonic()
        dropped = self._audio_io.flush_playback()  # instant: playback loop stops at the next frame
        await token.cancel()  # TTS callbacks: cancel the Cartesia context, stop receiving
        self._active = None
        self.interruptions += 1
        self.last_interruption_ms = (time.monotonic() - started) * 1000
        spoken_for = started - token.created_at
        print(
            f"[INTERRUPTED] {reason}; bot had been speaking for {spoken_for:.1f}s, "
            f"dropped {dropped} queued bytes, cancelled in {self.last_interruption_ms:.0f}ms",
            flush=True,
        )
        logger.info("barge-in: %s", reason)
        if self.on_interrupt is not None:
            try:
                self.on_interrupt(reason)
            except Exception:  # noqa: BLE001 - observers are best-effort
                logger.exception("barge-in observer failed")
