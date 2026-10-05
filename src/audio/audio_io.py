"""
Microphone and speaker I/O bridged to asyncio.

PortAudio runs its own real-time threads. Nothing in this module touches
asyncio state from those threads directly:

* Mic: PortAudio invokes :meth:`AudioIO._mic_callback` on its thread. The
  callback does exactly one thing: ``loop.call_soon_threadsafe(...)`` to hand
  the frame to the event loop, which enqueues it on :attr:`AudioIO.mic_queue`.
* Speaker: the event loop pulls chunks from :attr:`AudioIO.playback_queue`
  and writes them one frame (20 ms) at a time through a single-worker
  executor, so blocking ``stream.write`` calls never stall the loop and
  always run in order.

Barge-in latency: :meth:`AudioIO.flush_playback` drops everything queued and
bumps a generation counter that the playback loop checks between frames, so
sound stops within roughly one frame plus the device buffer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from array import array
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pyaudio

from src.audio.config import VoiceSettings

logger = logging.getLogger("deleg8.audio.io")

CARTESIA_SAMPLE_RATES: tuple[int, ...] = (24000, 48000, 44100, 22050, 16000, 8000)
"""Output rates Cartesia can produce, in order of preference for the speaker."""

MIC_SAMPLE_RATES: tuple[int, ...] = (16000, 48000, 44100, 32000, 24000)
"""Mic rates to try, in order of preference. Deepgram accepts any of them."""


@dataclass(frozen=True)
class AudioDevice:
    """One PortAudio device."""

    index: int
    name: str
    max_input_channels: int
    max_output_channels: int
    default_sample_rate: int


def list_audio_devices() -> list[AudioDevice]:
    """Enumerate PortAudio devices (blocking; call from a thread or before the loop starts)."""
    pa = pyaudio.PyAudio()
    try:
        devices = []
        for index in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(index)
            devices.append(
                AudioDevice(
                    index=index,
                    name=str(info["name"]),
                    max_input_channels=int(info["maxInputChannels"]),
                    max_output_channels=int(info["maxOutputChannels"]),
                    default_sample_rate=int(info["defaultSampleRate"]),
                )
            )
        return devices
    finally:
        pa.terminate()


def _mono_to_stereo(frame: bytes) -> bytes:
    """Duplicate 16-bit mono samples into interleaved stereo (C-speed slice assignment)."""
    mono = array("h")
    mono.frombytes(frame)
    stereo = array("h", bytes(len(frame) * 2))
    stereo[0::2] = mono
    stereo[1::2] = mono
    return stereo.tobytes()


class AudioIO:
    """Owns the PyAudio streams and the two asyncio queues that connect them to the pipeline.

    Lifecycle: ``await start()`` inside a running event loop, ``await stop()``
    on shutdown. All public methods except :func:`list_audio_devices` must be
    called from the event-loop thread.
    """

    _END_OF_UTTERANCE: None = None

    def __init__(self, settings: VoiceSettings, mic_queue_seconds: float = 5.0) -> None:
        """Prepare queues and state; no device is opened until :meth:`start`."""
        self._settings = settings
        frames_per_second = 1000 // settings.frame_ms
        self.mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=int(frames_per_second * mic_queue_seconds))
        self.playback_queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._pa: pyaudio.PyAudio | None = None
        self._in_stream: pyaudio.Stream | None = None
        self._out_stream: pyaudio.Stream | None = None
        self._speaker_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="deleg8-speaker")
        self._playback_task: asyncio.Task[None] | None = None

        self.mic_sample_rate: int = settings.mic_sample_rate
        self.speaker_sample_rate: int = settings.speaker_sample_rate
        self._speaker_channels: int = 1
        self._mic_frame_bytes: int = 0
        self._speaker_frame_bytes: int = 0
        self._silence: bytes = b""

        self._generation = 0
        self._speaking = False
        self._idle = asyncio.Event()
        self._idle.set()
        self._muted = False
        self.frames_dropped = 0
        self.bytes_played = 0
        self.last_playback_started_at: float | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Open the mic and speaker and begin streaming. Must run inside the event loop."""
        if self._pa is not None:
            return
        self._loop = asyncio.get_running_loop()
        await self._loop.run_in_executor(None, self._open_streams)
        self._playback_task = asyncio.create_task(self._playback_loop(), name="deleg8-playback")
        assert self._in_stream is not None
        self._in_stream.start_stream()
        logger.info(
            "audio started: mic %d Hz mono, speaker %d Hz %s, %d ms frames",
            self.mic_sample_rate, self.speaker_sample_rate,
            "stereo" if self._speaker_channels == 2 else "mono", self._settings.frame_ms,
        )

    async def stop(self) -> None:
        """Stop streams and release PortAudio. Idempotent."""
        if self._playback_task is not None:
            self._playback_task.cancel()
            try:
                await self._playback_task
            except asyncio.CancelledError:
                pass
            self._playback_task = None
        self.flush_playback()
        if self._loop is not None and self._pa is not None:
            await self._loop.run_in_executor(None, self._close_streams)
        self._speaker_executor.shutdown(wait=True, cancel_futures=True)
        logger.info("audio stopped (%d mic frames dropped, %d bytes played)", self.frames_dropped, self.bytes_played)

    def _open_streams(self) -> None:
        """Blocking: pick devices and rates, open both streams. Runs in a worker thread."""
        pa = pyaudio.PyAudio()
        self._pa = pa
        settings = self._settings

        in_index = settings.input_device_index
        if in_index is None:
            in_index = int(pa.get_default_input_device_info()["index"])
        in_info = pa.get_device_info_by_index(in_index)
        self.mic_sample_rate = self._pick_rate(pa, in_index, "input", settings.mic_sample_rate, MIC_SAMPLE_RATES, 1)
        mic_frames = self.mic_sample_rate * settings.frame_ms // 1000
        self._mic_frame_bytes = mic_frames * 2
        self._silence = bytes(self._mic_frame_bytes)
        self._in_stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self.mic_sample_rate,
            input=True,
            input_device_index=in_index,
            frames_per_buffer=mic_frames,
            stream_callback=self._mic_callback,
            start=False,
        )

        out_index = settings.output_device_index
        if out_index is None:
            out_index = int(pa.get_default_output_device_info()["index"])
        out_info = pa.get_device_info_by_index(out_index)
        self._speaker_channels = 2 if int(out_info["maxOutputChannels"]) >= 2 else 1
        self.speaker_sample_rate = self._pick_rate(
            pa, out_index, "output", settings.speaker_sample_rate, CARTESIA_SAMPLE_RATES, self._speaker_channels
        )
        speaker_frames = self.speaker_sample_rate * settings.frame_ms // 1000
        self._speaker_frame_bytes = speaker_frames * 2  # mono bytes per frame, before upmix
        self._out_stream = pa.open(
            format=pyaudio.paInt16,
            channels=self._speaker_channels,
            rate=self.speaker_sample_rate,
            output=True,
            output_device_index=out_index,
            frames_per_buffer=speaker_frames,
        )
        logger.info("mic: [%d] %s | speaker: [%d] %s", in_index, in_info["name"], out_index, out_info["name"])

    @staticmethod
    def _pick_rate(
        pa: pyaudio.PyAudio, device: int, direction: str, preferred: int, candidates: tuple[int, ...], channels: int
    ) -> int:
        """Return the first sample rate the device accepts, preferring ``preferred``."""
        for rate in (preferred, *candidates):
            try:
                if direction == "input":
                    pa.is_format_supported(rate, input_device=device, input_channels=channels, input_format=pyaudio.paInt16)
                else:
                    pa.is_format_supported(rate, output_device=device, output_channels=channels, output_format=pyaudio.paInt16)
                return rate
            except ValueError:
                continue
        raise RuntimeError(f"{direction} device {device} supports none of the sample rates {candidates}")

    def _close_streams(self) -> None:
        """Blocking: stop and close streams, terminate PortAudio."""
        for stream in (self._in_stream, self._out_stream):
            if stream is None:
                continue
            try:
                if stream.is_active():
                    stream.stop_stream()
                stream.close()
            except OSError as exc:
                logger.warning("closing audio stream failed: %s", exc)
        self._in_stream = self._out_stream = None
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None

    # ------------------------------------------------------------------ #
    # Microphone (PortAudio thread -> event loop)
    # ------------------------------------------------------------------ #

    def _mic_callback(
        self, in_data: bytes | None, frame_count: int, time_info: dict[str, float], status: int
    ) -> tuple[None, int]:
        """PortAudio thread. Hands the frame to the loop; never touches asyncio directly."""
        if in_data is not None and self._loop is not None and not self._loop.is_closed():
            frame = self._silence if self._muted else bytes(in_data)
            self._loop.call_soon_threadsafe(self._enqueue_mic, frame)
        return (None, pyaudio.paContinue)

    def _enqueue_mic(self, frame: bytes) -> None:
        """Event-loop thread. Drop the oldest frame on overflow so latency never grows."""
        if self.mic_queue.full():
            try:
                self.mic_queue.get_nowait()
                self.frames_dropped += 1
            except asyncio.QueueEmpty:
                pass
        self.mic_queue.put_nowait(frame)

    def set_muted(self, muted: bool) -> None:
        """Replace mic audio with silence (keeps the STT socket alive) when ``muted``."""
        if muted != self._muted:
            self._muted = muted
            logger.debug("mic %s", "muted" if muted else "live")

    @property
    def muted(self) -> bool:
        """True while mic frames are being replaced with silence."""
        return self._muted

    # ------------------------------------------------------------------ #
    # Speaker (event loop -> single worker thread)
    # ------------------------------------------------------------------ #

    def begin_utterance(self) -> None:
        """Mark that a bot utterance is about to be queued; clears the idle flag synchronously."""
        self._speaking = True
        self._idle.clear()
        self.last_playback_started_at = None

    def enqueue_playback(self, chunk: bytes) -> None:
        """Queue PCM s16le mono audio at :attr:`speaker_sample_rate` for playback."""
        if chunk:
            self.playback_queue.put_nowait(chunk)

    def end_utterance(self) -> None:
        """Queue the end-of-utterance marker; the idle flag is set once everything before it played."""
        self.playback_queue.put_nowait(self._END_OF_UTTERANCE)

    def flush_playback(self) -> int:
        """Discard all queued audio and stop the in-flight frame loop. Returns bytes dropped.

        Synchronous and safe to call from any coroutine on the loop thread. The
        playback loop notices the generation change before its next frame.
        """
        self._generation += 1
        dropped = 0
        while True:
            try:
                item = self.playback_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            dropped += len(item) if item else 0
        self._speaking = False
        self._idle.set()
        return dropped

    @property
    def is_speaking(self) -> bool:
        """True from :meth:`begin_utterance` until playback drains or is flushed."""
        return self._speaking

    async def wait_until_idle(self) -> None:
        """Block until the speaker has finished (or been flushed)."""
        await self._idle.wait()

    async def _playback_loop(self) -> None:
        """Pull chunks, split into frames, write through the single-worker executor."""
        assert self._loop is not None
        while True:
            item = await self.playback_queue.get()
            if item is self._END_OF_UTTERANCE:
                if self.playback_queue.empty():
                    self._speaking = False
                    self._idle.set()
                continue
            generation = self._generation
            frame_bytes = self._speaker_frame_bytes
            for offset in range(0, len(item), frame_bytes):
                if generation != self._generation:
                    break  # flushed by barge-in
                frame = item[offset:offset + frame_bytes]
                if len(frame) < frame_bytes:
                    frame = frame + bytes(frame_bytes - len(frame))
                if self.last_playback_started_at is None:
                    self.last_playback_started_at = time.monotonic()
                try:
                    await self._loop.run_in_executor(self._speaker_executor, self._write_frame, frame)
                except OSError as exc:
                    logger.error("speaker write failed: %s", exc)
                    break
                self.bytes_played += len(frame)

    def _write_frame(self, frame: bytes) -> None:
        """Worker thread: blocking write of one mono frame (upmixed if the device is stereo)."""
        stream = self._out_stream
        if stream is None:
            return
        stream.write(_mono_to_stereo(frame) if self._speaker_channels == 2 else frame)
