"""
Standalone verification for Sprint 3: the low-latency voice loop.

Starts the microphone, the speaker, Deepgram STT, Cartesia TTS, the Policy
Engine and (if configured) the Telegram escalation bridge, then listens
until Ctrl+C. Try these three things:

    1. Say:  "What is the status of the API?"
       -> GREEN topic with a grounded answer; the bot replies at once.
    2. Say:  "Can you commit to delivering the security audit by this Friday?"
       -> YELLOW topic; the bot stays silent, your phone buzzes, and it only
          speaks after you tap Approve / Reject / Decline Politely.
    3. While the bot is talking, say:  "Wait, stop!"
       -> "[INTERRUPTED]" is printed and the audio cuts immediately.

Wear headphones: there is no echo cancellation, so on open speakers the bot
hears itself. On speakers use --half-duplex (mic muted while the bot talks,
which disables barge-in).

Usage::

    python tests/test_voice_loop.py                 # run the loop, Ctrl+C to stop
    python tests/test_voice_loop.py --list-devices  # PyAudio device indexes
    python tests/test_voice_loop.py --list-voices   # Cartesia voices for CARTESIA_VOICE_ID
    python tests/test_voice_loop.py --half-duplex   # open speakers, no barge-in
    python tests/test_voice_loop.py --setup-help    # how to get the API keys
    python tests/test_voice_loop.py --no-briefing   # skip the post-meeting briefing on exit

Sprint 4: answers are grounded in the ingested documents (run
``python -m src.knowledge.ingest`` first), every utterance updates the live
meeting memory ([MEMORY] lines), and when you stop the loop (or send
/conclude to the bot) the executive briefing is pushed to Telegram.

Exit codes: 0 = clean stop, 1 = runtime failure, 2 = configuration problem.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import os
import signal
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from src.audio import (  # noqa: E402
    VOICE_SETUP_INSTRUCTIONS,
    AudioIO,
    BargeInHandler,
    SttStream,
    TtsStream,
    Utterance,
    VoiceConfigError,
    VoiceSettings,
    list_audio_devices,
)
from src.brain import (  # noqa: E402
    KnowledgeSource,
    PolicyEngine,
    ProfileKnowledge,
    profile_store,
)
from src.escalation import (  # noqa: E402
    EscalationBridge,
    EscalationConfigError,
    NullEscalationBridge,
    TelegramEscalationBridge,
    TelegramSettings,
)
from src.knowledge import (  # noqa: E402
    KnowledgeConfigError,
    KnowledgeSettings,
    KnowledgeStore,
    build_knowledge_source,
)
from src.memory import MeetingStateRegistry, PostMeetingReporter  # noqa: E402
from src.orchestrator import VoiceOrchestrator  # noqa: E402
from src.turn_processor import TurnProcessor  # noqa: E402

GUIDE: str = """\
Try these:
  1. "What is the status of the API?"                                 -> instant reply
  2. "Can you commit to delivering the security audit by this Friday?" -> silent, Telegram push, speaks after you tap
  3. While the bot talks: "Wait, stop!"                                -> [INTERRUPTED], audio cuts
"""


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list-devices", action="store_true", help="print PyAudio devices and exit")
    parser.add_argument("--list-voices", action="store_true", help="print Cartesia voices and exit")
    parser.add_argument("--setup-help", action="store_true", help="print API key instructions and exit")
    parser.add_argument("--half-duplex", action="store_true", help="mute the mic while the bot speaks (no barge-in)")
    parser.add_argument("--min-words", type=int, default=None, help="words needed to trigger barge-in (default from env, 1)")
    parser.add_argument("--speaker", default=None, help="name the microphone represents in the meeting")
    parser.add_argument("--log-level", default=os.getenv("DELEG8_LOG_LEVEL", "WARNING"), help="python log level for component logs")
    parser.add_argument("--no-briefing", action="store_true", help="do not send the post-meeting briefing when the loop stops")
    return parser.parse_args(argv)


def print_devices() -> None:
    """List audio devices with their indexes."""
    print("PyAudio devices (set DELEG8_INPUT_DEVICE_INDEX / DELEG8_OUTPUT_DEVICE_INDEX):")
    for device in list_audio_devices():
        kind = []
        if device.max_input_channels:
            kind.append(f"in={device.max_input_channels}")
        if device.max_output_channels:
            kind.append(f"out={device.max_output_channels}")
        print(f"  [{device.index}] {device.name}  ({', '.join(kind)}, {device.default_sample_rate} Hz)")


async def print_voices(settings: VoiceSettings) -> int:
    """List Cartesia voices so the user can pick CARTESIA_VOICE_ID."""
    audio = AudioIO(settings)  # not started; TtsStream only needs it for the sample rate at speak time
    tts = TtsStream(settings, audio, BargeInHandler(audio))
    try:
        voices = await tts.list_voices(limit=50)
    except VoiceConfigError as exc:
        print(f"\n{exc}")
        return 2
    print("Cartesia voices (set CARTESIA_VOICE_ID to one of these ids):")
    for voice in voices:
        print(f"  {voice.id}  {voice.name}  [{voice.language}]  {voice.description or ''}".rstrip())
    return 0


async def open_knowledge() -> tuple[KnowledgeSource, KnowledgeStore | None]:
    """Document index + profile facts; profile facts alone if the index cannot be opened."""
    try:
        store = KnowledgeStore(KnowledgeSettings.from_env())
        await store.open_async()
    except (KnowledgeConfigError, Exception) as exc:  # noqa: BLE001 - degrade, do not abort the voice session
        print(f"[WARN] document index unavailable ({exc}); answers come from profile facts only")
        return ProfileKnowledge(), None
    knowledge, _ = build_knowledge_source(store)
    return knowledge, store


def build_bridge() -> EscalationBridge:
    """Telegram if configured, else a null bridge with a clear warning."""
    if TelegramSettings.is_configured():
        return TelegramEscalationBridge(TelegramSettings.from_env())
    print(
        "[WARN] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set: escalations will not reach your phone; "
        "the bot will speak the fallback line instead. Run: python tests/test_escalation.py --setup-help"
    )
    return NullEscalationBridge("Telegram not configured")


async def run_loop(settings: VoiceSettings, *, briefing: bool = True) -> int:
    """Assemble the pipeline and run it until Ctrl+C, then brief the principal."""
    bridge = build_bridge()
    knowledge, knowledge_store = await open_knowledge()
    engine = PolicyEngine(profile_store, knowledge)
    memory = MeetingStateRegistry()
    turns = TurnProcessor(engine=engine, store=profile_store, bridge=bridge, memory=memory)
    reporter = PostMeetingReporter(bridge, store=profile_store)

    async def conclude_command(args: str) -> str:
        reports = await reporter.conclude_all(memory)
        return "Concluded " + ", ".join(r.meeting_id for r in reports) + "." if reports else "No active meeting."

    if isinstance(bridge, TelegramEscalationBridge):
        bridge.register_command("conclude", conclude_command)

    audio = AudioIO(settings)
    barge_in = BargeInHandler(audio, min_words=settings.barge_in_min_words)
    utterances: asyncio.Queue[Utterance] = asyncio.Queue()
    stt = SttStream(settings, audio, barge_in, utterances)
    tts = TtsStream(settings, audio, barge_in)
    orchestrator = VoiceOrchestrator(settings, audio, stt, tts, barge_in, engine, turns, utterances)

    print("DELEG8 Sprint 3 verification: live voice loop (with Sprint 4 knowledge + memory)")
    print(f"  STT: Deepgram {settings.deepgram_model} | TTS: Cartesia {settings.cartesia_model_id} | escalation: {bridge.describe()}")
    print(f"  knowledge: {knowledge.describe()} | briefing: {reporter.describe()}")
    print(f"  mode: {'half-duplex (mic muted while speaking, no barge-in)' if settings.half_duplex else 'full-duplex (wear headphones)'}"
          f", barge-in after {settings.barge_in_min_words} word(s), mic speaker label {settings.speaker_label!r}")
    print(GUIDE)

    try:
        await bridge.start()
    except EscalationConfigError as exc:
        print(f"\n{exc}")
        return 2

    loop = asyncio.get_running_loop()
    runner = asyncio.create_task(orchestrator.run(), name="deleg8-voice")
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, runner.cancel)

    exit_code = 0
    try:
        await runner
    except asyncio.CancelledError:
        print("\n[STOP] Shutting down...", flush=True)
    except VoiceConfigError as exc:
        print(f"\n{exc}")
        exit_code = 2
    except Exception as exc:  # noqa: BLE001 - report anything else and exit non-zero
        logging.getLogger("deleg8").exception("voice loop failed")
        print(f"\n[ERROR] {exc}")
        exit_code = 1
    finally:
        try:
            if briefing:
                for state in memory.active():
                    if state.is_empty:
                        memory.remove(state.meeting_id)
                        continue
                    print(f"[BRIEFING] concluding {state.meeting_id} via {reporter.describe()}...", flush=True)
                    report = await reporter.conclude(state, registry=memory)
                    status = "delivered" if report.receipt.delivered else f"not delivered ({report.receipt.detail})"
                    print(f"[BRIEFING] {status}\n" + "\n".join("  | " + line for line in report.text.splitlines()), flush=True)
        finally:
            await bridge.stop()
            if knowledge_store is not None:
                knowledge_store.close()
    print(f"[SESSION] {orchestrator.stats.summary()}")
    return exit_code


def main(argv: list[str]) -> int:
    """Entry point."""
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.setup_help:
        print(VOICE_SETUP_INSTRUCTIONS)
        return 0
    if args.list_devices:
        print_devices()
        return 0

    try:
        settings = VoiceSettings.from_env()
    except VoiceConfigError as exc:
        print(f"\nConfiguration problem: {exc}")
        return 2
    overrides: dict[str, object] = {}
    if args.half_duplex:
        overrides["half_duplex"] = True
    if args.min_words is not None:
        overrides["barge_in_min_words"] = max(1, args.min_words)
    if args.speaker:
        overrides["speaker_label"] = args.speaker
        overrides["participants"] = ("Durai Kumaran", args.speaker)
    if overrides:
        settings = dataclasses.replace(settings, **overrides)

    if args.list_voices:
        return asyncio.run(print_voices(settings))
    return asyncio.run(run_loop(settings, briefing=not args.no_briefing))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
