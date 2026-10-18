"""
Configuration for the voice pipeline, read from the environment.

Everything the audio layer can be tuned with lives in :class:`VoiceSettings`
so the pipeline modules never touch ``os.environ`` themselves.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

VOICE_SETUP_INSTRUCTIONS: str = """\
How to configure the DELEG8 voice pipeline
------------------------------------------
1. Deepgram (streaming speech-to-text)
   a. Sign up at https://console.deepgram.com and open "API Keys".
   b. Create a key and copy it into .env as   DEEPGRAM_API_KEY=<key>

2. Cartesia (streaming text-to-speech)
   a. Sign up at https://play.cartesia.ai and open "API Keys".
   b. Create a key and copy it into .env as   CARTESIA_API_KEY=<key>
   c. Optional: choose a voice with   python tests/test_voice_loop.py --list-voices
      and set CARTESIA_VOICE_ID=<id>. If unset, the first voice in your library
      is used and printed at startup.

3. Audio hardware
   a. macOS: brew install portaudio   (PyAudio links against it)
   b. Grant the terminal microphone access when macOS asks.
   c. Wear headphones. There is no echo cancellation yet, so on open speakers
      the bot hears its own voice and interrupts itself. If you must use
      speakers, set DELEG8_HALF_DUPLEX=true (mic muted while the bot talks,
      which disables barge-in).
"""


class VoiceConfigError(RuntimeError):
    """Raised when the voice pipeline is misconfigured."""


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def _env_optional_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise VoiceConfigError(f"{name} must be an integer (got {raw!r}).") from exc


def _env_int(name: str, default: int, *, minimum: int) -> int:
    value = _env_optional_int(name)
    if value is None:
        return default
    if value < minimum:
        raise VoiceConfigError(f"{name} must be >= {minimum} (got {value}).")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise VoiceConfigError(f"{name} must be true or false (got {raw!r}).")


@dataclass(frozen=True)
class VoiceSettings:
    """All knobs for the voice pipeline. Frozen so components can share it safely."""

    deepgram_api_key: str
    cartesia_api_key: str
    deepgram_model: str = "nova-3"
    cartesia_model_id: str = "sonic-3"
    cartesia_voice_id: str | None = None
    mic_sample_rate: int = 16000
    """Preferred mic rate. The device may not support it; AudioIO reports the actual rate."""
    speaker_sample_rate: int = 24000
    """Preferred speaker rate; must be one Cartesia can produce (8k/16k/22.05k/24k/44.1k/48k)."""
    frame_ms: int = 20
    """Audio frame size for both directions. Also the barge-in reaction granularity."""
    input_device_index: int | None = None
    output_device_index: int | None = None
    input_device_name: str | None = None
    """Device name for the bot's ears, preferred over the index. PortAudio
    renumbers devices whenever one appears or disappears (a Continuity-Camera
    iPhone microphone is enough), so an index written to ``.env`` can silently
    come to mean a different device - or one with no output channels at all -
    on the next run. A name does not drift."""
    output_device_name: str | None = None
    """Device name for the bot's mouth, preferred over the index; see above."""
    verify_cables: bool = True
    """Measure each virtual cable before the call by playing a tone into it and
    recording it back (:mod:`src.audio.cable_check`). A cable whose driver is
    installed but not loaded enumerates and opens normally while returning
    silence forever, which otherwise shows up only as a meeting in which the
    delegate never hears a single word."""
    auto_route: bool = True
    """Point the system default output/input at the chosen cables, since that is
    what Chromium plays into and records from."""
    endpointing_ms: int = 300
    """Deepgram: silence after speech that finalises an utterance."""
    utterance_end_ms: int = 1000
    """Deepgram: backstop that fires UtteranceEnd when endpointing did not."""
    barge_in_min_words: int = 1
    """Interrupt the bot once the user has said at least this many words."""
    half_duplex: bool = False
    """Mute the mic while the bot speaks (no barge-in, but no self-echo on speakers)."""
    speaker_label: str = "Counterpart"
    """Who the microphone represents. Must not be the principal or the brain stays silent."""
    meeting_id: str = "voice-session"
    user_id: str = "durai"
    participants: tuple[str, ...] = ("Durai Kumaran", "Counterpart")

    @staticmethod
    def is_configured() -> bool:
        """True when both platform provider keys are present in the environment."""
        return bool(os.getenv("DEEPGRAM_API_KEY", "").strip()) and bool(os.getenv("CARTESIA_API_KEY", "").strip())

    @classmethod
    def route_from_env(cls) -> VoiceSettings:
        """Only the audio-routing knobs, with placeholder provider keys.

        :mod:`src.audio.cable_check` needs the device configuration and nothing
        else, and must be runnable to diagnose a dead cable *before* the
        Deepgram and Cartesia keys are in place - a silent microphone is
        exactly the kind of problem someone hits while still setting up.
        """
        return cls(
            deepgram_api_key="unset",
            cartesia_api_key="unset",
            input_device_index=_env_optional_int("DELEG8_INPUT_DEVICE_INDEX"),
            output_device_index=_env_optional_int("DELEG8_OUTPUT_DEVICE_INDEX"),
            input_device_name=os.getenv("DELEG8_INPUT_DEVICE_NAME", "").strip() or None,
            output_device_name=os.getenv("DELEG8_OUTPUT_DEVICE_NAME", "").strip() or None,
            verify_cables=_env_bool("DELEG8_AUDIO_PREFLIGHT", True),
            auto_route=_env_bool("DELEG8_AUDIO_AUTOROUTE", True),
        )

    @classmethod
    def from_env(cls, *, deepgram_key: str | None = None, cartesia_key: str | None = None) -> VoiceSettings:
        """Build settings from the environment, with explicit keys taking precedence.

        The explicit keys are how a tenant's *own* Deepgram and Cartesia
        credentials are used: one process serves many users, so the keys cannot
        come from a single pair of environment variables, but every other knob
        here is a deployment setting and still does.

        Raises:
            VoiceConfigError: listing missing keys, with setup instructions.
        """
        deepgram_key = (deepgram_key or os.getenv("DEEPGRAM_API_KEY", "")).strip()
        cartesia_key = (cartesia_key or os.getenv("CARTESIA_API_KEY", "")).strip()
        missing = [n for n, v in (("DEEPGRAM_API_KEY", deepgram_key), ("CARTESIA_API_KEY", cartesia_key)) if not v]
        if missing:
            raise VoiceConfigError(f"Missing environment variable(s): {', '.join(missing)}.\n\n{VOICE_SETUP_INSTRUCTIONS}")

        speaker_label = _env_str("DELEG8_VOICE_SPEAKER", "Counterpart")
        user_id = _env_str("DELEG8_VOICE_USER_ID", "durai")
        return cls(
            deepgram_api_key=deepgram_key,
            cartesia_api_key=cartesia_key,
            deepgram_model=_env_str("DEEPGRAM_MODEL", "nova-3"),
            cartesia_model_id=_env_str("CARTESIA_MODEL_ID", "sonic-3"),
            cartesia_voice_id=os.getenv("CARTESIA_VOICE_ID", "").strip() or None,
            input_device_index=_env_optional_int("DELEG8_INPUT_DEVICE_INDEX"),
            output_device_index=_env_optional_int("DELEG8_OUTPUT_DEVICE_INDEX"),
            input_device_name=os.getenv("DELEG8_INPUT_DEVICE_NAME", "").strip() or None,
            output_device_name=os.getenv("DELEG8_OUTPUT_DEVICE_NAME", "").strip() or None,
            verify_cables=_env_bool("DELEG8_AUDIO_PREFLIGHT", True),
            auto_route=_env_bool("DELEG8_AUDIO_AUTOROUTE", True),
            endpointing_ms=_env_int("DELEG8_STT_ENDPOINTING_MS", 300, minimum=10),
            utterance_end_ms=_env_int("DELEG8_STT_UTTERANCE_END_MS", 1000, minimum=100),
            barge_in_min_words=_env_int("DELEG8_BARGE_IN_MIN_WORDS", 1, minimum=1),
            half_duplex=_env_bool("DELEG8_HALF_DUPLEX", False),
            speaker_label=speaker_label,
            meeting_id=_env_str("DELEG8_VOICE_MEETING_ID", "voice-session"),
            user_id=user_id,
            participants=("Durai Kumaran", speaker_label),
        )
