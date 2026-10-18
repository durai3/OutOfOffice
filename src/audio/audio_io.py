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

SILENCE_PEAK: int = 200
"""16-bit peak below which a frame counts as silence rather than sound.

A virtual cable with nothing playing into it measures in the tens; speech, even
distant and quiet, peaks in the thousands. The gap is wide enough that this
only has to be in the right order of magnitude.
"""


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
        self.input_device_name: str = ""
        self.output_device_name: str = ""
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

        self.frames_captured = 0
        self._peak_since_read = 0
        self._last_sound_at: float | None = None
        self._listening_since: float | None = None

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

        in_index = self._resolve_device(pa, settings.input_device_name, settings.input_device_index, want_input=True)
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

        out_index = self._resolve_device(pa, settings.output_device_name, settings.output_device_index, want_input=False)
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
        self.input_device_name = str(in_info["name"]).strip()
        self.output_device_name = str(out_info["name"]).strip()
        if in_index == out_index:
            # PortAudio cannot hold one Core Audio device open as a separate
            # input stream and output stream. The output succeeds, the input
            # reports AUHAL error -50 on stderr and then delivers zero frames
            # for the rest of the call - the delegate is completely deaf, with
            # nothing in the logs to say why. src.audio.cable_check keeps the
            # two ends apart; this is the backstop for a hand-written config.
            logger.error(
                "input and output are the same device ([%d] %s). PortAudio cannot open one device twice: "
                "the microphone will deliver no audio at all and the delegate will hear nothing. Point "
                "DELEG8_OUTPUT_DEVICE_NAME at a second virtual cable, or run "
                "`python -m src.audio.cable_check` to pick one.",
                in_index, self.input_device_name,
            )
        logger.info("mic: [%d] %s | speaker: [%d] %s", in_index, in_info["name"], out_index, out_info["name"])

    @staticmethod
    def _resolve_device(
        pa: pyaudio.PyAudio, name: str | None, index: int | None, *, want_input: bool
    ) -> int | None:
        """The index of the device to open, by name when one is configured.

        A name is resolved against the live device list on every start, which
        is the only way to stay pinned to the right device: PortAudio numbers
        devices by enumeration order, so an index saved in ``.env`` moves to a
        different device as soon as one appears or disappears. An index is used
        only when it still points at a device that has the channels this
        direction needs; otherwise it has drifted and the default is better
        than opening the wrong hardware.
        """
        channel_key = "maxInputChannels" if want_input else "maxOutputChannels"
        devices: list[tuple[int, str, int]] = []
        for candidate in range(pa.get_device_count()):
            try:
                info = pa.get_device_info_by_index(candidate)
            except OSError:
                continue
            devices.append((candidate, str(info["name"]).strip(), int(info[channel_key])))

        if name:
            wanted = name.strip()
            for candidate, device_name, channels in devices:
                if device_name == wanted and channels > 0:
                    return candidate
            for candidate, device_name, channels in devices:
                if wanted.lower() in device_name.lower() and channels > 0:
                    return candidate
            logger.warning("no %s device named %r; falling back", "input" if want_input else "output", name)
        if index is not None:
            for candidate, device_name, channels in devices:
                if candidate == index:
                    if channels > 0:
                        return candidate
                    logger.warning(
                        "%s device index %d is %r, which has no %s channels; device indexes drift when the "
                        "device list changes, so falling back to the system default",
                        "input" if want_input else "output", index, device_name,
                        "input" if want_input else "output",
                    )
                    return None
            logger.warning("no audio device with index %d is present; falling back", index)
        return None

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
        self._measure(frame)
        if self.mic_queue.full():
            try:
                self.mic_queue.get_nowait()
                self.frames_dropped += 1
            except asyncio.QueueEmpty:
                pass
        self.mic_queue.put_nowait(frame)

    def _measure(self, frame: bytes) -> None:
        """Track the input peak, so an input that hears nothing can be reported.

        A dead virtual cable is indistinguishable from a quiet room as far as
        Deepgram is concerned: both produce no transcript. The difference is
        visible only in the signal level, which is why it is measured here and
        surfaced by :class:`~src.orchestrator.VoiceOrchestrator`. ``max``/``min``
        on an :mod:`array` run at C speed, so this costs nothing at 50 frames
        a second.
        """
        self.frames_captured += 1
        if self._listening_since is None:
            self._listening_since = time.monotonic()
        if self._muted:
            return
        samples = array("h")
        samples.frombytes(frame)
        if not samples:
            return
        peak = max(abs(max(samples)), abs(min(samples)))
        if peak > self._peak_since_read:
            self._peak_since_read = peak
        if peak >= SILENCE_PEAK:
            self._last_sound_at = time.monotonic()

    def read_input_level(self) -> tuple[int, float | None]:
        """The peak seen since the last call, and seconds since the input last had sound.

        Resets the peak, so callers poll it on a fixed cadence to get a level
        meter. The second value is ``None`` while the input has never carried
        sound at all - the signature of a cable that is not connected to
        anything rather than a room that has gone quiet.
        """
        peak, self._peak_since_read = self._peak_since_read, 0
        if self._last_sound_at is None:
            return peak, None
        return peak, time.monotonic() - self._last_sound_at

    @property
    def listening_seconds(self) -> float:
        """How long mic frames have been arriving."""
        if self._listening_since is None:
            return 0.0
        return time.monotonic() - self._listening_since

    @property
    def heard_anything(self) -> bool:
        """True once any sound above the noise floor has reached the input."""
        return self._last_sound_at is not None

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
