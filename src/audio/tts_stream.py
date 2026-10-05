"""
Streaming text-to-speech over Cartesia's WebSocket API.

:meth:`TtsStream.speak` sends one transcript on a fresh Cartesia context,
decodes the PCM chunks as they stream back, and pushes them straight onto
:attr:`AudioIO.playback_queue` so the first audio plays before synthesis has
finished. A :class:`CancellationToken` from :class:`BargeInHandler` guards
the whole utterance: when the user interrupts, the token's callback cancels
the Cartesia context and stops the receive task, and ``speak`` returns with
``interrupted=True``.

One WebSocket connection is kept open for the session and reused across
utterances; it is re-established once if a send fails.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass

from cartesia import APIStatusError, AsyncCartesia
from cartesia.lib._tts import (
    AsyncBackcompatTTSResourceConnection,
    AsyncWebSocketContext,
)
from cartesia.types.voice import Voice

from src.audio.audio_io import AudioIO
from src.audio.barge_in_handler import BargeInHandler, CancellationToken
from src.audio.config import VoiceConfigError, VoiceSettings

logger = logging.getLogger("deleg8.audio.tts")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
"""Cartesia voice ids are UUIDs; a short number is almost always a copy-paste mistake
(e.g. an audio device index landing in CARTESIA_VOICE_ID instead of DELEG8_*_DEVICE_INDEX)."""


class TtsError(RuntimeError):
    """Raised when Cartesia reports an error for an utterance."""


@dataclass(frozen=True)
class SpeakResult:
    """What happened when one reply was spoken."""

    text: str
    interrupted: bool
    chunks: int
    audio_bytes: int
    first_audio_ms: float | None
    """Milliseconds from ``speak()`` to the first PCM chunk, or None if nothing arrived."""
    elapsed_seconds: float
    audio_seconds: float
    """Seconds of audio received (not necessarily played, if interrupted)."""


@dataclass
class _Progress:
    """Mutable counters shared between ``speak`` and its receive task."""

    chunks: int = 0
    audio_bytes: int = 0
    first_audio_at: float | None = None


class TtsStream:
    """Cartesia streaming synthesis bound to the speaker queue and the barge-in handler."""

    def __init__(
        self,
        settings: VoiceSettings,
        audio_io: AudioIO,
        barge_in: BargeInHandler,
        client: AsyncCartesia | None = None,
    ) -> None:
        """Wire the speaker sink and the interruption source. A client can be injected for tests."""
        self._settings = settings
        self._audio = audio_io
        self._barge_in = barge_in
        self._client = client or AsyncCartesia(api_key=settings.cartesia_api_key)
        self._conn: AsyncBackcompatTTSResourceConnection | None = None
        self.voice_id: str | None = settings.cartesia_voice_id
        self.voice_name: str | None = None
        self.utterances_spoken = 0

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Validate the voice, then open the long-lived WebSocket."""
        await self._resolve_voice()
        await self._connect()
        logger.info(
            "Cartesia connected (model=%s, voice=%s %s, %d Hz)",
            self._settings.cartesia_model_id, self.voice_id, self.voice_name, self._audio.speaker_sample_rate,
        )

    async def stop(self) -> None:
        """Close the WebSocket. Idempotent."""
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception as exc:  # noqa: BLE001 - closing must never raise on shutdown
                logger.debug("closing Cartesia socket: %s", exc)
            self._conn = None

    async def list_voices(self, limit: int = 50) -> list[Voice]:
        """Fetch up to ``limit`` voices from the account's library."""
        voices: list[Voice] = []
        async for voice in self._client.voices.list(limit=min(limit, 100)):
            voices.append(voice)
            if len(voices) >= limit:
                break
        return voices

    async def _resolve_voice(self) -> None:
        """Confirm the configured voice exists, or pick the first available one."""
        if self.voice_id and not _UUID_RE.match(self.voice_id):
            raise VoiceConfigError(
                f"CARTESIA_VOICE_ID={self.voice_id!r} does not look like a Cartesia voice id "
                "(those are UUIDs, e.g. a0e99841-438c-4a64-b679-ae501e7d6091). If you meant to pick "
                "an audio device, that's DELEG8_INPUT_DEVICE_INDEX / DELEG8_OUTPUT_DEVICE_INDEX instead. "
                "Leave CARTESIA_VOICE_ID empty to use your library's first voice, or run: "
                "python tests/test_voice_loop.py --list-voices"
            )
        try:
            if self.voice_id:
                voice = await self._client.voices.get(self.voice_id)
            else:
                voices = await self.list_voices(limit=1)
                if not voices:
                    raise VoiceConfigError("Your Cartesia library has no voices; set CARTESIA_VOICE_ID to a public voice id.")
                voice = voices[0]
                logger.warning("CARTESIA_VOICE_ID not set; using the first voice in your library: %s (%s)", voice.name, voice.id)
        except APIStatusError as exc:
            if exc.status_code == 401:
                raise VoiceConfigError("Cartesia rejected CARTESIA_API_KEY (HTTP 401). Create a new key at https://play.cartesia.ai") from exc
            if exc.status_code == 404:
                raise VoiceConfigError(
                    f"Cartesia voice {self.voice_id!r} not found. Run: python tests/test_voice_loop.py --list-voices"
                ) from exc
            raise VoiceConfigError(f"Cartesia returned HTTP {exc.status_code} while resolving the voice: {exc}") from exc
        self.voice_id = voice.id
        self.voice_name = voice.name

    async def _connect(self) -> None:
        """(Re)open the WebSocket connection."""
        await self.stop()
        conn = await self._client.tts.websocket()
        await conn.connect()
        self._conn = conn

    # ------------------------------------------------------------------ #
    # Speaking
    # ------------------------------------------------------------------ #

    async def speak(self, text: str, label: str = "reply") -> SpeakResult:
        """Synthesise ``text`` and play it; returns early with ``interrupted=True`` on barge-in."""
        if self._conn is None:
            raise RuntimeError("TtsStream.start() must be awaited before speak()")
        started = time.monotonic()
        progress = _Progress()
        token = self._barge_in.begin_speaking(label)
        ctx = self._conn.context()
        receiver: asyncio.Task[None] | None = None

        async def abort_generation() -> None:
            """Barge-in callback: stop consuming chunks and tell Cartesia to stop generating."""
            if receiver is not None and not receiver.done():
                receiver.cancel()
            try:
                await asyncio.wait_for(ctx.cancel(), timeout=2.0)
            except Exception as exc:  # noqa: BLE001 - the context may already be finished
                logger.debug("Cartesia cancel for %s: %s", label, exc)

        token.on_cancel(abort_generation)
        self._audio.begin_utterance()
        interrupted = False
        try:
            ctx = await self._send_with_retry(ctx, text)
            receiver = asyncio.create_task(self._receive(ctx, token, progress), name=f"deleg8-tts-{label}")
            try:
                await receiver
            except asyncio.CancelledError:
                if not token.cancelled:
                    receiver.cancel()
                    raise  # our own task is being cancelled (shutdown)
                interrupted = True
            if not interrupted:
                self._audio.end_utterance()
                await self._audio.wait_until_idle()
                interrupted = token.cancelled
        finally:
            self._barge_in.end_speaking(token)
            if token.cancelled:
                self._audio.flush_playback()
        self.utterances_spoken += 1
        elapsed = time.monotonic() - started
        rate = self._audio.speaker_sample_rate
        return SpeakResult(
            text=text,
            interrupted=interrupted,
            chunks=progress.chunks,
            audio_bytes=progress.audio_bytes,
            first_audio_ms=(progress.first_audio_at - started) * 1000 if progress.first_audio_at else None,
            elapsed_seconds=elapsed,
            audio_seconds=progress.audio_bytes / (rate * 2),
        )

    async def _send_with_retry(self, ctx: AsyncWebSocketContext, text: str) -> AsyncWebSocketContext:
        """Send the transcript; reconnect once and retry on a dead socket. Returns the live context."""
        for attempt in (1, 2):
            try:
                await ctx.send(
                    transcript=text,
                    voice={"mode": "id", "id": self.voice_id},
                    model_id=self._settings.cartesia_model_id,
                    output_format={
                        "container": "raw",
                        "encoding": "pcm_s16le",
                        "sample_rate": self._audio.speaker_sample_rate,
                    },
                    continue_=False,
                )
                return ctx
            except Exception as exc:  # noqa: BLE001 - any socket failure: reconnect once
                if attempt == 2:
                    raise TtsError(f"Cartesia send failed after reconnect: {exc}") from exc
                logger.warning("Cartesia send failed (%s); reconnecting", exc)
                await self._connect()
                assert self._conn is not None
                ctx = self._conn.context()
        raise TtsError("unreachable")

    async def _receive(self, ctx: AsyncWebSocketContext, token: CancellationToken, progress: _Progress) -> None:
        """Decode chunks onto the speaker queue until Cartesia says done (or we are cancelled)."""
        async for event in ctx.receive():
            if token.cancelled:
                return
            kind = getattr(event, "type", None)
            if kind == "chunk":
                audio = base64.b64decode(event.data)
                if progress.first_audio_at is None:
                    progress.first_audio_at = time.monotonic()
                progress.chunks += 1
                progress.audio_bytes += len(audio)
                self._audio.enqueue_playback(audio)
            elif kind == "done":
                return
            elif kind == "error":
                raise TtsError(f"Cartesia error {getattr(event, 'error_code', '?')}: {getattr(event, 'message', event)}")
