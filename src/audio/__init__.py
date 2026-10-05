"""
DELEG8 voice pipeline: microphone -> Deepgram STT -> brain -> Cartesia TTS -> speaker.

Public surface:
    - :mod:`src.audio.config`            VoiceSettings and setup instructions.
    - :mod:`src.audio.audio_io`          PyAudio mic/speaker bridged to asyncio queues.
    - :mod:`src.audio.barge_in_handler`  Cancellation tokens; stops playback when the user talks.
    - :mod:`src.audio.stt_stream`        Deepgram streaming transcription.
    - :mod:`src.audio.tts_stream`        Cartesia streaming synthesis.
"""

from src.audio.audio_io import AudioIO, list_audio_devices
from src.audio.barge_in_handler import BargeInHandler, CancellationToken
from src.audio.config import VOICE_SETUP_INSTRUCTIONS, VoiceConfigError, VoiceSettings
from src.audio.stt_stream import SttStream, Utterance
from src.audio.tts_stream import SpeakResult, TtsStream

__all__ = [
    "AudioIO",
    "BargeInHandler",
    "CancellationToken",
    "SpeakResult",
    "SttStream",
    "TtsStream",
    "Utterance",
    "VOICE_SETUP_INSTRUCTIONS",
    "VoiceConfigError",
    "VoiceSettings",
    "list_audio_devices",
]
