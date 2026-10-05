"""
Unified system diagnostic: proves DELEG8's Sprint 1-5 components are
genuinely wired together, not just individually importable.

Six stages, run in order and never aborting early (a failed stage is
reported and the rest still run, so one dashboard shows the whole system):

    1. Environment & Credentials  - .env keys present, shaped correctly, and
       (unless --skip-live-keys) actually accepted by Deepgram/Cartesia.
    2. Audio Device & Virtual Cable - enumerates PortAudio devices via the
       same path src/audio/device_setup.py uses, checks the configured
       DELEG8_INPUT/OUTPUT_DEVICE_INDEX exist and are distinct, and (unless
       --skip-audio-open) actually opens non-blocking PyAudio streams on them
       at 16 kHz and 48 kHz.
    3. Policy Engine State Machine - the three Sprint 1 scenarios
       (AUTO_REPLY / ESCALATE / SILENT) through the real PolicyEngine.
    4. ChromaDB RAG Retrieval - opens the real data/chroma_db/ index, builds a
       query out of a chunk actually stored there (so the assertion is not
       hostage to what happens to be ingested), and checks it comes back
       GROUNDED above the configured similarity threshold, while an unrelated
       query is rejected as UNKNOWN_CONTEXT.
    5. Telegram Escalation Live Ping - sends a real Approve/Reject card to
       your configured chat and waits up to --telegram-timeout seconds.
       Delivery succeeding is a PASS; a tap is a bonus, not a requirement.
    6. Orchestrator Pipeline Integration - runs a real asyncio.Queue[Utterance]
       through TurnProcessor concurrently with a 50ms heartbeat to prove nothing
       blocks the event loop (the same pattern src/gateway/session.py uses to
       run the voice loop and the Meet health loop side by side), then (unless
       --skip-browser) launches a real headless Chromium to prove Playwright
       itself works.

Usage::

    python tests/diagnose_system.py                    # all 6 stages
    python tests/diagnose_system.py --stage 4          # just the RAG check
    python tests/diagnose_system.py --skip-telegram    # no message sent
    python tests/diagnose_system.py -y                 # no confirmation prompt before Stage 5
    python tests/diagnose_system.py --skip-live-keys --skip-audio-open --skip-browser --skip-telegram
                                                        # fully offline structural pass

Stage 5 and a live server/bot sharing the same TELEGRAM_BOT_TOKEN cannot poll
at once (Telegram allows one poller per token); stop a credentialed server or
``python -m src.main join`` first, or pass --skip-telegram. Message *delivery*
(what this stage measures) still works either way - only button-tap routing
needs the exclusive poller.

Exit codes: 0 = no stage FAILed (WARN/SKIP is fine), 1 = at least one FAIL.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

logger = logging.getLogger("deleg8.diagnose")

APP_NAME: str = "DELEG8 System Diagnostic"


# --------------------------------------------------------------------------- #
# Result model and rendering
# --------------------------------------------------------------------------- #

class Level(str, Enum):
    """Severity of one check."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


_ORDER: dict[Level, int] = {Level.SKIP: 0, Level.PASS: 1, Level.WARN: 2, Level.FAIL: 3}
"""Severity for picking a stage's headline level. SKIP ranks below PASS: a stage
that mostly passed with one thing skipped is a PASS, not a SKIP; only a stage
where *everything* was skipped reports as SKIP."""
_ICON: dict[Level, str] = {Level.PASS: "✓", Level.WARN: "!", Level.FAIL: "✗", Level.SKIP: "-"}
_ANSI: dict[Level, str] = {Level.PASS: "32", Level.WARN: "33", Level.FAIL: "31", Level.SKIP: "90"}


def _color_enabled() -> bool:
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _c(text: str, level: Level) -> str:
    return f"\033[{_ANSI[level]}m{text}\033[0m" if _color_enabled() else text


@dataclass(frozen=True)
class CheckResult:
    """One assertion within a stage."""

    name: str
    level: Level
    detail: str = ""
    hint: str = ""
    """Actionable remediation; printed only for WARN/FAIL."""


@dataclass
class StageReport:
    """Everything one stage produced."""

    number: int
    title: str
    results: list[CheckResult] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def worst(self) -> Level:
        """The most severe level seen, or SKIP if the stage produced nothing."""
        if not self.results:
            return Level.SKIP
        return max(self.results, key=lambda r: _ORDER[r.level]).level


def _print_stage_header(number: int, title: str) -> None:
    bar = "─" * 68
    label = f" STAGE {number}/6: {title} "
    print(f"\n┌{bar}┐")
    print(f"│{label.center(68)}│")
    print(f"└{bar}┘")


def _print_result(result: CheckResult) -> None:
    icon = _c(_ICON[result.level], result.level)
    line = f"  [{icon}] {result.name}"
    if result.detail:
        line += f"  — {result.detail}"
    print(line)
    if result.hint and result.level in (Level.WARN, Level.FAIL):
        print(f"          → {result.hint}")


def _print_summary(reports: list[StageReport]) -> None:
    bar = "=" * 70
    print(f"\n{bar}")
    print(f"{APP_NAME.upper()} — SUMMARY  ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
    print(bar)
    for r in reports:
        worst = r.worst
        counts = {lvl: sum(1 for res in r.results if res.level is lvl) for lvl in Level}
        tally = " ".join(f"{counts[lvl]}{lvl.value[0]}" for lvl in Level if counts[lvl])
        print(f"  Stage {r.number}  {r.title:<38} {_c(worst.value, worst):<6} ({r.elapsed_seconds:5.1f}s)  [{tally or '-'}]")
    print(bar)
    failed = [r for r in reports if r.worst is Level.FAIL]
    warned = [r for r in reports if r.worst is Level.WARN]
    if failed:
        names = ", ".join(f"#{r.number}" for r in failed)
        print(f"RESULT: FAIL — stage(s) {names} failed. Fix the hints above before declaring the backend complete.")
    elif warned:
        names = ", ".join(f"#{r.number}" for r in warned)
        print(f"RESULT: PASS with warnings — stage(s) {names} degraded gracefully. Review the hints when convenient.")
    else:
        print("RESULT: PASS — every stage is genuinely wired together and working.")
    print(bar)


def _redact(key: str, value: str) -> str:
    if any(word in key for word in ("TOKEN", "KEY", "PASSWORD")):
        return f"{value[:4]}…{value[-2:]} ({len(value)} chars)" if len(value) > 8 else "set (short value)"
    return value


def _int_env(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    return int(raw) if re.fullmatch(r"-?\d+", raw or "") else None


async def _run_stage(number: int, title: str, fn) -> StageReport:  # noqa: ANN001
    """Run one stage, catching anything it raises so one bug never kills the dashboard."""
    t0 = time.monotonic()
    try:
        report = await fn()
    except Exception as exc:  # noqa: BLE001 - a stage must never abort the whole run
        logger.exception("stage %d (%s) crashed", number, title)
        report = StageReport(number, title)
        report.results.append(CheckResult(f"stage {number} crashed unexpectedly", Level.FAIL, repr(exc)))
    report.elapsed_seconds = time.monotonic() - t0
    return report


# --------------------------------------------------------------------------- #
# Stage 1: Environment & Credentials
# --------------------------------------------------------------------------- #

_REQUIRED_KEYS: dict[str, str] = {
    "TELEGRAM_BOT_TOKEN": "Sprint 2 escalation + briefing delivery",
    "TELEGRAM_CHAT_ID": "Sprint 2 escalation + briefing delivery",
    "DEEPGRAM_API_KEY": "Sprint 3 streaming STT",
    "CARTESIA_API_KEY": "Sprint 3 streaming TTS",
    "MEET_JOIN_URL": "Sprint 5 Google Meet ingress",
}
_TELEGRAM_TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{20,}$")


async def stage1_environment(args: argparse.Namespace) -> StageReport:
    """Presence, shape, and (optionally) live acceptance of every required credential."""
    report = StageReport(1, "Environment & Credentials Check")

    for key, purpose in _REQUIRED_KEYS.items():
        value = os.getenv(key, "").strip()
        if not value:
            report.results.append(CheckResult(
                f"{key} present", Level.WARN, f"not set — {purpose} will be degraded or disabled",
                hint=f"set {key} in .env",
            ))
        else:
            report.results.append(CheckResult(f"{key} present", Level.PASS, _redact(key, value)))

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if token and not _TELEGRAM_TOKEN_RE.match(token):
        report.results.append(CheckResult(
            "TELEGRAM_BOT_TOKEN shape", Level.WARN, "does not look like <digits>:<35+ chars>",
            hint="re-copy the token from @BotFather",
        ))
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if chat_id and not re.fullmatch(r"-?\d+", chat_id):
        report.results.append(CheckResult(
            "TELEGRAM_CHAT_ID shape", Level.FAIL, f"{chat_id!r} is not an integer",
            hint="get your numeric id from @userinfobot",
        ))
    url = os.getenv("MEET_JOIN_URL", "").strip()
    if url:
        try:
            from src.gateway.meet_bot import MeetConfigError, meeting_code

            meeting_code(url)
            report.results.append(CheckResult("MEET_JOIN_URL shape", Level.PASS, url))
        except MeetConfigError as exc:
            report.results.append(CheckResult("MEET_JOIN_URL shape", Level.FAIL, str(exc)))

    if args.skip_live_keys:
        report.results.append(CheckResult("live Deepgram/Cartesia key validation", Level.SKIP, "--skip-live-keys"))
    else:
        if os.getenv("DEEPGRAM_API_KEY", "").strip():
            report.results.append(await _check_deepgram_key())
        if os.getenv("CARTESIA_API_KEY", "").strip():
            report.results.append(await _check_cartesia_key())
    return report


async def _check_deepgram_key() -> CheckResult:
    """A real, cheap authenticated call (list projects) - no audio, no billed transcription."""
    try:
        from deepgram import AsyncDeepgramClient
        from deepgram.core.api_error import ApiError
    except ImportError as exc:
        return CheckResult("DEEPGRAM_API_KEY live check", Level.WARN, f"deepgram-sdk not importable ({exc})")
    client = AsyncDeepgramClient(api_key=os.getenv("DEEPGRAM_API_KEY", "").strip())
    try:
        await asyncio.wait_for(client.manage.v1.projects.list(), timeout=8.0)
        return CheckResult("DEEPGRAM_API_KEY live check", Level.PASS, "accepted by Deepgram")
    except ApiError as exc:
        if exc.status_code == 401:
            return CheckResult("DEEPGRAM_API_KEY live check", Level.FAIL, "Deepgram rejected the key (HTTP 401)",
                                hint="create a new key at https://console.deepgram.com")
        return CheckResult("DEEPGRAM_API_KEY live check", Level.WARN, f"Deepgram returned HTTP {exc.status_code}")
    except TimeoutError:
        return CheckResult("DEEPGRAM_API_KEY live check", Level.WARN, "timed out reaching Deepgram")
    except Exception as exc:  # noqa: BLE001 - network/DNS/etc. must not fail the whole stage
        return CheckResult("DEEPGRAM_API_KEY live check", Level.WARN, f"could not reach Deepgram ({exc})")


async def _check_cartesia_key() -> CheckResult:
    """A real, cheap authenticated call (list one voice) via the same SDK path tts_stream.py uses."""
    try:
        from cartesia import APIStatusError, AsyncCartesia
    except ImportError as exc:
        return CheckResult("CARTESIA_API_KEY live check", Level.WARN, f"cartesia SDK not importable ({exc})")

    async def _first_voice(client: "AsyncCartesia") -> None:
        async for _voice in client.voices.list(limit=1):
            return

    try:
        async with AsyncCartesia(api_key=os.getenv("CARTESIA_API_KEY", "").strip()) as client:
            await asyncio.wait_for(_first_voice(client), timeout=8.0)
        return CheckResult("CARTESIA_API_KEY live check", Level.PASS, "accepted by Cartesia")
    except APIStatusError as exc:
        if exc.status_code == 401:
            return CheckResult("CARTESIA_API_KEY live check", Level.FAIL, "Cartesia rejected the key (HTTP 401)",
                                hint="create a new key at https://play.cartesia.ai")
        return CheckResult("CARTESIA_API_KEY live check", Level.WARN, f"Cartesia returned HTTP {exc.status_code}")
    except TimeoutError:
        return CheckResult("CARTESIA_API_KEY live check", Level.WARN, "timed out reaching Cartesia")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("CARTESIA_API_KEY live check", Level.WARN, f"could not reach Cartesia ({exc})")


# --------------------------------------------------------------------------- #
# Stage 2: Audio Device & Virtual Cable Check
# --------------------------------------------------------------------------- #

async def stage2_audio(args: argparse.Namespace) -> StageReport:
    """Device enumeration (shared with device_setup.py), plus real non-blocking stream opens."""
    report = StageReport(2, "Audio Device & Virtual Cable Check")
    try:
        from src.audio.device_setup import (
            INPUT_KEY,
            OUTPUT_KEY,
            cross_check_with_pyaudio,
            list_devices,
            suggest,
        )
    except ImportError as exc:
        report.results.append(CheckResult("import src.audio.device_setup", Level.FAIL, str(exc)))
        return report

    try:
        devices = await asyncio.to_thread(list_devices)
    except Exception as exc:  # noqa: BLE001
        report.results.append(CheckResult("enumerate PortAudio devices", Level.FAIL, str(exc)))
        return report
    report.results.append(CheckResult(
        "PortAudio devices found", Level.PASS if devices else Level.FAIL, f"{len(devices)} device(s)",
    ))
    if not devices:
        return report

    problems = await asyncio.to_thread(cross_check_with_pyaudio, devices)
    if problems:
        for problem in problems:
            report.results.append(CheckResult("sounddevice/PyAudio index agreement", Level.WARN, problem))
    else:
        report.results.append(CheckResult("sounddevice/PyAudio index agreement", Level.PASS, "indexes and names match"))

    virtual = [d for d in devices if d.looks_virtual]
    if virtual:
        report.results.append(CheckResult(
            "virtual cable(s) detected", Level.PASS, ", ".join(f"[{d.index}] {d.name}" for d in virtual),
        ))
    else:
        report.results.append(CheckResult(
            "virtual cable(s) detected", Level.WARN, "none found by name (BlackHole / VB-Cable / ...)",
            hint="brew install --cask blackhole-2ch blackhole-16ch, then: "
                 "sudo launchctl kickstart -kill system/com.apple.audio.coreaudiod",
        ))
    suggested_a, suggested_b = suggest(devices)
    virtual_indexes = {d.index for d in virtual}

    in_idx = _int_env("DELEG8_INPUT_DEVICE_INDEX")
    out_idx = _int_env("DELEG8_OUTPUT_DEVICE_INDEX")
    for key, idx in ((INPUT_KEY, in_idx), (OUTPUT_KEY, out_idx)):
        if idx is None:
            report.results.append(CheckResult(
                f"{key} configured", Level.WARN, "not set",
                hint="python -m src.audio.device_setup --select --write",
            ))
        else:
            report.results.append(CheckResult(f"{key} configured", Level.PASS, str(idx)))
    if in_idx is not None and out_idx is not None and in_idx == out_idx:
        report.results.append(CheckResult(
            "input and output are distinct devices", Level.WARN,
            f"both point at device {in_idx}; the bot would hear its own speech",
        ))
    if virtual and in_idx is not None and out_idx is not None and not ({in_idx, out_idx} & virtual_indexes):
        report.results.append(CheckResult(
            "configured devices actually use a virtual cable", Level.WARN,
            f"a virtual cable exists ({', '.join(f'[{d.index}] {d.name}' for d in virtual)}) but "
            f"{INPUT_KEY}={in_idx} / {OUTPUT_KEY}={out_idx} point elsewhere - Meet audio will not reach the bot",
            hint="python -m src.audio.device_setup --select --write --set-system-defaults"
                 + (f"  (suggested: {INPUT_KEY}={suggested_a.index}, {OUTPUT_KEY}={suggested_b.index})"
                    if suggested_a and suggested_b else ""),
        ))

    if args.skip_audio_open:
        report.results.append(CheckResult("non-blocking stream open test", Level.SKIP, "--skip-audio-open"))
        return report

    for label, idx, direction in (("input", in_idx, "in"), ("output", out_idx, "out")):
        if idx is None:
            continue
        device = next((d for d in devices if d.index == idx), None)
        if device is None:
            report.results.append(CheckResult(f"{label} device {idx} exists", Level.FAIL, "index not found by PortAudio"))
            continue
        channels = device.input_channels if direction == "in" else device.output_channels
        if channels == 0:
            report.results.append(CheckResult(
                f"{label} device {idx} has {direction}put channels", Level.FAIL, f"{device.name} has 0",
            ))
            continue
        for rate in (16000, 48000):
            ok, detail = await asyncio.to_thread(_try_open_stream, idx, rate, direction)
            report.results.append(CheckResult(
                f"{label} stream @ {rate} Hz ({device.name})", Level.PASS if ok else Level.WARN, detail,
            ))
    return report


def _try_open_stream(index: int, rate: int, direction: str) -> tuple[bool, str]:
    """Open a non-blocking PyAudio stream on ``index`` at ``rate`` and close it immediately."""
    import pyaudio

    pa = pyaudio.PyAudio()
    stream = None

    def _callback(in_data, frame_count, time_info, status):  # noqa: ANN001, ARG001
        return (None, pyaudio.paContinue)

    try:
        kwargs: dict = dict(
            format=pyaudio.paInt16, channels=1, rate=rate,
            frames_per_buffer=max(1, int(rate * 0.02)), stream_callback=_callback,
        )
        if direction == "in":
            kwargs["input"] = True
            kwargs["input_device_index"] = index
        else:
            kwargs["output"] = True
            kwargs["output_device_index"] = index
        stream = pa.open(**kwargs)
        stream.start_stream()
        time.sleep(0.05)
        return True, f"opened a non-blocking {direction}put stream at {rate} Hz"
    except Exception as exc:  # noqa: BLE001 - report every PortAudio failure mode uniformly
        return False, str(exc)
    finally:
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        pa.terminate()


# --------------------------------------------------------------------------- #
# Stage 3: Policy Engine State Machine Check
# --------------------------------------------------------------------------- #

async def stage3_policy_engine(_args: argparse.Namespace) -> StageReport:
    """The three Sprint 1 routing scenarios through the real, live PolicyEngine."""
    report = StageReport(3, "Policy Engine State Machine Check")
    try:
        from src.brain import DecisionState, MeetingContext, PolicyEngine, profile_store
    except ImportError as exc:
        report.results.append(CheckResult("import src.brain", Level.FAIL, str(exc)))
        return report

    engine = PolicyEngine(profile_store)
    scenarios = (
        ("AUTO_REPLY (GREEN topic, grounded answer)", "What is the status of the API integration?", "Mike", DecisionState.AUTO_REPLY),
        ("ESCALATE (RED topic, pricing)", "Can we lower the enterprise price by 20%?", "Mike", DecisionState.ESCALATE),
        ("SILENT (addressed to someone else)", "Hey Sarah, how was your weekend?", "Mike", DecisionState.SILENT),
        # Real missed-question phrasings a request lexicon that only covers
        # "can you"/"what's"/etc. drops: no "?", no old marker phrase, but a
        # human would clearly hear a question. See tests/test_policy_signals.py.
        ("AUTO_REPLY (GREEN topic, 'why is' phrasing)", "Why is the api integration progress behind schedule", "Mike", DecisionState.AUTO_REPLY),
        ("ESCALATE (YELLOW topic, 'what happened to' phrasing)", "What happened to the delivery date for this", "Mike", DecisionState.ESCALATE),
        ("SILENT (topic mentioned, still no question)", "The API integration progress looks solid this week.", "Mike", DecisionState.SILENT),
    )
    for name, transcript, speaker, expected in scenarios:
        try:
            context = MeetingContext(
                meeting_id="diagnostic", user_id="durai", transcript=transcript,
                speaker=speaker, participants=["Durai Kumaran", "Sarah", "Mike"],
            )
            decision = await asyncio.wait_for(engine.evaluate_async(context), timeout=10.0)
            ok = decision.state is expected
            report.results.append(CheckResult(
                name, Level.PASS if ok else Level.FAIL,
                f"got {decision.state.value} (expected {expected.value}): {decision.reason}",
                hint="" if ok else "policy_engine.py's state machine or profile_store.py's seed data changed",
            ))
        except Exception as exc:  # noqa: BLE001
            report.results.append(CheckResult(name, Level.FAIL, f"raised {exc!r}"))
    return report


# --------------------------------------------------------------------------- #
# Stage 4: ChromaDB RAG Retrieval Check
# --------------------------------------------------------------------------- #

async def stage4_rag(_args: argparse.Namespace) -> StageReport:
    """Open the real index and prove retrieval works using a query built from what is actually stored."""
    report = StageReport(4, "ChromaDB RAG Retrieval Check")
    try:
        from src.knowledge.config import KnowledgeConfigError, KnowledgeSettings
        from src.knowledge.retriever import KnowledgeRetriever, RetrievalStatus
        from src.knowledge.store import KnowledgeStore
    except ImportError as exc:
        report.results.append(CheckResult("import src.knowledge", Level.FAIL, str(exc)))
        return report

    try:
        settings = KnowledgeSettings.from_env()
    except KnowledgeConfigError as exc:
        report.results.append(CheckResult("knowledge settings", Level.FAIL, str(exc)))
        return report

    if not settings.chroma_dir.exists():
        report.results.append(CheckResult(
            "data/chroma_db/ exists", Level.WARN, str(settings.chroma_dir), hint="python -m src.knowledge.ingest",
        ))

    store = KnowledgeStore(settings)
    try:
        await asyncio.wait_for(store.open_async(), timeout=60.0)
    except Exception as exc:  # noqa: BLE001 - covers a missing/corrupt model cache, disk errors, etc.
        report.results.append(CheckResult("open embedding model + collection", Level.FAIL, str(exc)))
        return report
    report.results.append(CheckResult("embedding model + collection opened", Level.PASS, settings.embedding_model))

    try:
        count = await store.run(store.count)
        if count == 0:
            report.results.append(CheckResult(
                "index has chunks", Level.WARN, "0 chunks",
                hint="put a file in data/documents/ then run: python -m src.knowledge.ingest",
            ))
            return report
        report.results.append(CheckResult("index has chunks", Level.PASS, f"{count} chunk(s)"))

        retriever = KnowledgeRetriever(store, settings)

        unrelated = await retriever.retrieve("What is the capital of France and who won the 1998 World Cup?")
        gated = unrelated.status is RetrievalStatus.UNKNOWN_CONTEXT
        report.results.append(CheckResult(
            "similarity gate rejects an unrelated query", Level.PASS if gated else Level.WARN,
            f"status={unrelated.status.value} best_similarity={unrelated.best_similarity} threshold={settings.min_similarity}",
        ))

        sample = await store.run(lambda: store.collection.get(limit=1, include=["documents"]))
        docs = sample.get("documents") or []
        if not docs or not docs[0]:
            report.results.append(CheckResult("grounded-query round trip", Level.WARN, "could not sample a chunk to build a query from"))
        else:
            words = docs[0].strip().split()
            probe_query = " ".join(words[:14]) or docs[0][:80]
            result = await retriever.retrieve(probe_query)
            grounded = result.status is RetrievalStatus.GROUNDED
            above = (result.best_similarity or 0.0) >= settings.min_similarity
            ok = grounded and above
            report.results.append(CheckResult(
                "grounded-query round trip (query built from an indexed chunk)",
                Level.PASS if ok else Level.FAIL,
                f"status={result.status.value} best_similarity={result.best_similarity} threshold={settings.min_similarity}",
                hint="" if ok else "the same text just embedded should retrieve itself above threshold; check the "
                                    "embedding model, the HNSW space configuration, or DELEG8_RAG_MIN_SIMILARITY",
            ))
    finally:
        store.close()
    return report


# --------------------------------------------------------------------------- #
# Stage 5: Telegram Escalation Live Ping
# --------------------------------------------------------------------------- #

async def stage5_telegram(args: argparse.Namespace) -> StageReport:
    """Send a real Approve/Reject card and confirm delivery (a tap is a bonus, not a requirement)."""
    report = StageReport(5, "Telegram Escalation Live Ping")
    if args.skip_telegram:
        report.results.append(CheckResult("Telegram ping", Level.SKIP, "--skip-telegram"))
        return report
    try:
        from src.brain.models import (
            DecisionState,
            MeetingContext,
            PolicyDecision,
            PolicyTier,
        )
        from src.escalation.telegram_bot import (
            EscalationConfigError,
            TelegramEscalationBridge,
            TelegramSettings,
        )
    except ImportError as exc:
        report.results.append(CheckResult("import src.escalation", Level.FAIL, str(exc)))
        return report

    if not TelegramSettings.is_configured():
        report.results.append(CheckResult(
            "Telegram configured", Level.WARN, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set",
            hint="python tests/test_escalation.py --setup-help",
        ))
        return report
    try:
        settings = TelegramSettings.from_env()
    except EscalationConfigError as exc:
        report.results.append(CheckResult("Telegram settings", Level.FAIL, str(exc)))
        return report
    settings = dataclasses.replace(settings, timeout_seconds=args.telegram_timeout)

    bridge = TelegramEscalationBridge(settings)
    try:
        await asyncio.wait_for(bridge.start(), timeout=20.0)
    except (EscalationConfigError, TimeoutError) as exc:
        report.results.append(CheckResult(
            "Telegram bridge connect", Level.FAIL, str(exc),
            hint="another process may already be polling this bot token - only one may at a time",
        ))
        return report
    report.results.append(CheckResult("Telegram bridge connected", Level.PASS, bridge.describe()))

    context = MeetingContext(
        meeting_id="diagnostic", user_id="durai",
        transcript="[DELEG8 diagnostic] Tap a button to confirm the escalation bridge is live.",
        speaker="Diagnostic", participants=["Durai Kumaran", "Diagnostic"],
    )
    decision = PolicyDecision(
        state=DecisionState.ESCALATE, reason="Diagnostic health check ping; not a real request.",
        topic="diagnostic_ping", tier=PolicyTier.YELLOW,
    )
    try:
        print(f"  sending a test card to your Telegram chat; waiting up to {args.telegram_timeout:.0f}s for a tap "
              "(Approve or Reject either one is fine - this only checks connectivity)...")
        result = await bridge.send_escalation_request(
            context, "This is a diagnostic ping, not a real commitment.", decision,
        )
        if not result.delivered:
            report.results.append(CheckResult(
                "test card delivered", Level.FAIL, result.detail or "not delivered",
                hint="check the bot token and chat id, and that you have pressed Start in a chat with the bot",
            ))
        elif result.human_decided:
            report.results.append(CheckResult(
                "test card delivered and answered", Level.PASS,
                f"{result.outcome.value} by {result.responded_by} in {result.response_seconds:.1f}s",
            ))
        else:
            report.results.append(CheckResult(
                "test card delivered", Level.PASS,
                f"no tap within {args.telegram_timeout:.0f}s ({result.outcome.value}); delivery is what this stage checks",
            ))
    finally:
        await bridge.stop()
    return report


# --------------------------------------------------------------------------- #
# Stage 6: Orchestrator Pipeline Integration
# --------------------------------------------------------------------------- #

async def stage6_orchestrator(args: argparse.Namespace) -> StageReport:
    """Prove the gateway's real concurrency shape works: queue + policy pipeline never blocks the loop."""
    report = StageReport(6, "Orchestrator Pipeline Integration")
    try:
        from src.audio.stt_stream import Utterance
        from src.brain import MeetingContext, PolicyEngine, profile_store
        from src.escalation.bridge import NullEscalationBridge
        from src.gateway.meet_bot import GoogleMeetBot, MeetSettings
        from src.memory.meeting_state import MeetingStateRegistry
        from src.turn_processor import TurnProcessor
    except ImportError as exc:
        report.results.append(CheckResult("import gateway/orchestrator modules", Level.FAIL, str(exc)))
        return report
    report.results.append(CheckResult("import gateway/orchestrator modules", Level.PASS))

    try:
        bot = GoogleMeetBot(MeetSettings.from_env(require_url=False))
        engine = PolicyEngine(profile_store)
        memory = MeetingStateRegistry()
        turns = TurnProcessor(engine=engine, store=profile_store, bridge=NullEscalationBridge("diagnostic"), memory=memory)
        report.results.append(CheckResult(
            "construct bot + engine + memory + turn processor", Level.PASS, f"bot status={bot.status.value}",
        ))
    except Exception as exc:  # noqa: BLE001
        report.results.append(CheckResult("construct bot + engine + memory + turn processor", Level.FAIL, str(exc)))
        return report

    heartbeat_ticks = 0
    stop_heartbeat = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal heartbeat_ticks
        while not stop_heartbeat.is_set():
            await asyncio.sleep(0.05)
            heartbeat_ticks += 1

    utterances: asyncio.Queue[Utterance] = asyncio.Queue()
    processed: list[str] = []
    transcripts = ("What is the status of the API integration?", "Can we lower the enterprise price by 20%?")

    async def producer() -> None:
        for text in transcripts:
            await asyncio.sleep(0.1)
            utterances.put_nowait(Utterance(text=text, confidence=0.95, started_at=0.0, finalized_at=0.0, interrupted_bot=False))

    async def consumer() -> None:
        for _ in transcripts:
            utt = await utterances.get()
            context = MeetingContext(
                meeting_id="diagnostic-loop", user_id="durai", transcript=utt.text,
                speaker="Mike", participants=["Durai Kumaran", "Mike"],
            )
            result = await turns.process_turn(context)
            processed.append(result.state.value)

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        await asyncio.wait_for(asyncio.gather(producer(), consumer()), timeout=10.0)
    except Exception as exc:  # noqa: BLE001
        report.results.append(CheckResult("concurrent queue + policy pipeline", Level.FAIL, str(exc)))
    else:
        report.results.append(CheckResult(
            "concurrent queue + policy pipeline", Level.PASS, f"processed {processed} while the heartbeat kept ticking",
        ))
    finally:
        stop_heartbeat.set()
        await heartbeat_task

    report.results.append(CheckResult(
        "event loop stayed responsive (not blocked)", Level.PASS if heartbeat_ticks >= 3 else Level.WARN,
        f"{heartbeat_ticks} heartbeat tick(s) during the concurrent run",
    ))

    if args.skip_browser:
        report.results.append(CheckResult("Playwright browser launch", Level.SKIP, "--skip-browser"))
    else:
        ok, detail = await _check_playwright_launch()
        report.results.append(CheckResult(
            "Playwright browser launch (headless, closes immediately)", Level.PASS if ok else Level.WARN, detail,
        ))
    return report


async def _check_playwright_launch() -> tuple[bool, str]:
    """Launch a throwaway headless Chromium (never the persistent Meet profile) and close it."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        return False, f"playwright not importable ({exc})"
    try:
        async with async_playwright() as p:
            browser = await asyncio.wait_for(p.chromium.launch(headless=True), timeout=20.0)
            try:
                page = await browser.new_page()
                await page.goto("about:blank")
                webdriver = await page.evaluate("() => navigator.webdriver")
            finally:
                await browser.close()
        return True, (
            f"Chromium launched and closed cleanly (navigator.webdriver={webdriver!r} on this vanilla launch - "
            "expected; the real Meet bot's anti-detection flags are covered by test_meet_ingress.py, not this stage)"
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{exc} (run: python -m playwright install chromium)"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

STAGES: tuple[tuple[int, str, "object"], ...] = (
    (1, "Environment & Credentials Check", stage1_environment),
    (2, "Audio Device & Virtual Cable Check", stage2_audio),
    (3, "Policy Engine State Machine Check", stage3_policy_engine),
    (4, "ChromaDB RAG Retrieval Check", stage4_rag),
    (5, "Telegram Escalation Live Ping", stage5_telegram),
    (6, "Orchestrator Pipeline Integration", stage6_orchestrator),
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", type=int, choices=range(1, 7), default=None, help="run only this stage (default: all 6)")
    parser.add_argument("-y", "--yes", action="store_true", help="skip the confirmation before Stage 5's Telegram send")
    parser.add_argument("--skip-telegram", action="store_true", help="skip Stage 5 entirely (no message sent)")
    parser.add_argument("--telegram-timeout", type=float, default=10.0, help="seconds to wait for a tap in Stage 5 (default 10)")
    parser.add_argument("--skip-live-keys", action="store_true", help="Stage 1: skip live Deepgram/Cartesia validation")
    parser.add_argument("--skip-audio-open", action="store_true", help="Stage 2: skip actually opening PyAudio streams")
    parser.add_argument("--skip-browser", action="store_true", help="Stage 6: skip the real headless Chromium launch")
    parser.add_argument("--log-level", default="ERROR", help="component log level; default ERROR keeps the dashboard clean")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    """Run the selected stage(s) and print the dashboard."""
    bar = "=" * 70
    print(bar)
    print(f"{APP_NAME}  ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
    print(f"project root: {PROJECT_ROOT}")
    print(bar)

    to_run = STAGES if args.stage is None else [s for s in STAGES if s[0] == args.stage]
    reports: list[StageReport] = []
    for number, title, fn in to_run:
        _print_stage_header(number, title)
        if number == 5 and not args.yes and not args.skip_telegram and sys.stdin.isatty():
            reply = input("  This sends a real message to your configured Telegram chat. Continue? [Y/n] ").strip().lower()
            if reply.startswith("n"):
                report = StageReport(5, title)
                report.results.append(CheckResult("Telegram ping", Level.SKIP, "skipped interactively"))
                for r in report.results:
                    _print_result(r)
                reports.append(report)
                continue
        report = await _run_stage(number, title, lambda fn=fn: fn(args))
        for r in report.results:
            _print_result(r)
        reports.append(report)

    _print_summary(reports)
    return 1 if any(r.worst is Level.FAIL for r in reports) else 0


def main(argv: list[str]) -> int:
    """Entry point."""
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.ERROR),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
