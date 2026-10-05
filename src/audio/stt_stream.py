"""
Streaming speech-to-text over Deepgram's WebSocket API.

:class:`SttStream` pumps mic frames from :attr:`AudioIO.mic_queue` to
Deepgram and turns the result stream into two things:

* Every interim or final result is reported to :class:`BargeInHandler`
  immediately, so an interruption fires as soon as Deepgram hears words.
* Finalised segments are accumulated until Deepgram signals the end of an
  utterance (``speech_final`` from endpointing, or ``UtteranceEnd`` as the
  backstop); the joined text is then queued as an :class:`Utterance` for the
  orchestrator, which passes it to the Policy Engine.

The session reconnects with backoff if the socket drops.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from deepgram import AsyncDeepgramClient
from deepgram.core.api_error import ApiError
from deepgram.core.events import EventType
from deepgram.listen.v1 import (
    ListenV1Metadata,
    ListenV1Results,
    ListenV1SpeechStarted,
    ListenV1UtteranceEnd,
)
from deepgram.listen.v1.socket_client import AsyncV1SocketClient

from src.audio.audio_io import AudioIO
from src.audio.barge_in_handler import BargeInHandler
from src.audio.config import VoiceConfigError, VoiceSettings

logger = logging.getLogger("deleg8.audio.stt")

KEEPALIVE_AFTER_SECONDS: float = 5.0
"""Send a KeepAlive if no mic audio has been sent for this long."""


@dataclass(frozen=True)
class Utterance:
    """One finalised stretch of user speech."""

    text: str
    confidence: float
    started_at: float
    """Monotonic time of the first interim result."""
    finalized_at: float
    """Monotonic time the utterance was closed."""
    interrupted_bot: bool
    """True if this utterance triggered a barge-in."""

    @property
    def duration_seconds(self) -> float:
        """Wall time from first interim to finalisation."""
        return self.finalized_at - self.started_at


class SttStream:
    """Deepgram live transcription bound to the mic queue and the barge-in handler."""

    def __init__(
        self,
        settings: VoiceSettings,
        audio_io: AudioIO,
        barge_in: BargeInHandler,
        utterances: asyncio.Queue[Utterance],
        on_interim: Callable[[str], None] | None = None,
    ) -> None:
        """Wire the mic source, the interruption sink, and the utterance sink.

        ``on_interim`` receives the utterance-so-far each time Deepgram revises
        it (and an empty string once the utterance is closed), for live captions.
        """
        self._settings = settings
        self._on_interim = on_interim
        self._audio = audio_io
        self._barge_in = barge_in
        self._utterances = utterances
        self._client = AsyncDeepgramClient(api_key=settings.deepgram_api_key)
        self._running = False
        self._connected = asyncio.Event()

        self._segments: list[str] = []
        self._confidences: list[float] = []
        self._interim: str = ""
        self._utterance_started_at: float | None = None
        self._interrupted_bot = False
        self.utterances_emitted = 0
        self.request_id: str | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """Stream until :meth:`stop`; reconnects with backoff on socket failure."""
        self._running = True
        backoff = 1.0
        while self._running:
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except ApiError as exc:
                if exc.status_code == 401:
                    raise VoiceConfigError(
                        "Deepgram rejected DEEPGRAM_API_KEY (HTTP 401). Create a new key at https://console.deepgram.com"
                    ) from exc
                logger.error("Deepgram API error %s: %s", exc.status_code, exc.body)
            except Exception as exc:  # noqa: BLE001 - keep the mic alive across transient failures
                logger.error("STT session ended: %s", exc)
            if self._running:
                self._connected.clear()
                logger.info("reconnecting to Deepgram in %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    async def stop(self) -> None:
        """Ask the stream to finish; :meth:`run` returns shortly after."""
        self._running = False

    async def wait_connected(self, timeout: float = 10.0) -> None:
        """Block until the socket is open (raises on timeout)."""
        await asyncio.wait_for(self._connected.wait(), timeout)

    @property
    def connected(self) -> bool:
        """True while the Deepgram socket is open."""
        return self._connected.is_set()

    # ------------------------------------------------------------------ #
    # One socket session
    # ------------------------------------------------------------------ #

    async def _session(self) -> None:
        """Open a socket, run the mic pump and the listener until one of them ends."""
        settings = self._settings
        async with self._client.listen.v1.connect(
            model=settings.deepgram_model,
            encoding="linear16",
            sample_rate=self._audio.mic_sample_rate,
            channels=1,
            interim_results="true",
            smart_format="true",
            punctuate="true",
            vad_events="true",
            endpointing=settings.endpointing_ms,
            utterance_end_ms=settings.utterance_end_ms,
        ) as socket:
            socket.on(EventType.OPEN, self._on_open)
            socket.on(EventType.MESSAGE, self._on_message)
            socket.on(EventType.ERROR, self._on_error)
            socket.on(EventType.CLOSE, self._on_close)
            listener = asyncio.create_task(socket.start_listening(), name="deleg8-stt-listen")
            pump = asyncio.create_task(self._pump(socket), name="deleg8-stt-pump")
            done, pending = await asyncio.wait({listener, pump}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            for task in done:
                task.result()  # surface the failure that ended the session

    async def _pump(self, socket: AsyncV1SocketClient) -> None:
        """Forward mic frames; send KeepAlive during silence; CloseStream on stop."""
        while self._running:
            try:
                frame = await asyncio.wait_for(self._audio.mic_queue.get(), timeout=KEEPALIVE_AFTER_SECONDS)
            except asyncio.TimeoutError:
                await socket.send_keep_alive()
                continue
            await socket.send_media(frame)
        await socket.send_close_stream()

    # ------------------------------------------------------------------ #
    # Deepgram events (all run on the event loop)
    # ------------------------------------------------------------------ #

    def _on_open(self, _: object) -> None:
        self._connected.set()
        logger.info("Deepgram connected (model=%s, %d Hz)", self._settings.deepgram_model, self._audio.mic_sample_rate)

    def _on_close(self, _: object) -> None:
        self._connected.clear()
        logger.info("Deepgram socket closed")

    def _on_error(self, error: object) -> None:
        logger.error("Deepgram socket error: %s", error)

    async def _on_message(self, message: object) -> None:
        """Route a decoded Deepgram message."""
        if isinstance(message, ListenV1Results):
            await self._on_results(message)
        elif isinstance(message, ListenV1UtteranceEnd):
            if self._segments:
                await self._emit()
        elif isinstance(message, ListenV1SpeechStarted):
            logger.debug("speech started at %.2fs", message.timestamp or 0.0)
        elif isinstance(message, ListenV1Metadata):
            self.request_id = message.request_id
            logger.debug("Deepgram request %s", message.request_id)

    async def _on_results(self, results: ListenV1Results) -> None:
        """Handle interim and final transcripts."""
        alternatives = results.channel.alternatives if results.channel else None
        alt = alternatives[0] if alternatives else None
        text = (alt.transcript or "").strip() if alt is not None else ""

        if text:
            word_count = len(alt.words) if alt is not None and alt.words else len(text.split())
            if self._utterance_started_at is None:
                self._utterance_started_at = time.monotonic()
            source = "final" if results.is_final else "interim"
            if await self._barge_in.on_user_speech(text, word_count=word_count, source=source):
                self._interrupted_bot = True
            if results.is_final:
                self._segments.append(text)
                self._confidences.append(float(alt.confidence or 0.0) if alt is not None else 0.0)
                self._interim = ""
            else:
                self._interim = text
            self._notify_interim(" ".join([*self._segments, self._interim]).strip())

        if results.is_final and results.speech_final and self._segments:
            await self._emit()

    async def _emit(self) -> None:
        """Close the current utterance and hand it to the orchestrator."""
        text = " ".join(self._segments).strip()
        confidence = sum(self._confidences) / len(self._confidences) if self._confidences else 0.0
        now = time.monotonic()
        utterance = Utterance(
            text=text,
            confidence=confidence,
            started_at=self._utterance_started_at or now,
            finalized_at=now,
            interrupted_bot=self._interrupted_bot,
        )
        self._segments.clear()
        self._confidences.clear()
        self._interim = ""
        self._utterance_started_at = None
        self._interrupted_bot = False
        self.utterances_emitted += 1
        self._notify_interim("")
        await self._utterances.put(utterance)

    def _notify_interim(self, text: str) -> None:
        if self._on_interim is None:
            return
        try:
            self._on_interim(text)
        except Exception:  # noqa: BLE001 - a caption observer must never break transcription
            logger.exception("interim transcript observer failed")
