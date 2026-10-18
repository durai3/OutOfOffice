"""
The meeting session: everything DELEG8 needs to sit in a call, composed.

    Telegram bridge             ─┐
    knowledge index             ─┼─► PolicyEngine ─► TurnProcessor ─► MeetingStateRegistry
    voice loop                  ─┘        ▲                 │
                                          │                 ▼
    a platform bot ──────────── audio via virtual cables ── VoiceOrchestrator
    (GoogleMeetBot / ZoomBot /
     TeamsBot - picked by
     detect_platform(url))
                                                              │
                              on exit: PostMeetingReporter ─► executive briefing ─► Telegram

Everything above the bot itself is platform-agnostic: it only ever talks to
whichever bot :func:`_build_bot` constructed through the shared
:class:`~src.gateway.bot_base.MeetingBot` surface (start/login/join_meeting/
leave/check_health/stay_connected/inspect_prejoin), so a Zoom or Teams link
runs the identical session, voice loop, brain and briefing a Google Meet link
does - see :mod:`src.gateway.bot_base` for why that seam exists.

Two callers drive a session:

* ``python -m src.main join`` calls :func:`run_meeting_session`, which builds
  its own :class:`~src.gateway.components.SessionComponents`, starts the
  Telegram bridge, installs signal handlers, runs one
  :class:`MeetingSession`, and tears everything down.
* The HTTP API (``POST /api/v1/meetings/join``) constructs a
  :class:`MeetingSession` on the components it already owns and runs it as a
  background task; ``POST /api/v1/meetings/abort`` calls
  :meth:`MeetingSession.request_stop`. Every step is reported through the
  session's event callback so the web console can watch it live.

Whichever ends first (Ctrl+C, ``/leave`` on Telegram, the recall button,
being removed, the call ending) tears the rest down in reverse order and
sends the briefing.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.audio.audio_io import AudioIO
from src.audio.barge_in_handler import BargeInHandler
from src.audio.cable_check import preflight as audio_preflight
from src.audio.config import VoiceConfigError, VoiceSettings
from src.audio.stt_stream import SttStream, Utterance
from src.audio.tts_stream import TtsStream
from src.brain.profile_store import ProfileNotFoundError, ProfileStore, profile_store
from src.compliance.consent import (
    ConsentGate,
    consent_timeout_from_env,
    mode_from_env,
    mode_from_value,
)
from src.db.models import ConsentMode
from src.dispatch.bundle import (
    BundleDossierStore,
    BundleError,
    BundleProfileStore,
    MeetingBundle,
    fetch_bundle,
)
from src.dispatch.callback import build_emitter, callback_url, is_worker, job_token
from src.dispatch.relay import RelayEscalationBridge, RelayReportArchive
from src.escalation.telegram_bot import EscalationConfigError, TelegramEscalationBridge
from src.events import EventCallback, safe_emit
from src.gateway.bot_base import (
    MeetConfigError,
    MeetingBot,
    MeetJoinError,
    MeetLoginError,
    MeetStatus,
    Platform,
    detect_platform,
    meeting_identifier,
)
from src.gateway.components import (
    SessionComponents,
    SessionOptions,
    build_bridge,
    open_knowledge,
)
from src.gateway.meet_bot import GoogleMeetBot, MeetSettings
from src.gateway.teams_bot import TeamsBot, TeamsSettings
from src.gateway.zoom_bot import ZoomBot, ZoomSettings
from src.llm import allow_no_llm
from src.memory.history_store import MeetingHistoryStore
from src.orchestrator import VoiceOrchestrator

BotSettings = "MeetSettings | ZoomSettings | TeamsSettings"
"""Union of every platform's settings dataclass - a string alias (used only in
annotations, with ``from __future__ import annotations`` in effect) because
the three concrete types live in three different modules and this file
should not need to know their fields, only that :mod:`src.gateway.bot_base`'s
``BotSettingsLike`` surface is present on whichever one a session holds."""

_ENV_VAR_BY_PLATFORM: dict[Platform, str] = {
    Platform.GOOGLE_MEET: "MEET_JOIN_URL",
    Platform.ZOOM: "ZOOM_JOIN_URL",
    Platform.TEAMS: "TEAMS_JOIN_URL",
}
_ID_PREFIX_BY_PLATFORM: dict[Platform, str] = {
    Platform.GOOGLE_MEET: "meet",  # unchanged from before multi-platform support existed
    Platform.ZOOM: "zoom",
    Platform.TEAMS: "teams",
}


def _resolve_join_url(explicit_url: str | None) -> tuple[Platform, str]:
    """The meeting link and its platform, from an explicit override or the environment.

    Checks MEET_JOIN_URL, then ZOOM_JOIN_URL, then TEAMS_JOIN_URL (first
    non-empty wins) when no explicit override is given, so an existing
    Meet-only ``.env`` keeps working completely unchanged and Zoom/Teams
    links opt in through their own variable.

    Raises:
        UnsupportedMeetingUrlError: an explicit url matches no known platform.
        MeetConfigError: no explicit url, and none of the three env vars is set.
    """
    if explicit_url and explicit_url.strip():
        url = explicit_url.strip()
        return detect_platform(url), url
    for platform, env_var in _ENV_VAR_BY_PLATFORM.items():
        url = os.getenv(env_var, "").strip()
        if url:
            return platform, url
    raise MeetConfigError(
        "No meeting link configured. Set one of MEET_JOIN_URL (Google Meet), "
        "ZOOM_JOIN_URL, or TEAMS_JOIN_URL."
    )


def _bot_settings_from_env(platform: Platform, *, require_url: bool) -> BotSettings:
    """Build the right platform's settings dataclass from its own env vars."""
    if platform is Platform.GOOGLE_MEET:
        return MeetSettings.from_env(require_url=require_url)
    if platform is Platform.ZOOM:
        return ZoomSettings.from_env(require_url=require_url)
    return TeamsSettings.from_env(require_url=require_url)


def _build_bot(
    platform: Platform,
    settings: BotSettings,
    *,
    on_status: Callable[[MeetStatus, str | None], None] | None = None,
) -> MeetingBot:
    """Construct the right bot class for ``platform``, bound to ``settings``."""
    if platform is Platform.GOOGLE_MEET:
        assert isinstance(settings, MeetSettings)
        return GoogleMeetBot(settings, on_status=on_status)
    if platform is Platform.ZOOM:
        assert isinstance(settings, ZoomSettings)
        return ZoomBot(settings, on_status=on_status)
    assert isinstance(settings, TeamsSettings)
    return TeamsBot(settings, on_status=on_status)


logger = logging.getLogger("deleg8.gateway.session")

__all__ = [
    "MeetingSession",
    "SessionComponents",
    "SessionConfig",
    "SessionOptions",
    "build_bridge",
    "open_knowledge",
    "run_meeting_session",
]


def _print(line: str) -> None:
    print(line, flush=True)


def _escalation_timeout() -> float:
    """``ESCALATION_TIMEOUT_SECONDS`` (default 30) - how long the delegate waits on a human."""
    raw = os.getenv("ESCALATION_TIMEOUT_SECONDS", "").strip()
    try:
        return float(raw) if raw else 30.0
    except ValueError:
        return 30.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _principal_display_name(store: ProfileStore, user_id: str) -> str:
    """The principal's real name from the profile store, falling back to the raw id if unknown."""
    try:
        return store.get_profile(user_id).display_name
    except ProfileNotFoundError:
        return user_id


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SessionConfig:
    """Everything a session needs, resolved and validated before anything is started."""

    options: SessionOptions
    platform: Platform
    bot_settings: BotSettings
    voice: VoiceSettings
    url: str
    code: str
    meeting_id: str
    principal_name: str

    @classmethod
    def from_options(
        cls,
        options: SessionOptions,
        *,
        store: ProfileStore | None = None,
        user_id: str | None = None,
        voice_keys: tuple[str, str] | None = None,
    ) -> SessionConfig:
        """Resolve the bot and voice settings from the environment plus ``options``.

        ``user_id`` names the principal this session represents. It is explicit
        because a multi-tenant server runs sessions for many principals from
        one process, and ``DELEG8_VOICE_USER_ID`` can only ever name one of
        them; it falls back to that variable for the single-tenant command line.

        ``voice_keys`` is ``(deepgram_key, cartesia_key)`` for tenants using
        their own speech providers, for the same reason.

        Raises:
            MeetConfigError: bad, unsupported, or missing meeting link, malformed ``BOT_*`` value.
            VoiceConfigError: missing Deepgram / Cartesia keys or malformed voice settings.
        """
        platform, url = _resolve_join_url(options.join_url)
        bot_settings = _bot_settings_from_env(platform, require_url=False)
        bot_settings = dataclasses.replace(bot_settings, join_url=url)
        code = meeting_identifier(url, platform=platform)
        voice = VoiceSettings.from_env(
            deepgram_key=voice_keys[0] if voice_keys else None,
            cartesia_key=voice_keys[1] if voice_keys else None,
        )
        meeting_id = (options.meeting_id or "").strip() or f"{_ID_PREFIX_BY_PLATFORM[platform]}-{code}"
        principal_id = (user_id or "").strip() or voice.user_id
        principal_name = _principal_display_name(store if store is not None else profile_store, principal_id)
        voice = dataclasses.replace(
            voice,
            user_id=principal_id,
            meeting_id=meeting_id,
            speaker_label=options.speaker_label,
            participants=(principal_name, options.speaker_label),
            half_duplex=options.half_duplex or voice.half_duplex,
        )
        return cls(
            options=options, platform=platform, bot_settings=bot_settings, voice=voice,
            url=url, code=code, meeting_id=meeting_id, principal_name=principal_name,
        )

    def describe_audio(self) -> str:
        """One line about the audio devices in use."""
        voice = self.voice
        inp = voice.input_device_index if voice.input_device_index is not None else "default"
        out = voice.output_device_index if voice.output_device_index is not None else "default"
        duplex = "half" if voice.half_duplex else "full"
        return f"input device {inp}, output device {out}, {duplex}-duplex"


# --------------------------------------------------------------------------- #
# One session
# --------------------------------------------------------------------------- #


class _StopRequested(Exception):
    """Raised inside :meth:`MeetingSession.run` when a stop arrives before the call is joined."""


class MeetingSession:
    """One attempt to attend one Google Meet on a set of shared components."""

    def __init__(
        self,
        config: SessionConfig,
        components: SessionComponents,
        *,
        emit: EventCallback | None = None,
        consent_mode: ConsentMode = ConsentMode.NOTIFY_ONLY,
        on_consent: Callable[[Any], None] | None = None,
        meter: Any | None = None,
    ) -> None:
        """Bind configuration and components; nothing starts until :meth:`run`.

        ``consent_mode`` decides whether the delegate merely announces itself or
        waits for the room to agree before anything is retained; ``on_consent``
        receives each :class:`~src.compliance.consent.ConsentDecision` for the
        audit trail, and ``meter`` records what this meeting consumes.
        """
        self.config = config
        self.components = components
        self._emit_cb = emit
        self.consent = ConsentGate(
            mode=consent_mode,
            principal=config.principal_name,
            meeting_id=config.meeting_id,
            timeout_seconds=consent_timeout_from_env(),
            on_decision=on_consent,
            delegate_name=config.bot_settings.display_name or "DELEG8",
        )
        self.meter = meter
        self.bot = _build_bot(config.platform, config.bot_settings, on_status=self._on_bot_status)
        self.orchestrator: VoiceOrchestrator | None = None
        self._barge_in: BargeInHandler | None = None
        self.created_at: datetime = _utc_now()
        self.started_at: datetime | None = None
        self.joined_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.exit_code: int | None = None
        self.error: str | None = None
        self.stop_reason: str | None = None
        self._stop = asyncio.Event()

    # -- identity -----------------------------------------------------------

    @property
    def url(self) -> str:
        """The Meet link."""
        return self.config.url

    @property
    def code(self) -> str:
        """The ``abc-defg-hij`` meeting code."""
        return self.config.code

    @property
    def meeting_id(self) -> str:
        """Memory / briefing id."""
        return self.config.meeting_id

    @property
    def status(self) -> MeetStatus:
        """Where the bot is in the call lifecycle."""
        return self.bot.status

    @property
    def stopping(self) -> bool:
        """True once a stop has been requested."""
        return self._stop.is_set()

    @property
    def finished(self) -> bool:
        """True once :meth:`run` has returned."""
        return self.finished_at is not None

    # -- control ------------------------------------------------------------

    def request_stop(self, reason: str) -> None:
        """Ask the session to leave the call and wind down. Idempotent; the first reason wins."""
        if not self._stop.is_set():
            self.stop_reason = reason
            self._stop.set()
            self._emit("session.stopping", reason=reason)

    def _emit(self, event_type: str, **payload: Any) -> None:
        payload.setdefault("meeting_id", self.meeting_id)
        safe_emit(self._emit_cb, event_type, payload)

    async def _until_stopped(self, coro: Any) -> Any:
        """Await ``coro`` unless a stop is requested first, in which case cancel it and raise :class:`_StopRequested`.

        Used for the start / sign-in / join steps so an emergency recall works
        while the bot is still in the lobby, not only once the voice loop runs.
        """
        task = asyncio.ensure_future(coro)
        stopper = asyncio.ensure_future(self._stop.wait())
        try:
            done, _ = await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return task.result()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            raise _StopRequested()
        finally:
            stopper.cancel()

    def _on_bot_status(self, status: MeetStatus, reason: str | None) -> None:
        if status is MeetStatus.IN_CALL and self.joined_at is None:
            self.joined_at = _utc_now()
        self._emit("session.phase", phase=status.value, reason=reason, code=self.code)

    def counters(self) -> dict[str, Any]:
        """Voice-loop counters so far (zeros before the loop starts)."""
        if self.orchestrator is None:
            return {"utterances": 0, "auto_replies": 0, "escalations": 0, "silent": 0, "interruptions": 0,
                    "brain_avg_ms": None, "first_audio_avg_ms": None}
        counters = self.orchestrator.stats.as_dict()
        if self._barge_in is not None:
            counters["interruptions"] = self._barge_in.interruptions
        return counters

    async def _announce_consent(self) -> None:
        """Speak the delegate's disclosure once the voice loop is ready.

        Waits for the loop rather than firing immediately: speaking into a call
        before TTS has a socket produces silence, and a disclosure nobody heard
        is worse than none at all because the audit trail would claim it was made.
        """
        orchestrator = self.orchestrator
        if orchestrator is None:
            return
        for _ in range(100):  # up to ~20s for TTS and STT to come up
            if orchestrator.ready or self._stop.is_set():
                break
            await asyncio.sleep(0.2)
        if self._stop.is_set():
            return
        line = self.consent.announce()
        _print(f"[CONSENT] {self.consent.mode.value}: announcing to the room")
        self._emit("consent.announced", text=line, mode=self.consent.mode.value)
        try:
            await orchestrator.say(line, label="consent-disclosure")
        except Exception as exc:  # noqa: BLE001 - a failed announcement must not end the call
            logger.warning("could not speak the consent disclosure: %s", exc)
            self._emit("consent.announce_failed", error=str(exc))

    async def _verify_audio_route(self) -> None:
        """Measure the virtual cables and pin the session to ones that work.

        The delegate hears a browser meeting through a loopback device, and two
        ways of having the wrong one are completely silent: a cable whose
        driver is installed but never loaded (it enumerates, opens, and returns
        silence forever) and a device index that has drifted onto something
        else because PortAudio renumbered the device list. Either one produces
        a call in which Deepgram is connected, the console says ``[READY]``,
        and not one word is ever transcribed.

        So the route is measured here rather than assumed:
        :func:`~src.audio.cable_check.preflight` plays a tone into each
        loopback device and records it back, swaps in a working cable when the
        configured one is deaf, and points the system defaults - which is all
        Chromium follows - at the two ends. The session is then rebound to the
        devices by *name*, so what the voice loop opens cannot drift between
        this check and the stream actually opening.

        Never fatal: a machine with no virtual cable at all (a Linux worker, a
        CI box) still joins and still speaks, and the reason it cannot hear is
        on the record instead of being a mystery.
        """
        voice = self.config.voice
        if not voice.verify_cables and not voice.auto_route:
            return
        try:
            route, changes = await asyncio.to_thread(audio_preflight, voice)
        except Exception as exc:  # noqa: BLE001 - a diagnostic must never end a call
            logger.warning("audio route check failed: %s", exc)
            self._emit("audio.route_check_failed", error=str(exc))
            return

        _print("[AUDIO] route check:")
        _print(route.describe())
        for change in changes:
            _print(f"[AUDIO] {change}")
        if route.deaf:
            _print("[AUDIO] WARNING: the delegate's input device carries no audio - it will hear nothing. "
                   "Run `python -m src.audio.cable_check` to see which cables work.")
        self._emit("audio.route", **route.as_dict())

        if route.ears is None or route.mouth is None:
            return
        # Pin by name: the names cannot be renumbered out from under the voice
        # loop between now and AudioIO opening the streams, while the indexes can.
        voice = dataclasses.replace(
            voice,
            input_device_name=route.ears.name,
            output_device_name=route.mouth.name,
            input_device_index=route.ears.index,
            output_device_index=route.mouth.index,
        )
        bot_settings = self.config.bot_settings
        pick_mic_in_app = bool(route.in_app_mic) and not getattr(bot_settings, "meet_mic_name", "")
        if pick_mic_in_app:
            # The OS refused to make this cable the default input, so the
            # meeting app has to pick it from its own audio settings or the
            # room never hears the delegate at all.
            _print(f"[AUDIO] asking the meeting to select {route.in_app_mic!r} as its microphone.")
            bot_settings = dataclasses.replace(bot_settings, meet_mic_name=route.in_app_mic)
        self.config = dataclasses.replace(self.config, voice=voice, bot_settings=bot_settings)
        if pick_mic_in_app:
            # Nothing has started yet, so rebuilding the bot on the corrected
            # settings is safe - and it is the only way the new microphone name
            # reaches it, since the bot was constructed with the old ones.
            self.bot = _build_bot(self.config.platform, bot_settings, on_status=self._on_bot_status)

    async def interrupt_speech(self, reason: str) -> None:
        """Cut the delegate off mid-sentence (no-op when it is not speaking)."""
        if self._barge_in is not None and self._barge_in.is_bot_speaking:
            await self._barge_in.interrupt(reason)

    # -- run ----------------------------------------------------------------

    async def run(self) -> int:
        """Join the call, run the delegate until it is over, brief the principal.

        Exit codes: 0 clean, 1 runtime failure (could not join, voice loop crashed), 2 configuration problem.
        The escalation bridge must already be started by the caller.
        """
        config, components = self.config, self.components
        options, voice = config.options, config.voice
        memory, reporter = components.memory, components.reporter
        tag = config.platform.label.upper()
        self.started_at = _utc_now()
        self._emit(
            "session.started",
            url=self.url, code=self.code, platform=config.platform.value, speaker_label=options.speaker_label,
            principal=config.principal_name, half_duplex=voice.half_duplex, briefing=options.briefing,
            max_call_seconds=options.max_call_seconds,
        )
        if config.bot_settings.headless:
            _print("[WARN] BOT_HEADLESS=true: headless Chromium has no audio devices, the delegate will hear nothing.")

        self._emit(
            "consent.mode",
            mode=self.consent.mode.value,
            state=self.consent.state.value,
            disclosure=self.consent.disclosure,
        )

        exit_code = 0
        try:
            # -- audio route, before the browser exists ------------------
            # The probe plays a tone into the cable the meeting would be
            # carrying, and the system defaults have to be right before
            # Chromium opens its audio streams, so this goes first.
            await self._verify_audio_route()
            config = self.config
            options, voice = config.options, config.voice

            # -- into the call (each step abandoned at once on a recall) --
            await self._until_stopped(self.bot.start())
            if config.bot_settings.has_credentials:
                _print(f"[{tag}] signing in as {config.bot_settings.email}...")
                await self._until_stopped(self.bot.login())
            _print(f"[{tag}] joining {self.url} as {config.bot_settings.display_name!r}...")
            await self._until_stopped(self.bot.join_meeting(self.url))

            # -- voice loop -----------------------------------------------
            audio = AudioIO(voice)
            barge_in = BargeInHandler(audio, min_words=voice.barge_in_min_words)
            barge_in.on_interrupt = lambda reason: self._emit("speech.interrupted", reason=reason)
            self._barge_in = barge_in
            utterances: asyncio.Queue[Utterance] = asyncio.Queue()
            stt = SttStream(
                voice, audio, barge_in, utterances,
                on_interim=lambda text: self._emit("transcript.interim", speaker=options.speaker_label, text=text),
            )
            tts = TtsStream(voice, audio, barge_in)
            self.orchestrator = VoiceOrchestrator(
                voice, audio, stt, tts, barge_in, components.engine, components.turns, utterances, emit=self._emit_cb,
                participant_count_provider=lambda: self.bot.participant_count,
                consent=self.consent,
                meter=self.meter,
            )

            runner = asyncio.create_task(self.orchestrator.run(), name="deleg8-voice")
            # The disclosure goes out before anything is listened to, not after:
            # in all-party mode the gate is already shut, and in every mode the
            # room is told what is in the call with them before it speaks.
            announcer = asyncio.create_task(self._announce_consent(), name="deleg8-consent")
            watcher = asyncio.create_task(self.bot.stay_connected(), name="deleg8-meet-health")
            stopper = asyncio.create_task(self._stop.wait(), name="deleg8-stop")
            waiters = {runner, watcher, stopper}
            _ = announcer  # kept alive by the task reference below; cancelled in teardown
            timer: asyncio.Task[None] | None = None
            if options.max_call_seconds:
                timer = asyncio.create_task(asyncio.sleep(options.max_call_seconds), name="deleg8-max-call")
                waiters.add(timer)

            done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                reason = watcher.result()
                _print(f"[{tag}] out of the call: {reason} (status {self.bot.status.value})")
                self.stop_reason = self.stop_reason or f"out of the call: {reason}"
                if self.bot.status is MeetStatus.ERROR:
                    exit_code = 1
            elif stopper in done:
                _print(f"[STOP] {self.stop_reason or 'stop requested'}; leaving the call...")
            elif timer is not None and timer in done:
                _print(f"[STOP] maximum call length of {options.max_call_seconds:.0f}s reached; leaving the call...")
                self.stop_reason = f"maximum call length of {options.max_call_seconds:.0f}s reached"
            elif runner in done:
                try:
                    runner.result()
                    reason = self.orchestrator.leave_reason or "voice loop ended"
                    _print(f"[VOICE] {reason}")
                    self.stop_reason = self.stop_reason or reason
                except VoiceConfigError as exc:
                    _print(f"\n{exc}")
                    self.error = str(exc)
                    exit_code = 2
                except Exception as exc:
                    logger.exception("voice loop failed")
                    _print(f"\n[ERROR] voice loop failed: {exc}")
                    self.error = f"voice loop failed: {exc}"
                    exit_code = 1
            pending = set(pending) | ({announcer} if not announcer.done() else set())
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        except _StopRequested:
            _print(f"[STOP] {self.stop_reason or 'stop requested'} before the call was joined; leaving...")
        except (MeetConfigError, MeetLoginError, MeetJoinError) as exc:
            _print(f"\n[{tag}] {exc}")
            self.error = str(exc)
            self.stop_reason = self.stop_reason or "could not get into the call"
            exit_code = 1 if isinstance(exc, MeetJoinError) else 2
        except VoiceConfigError as exc:
            _print(f"\n{exc}")
            self.error = str(exc)
            exit_code = 2
        finally:
            # -- teardown in reverse order --------------------------------
            try:
                await self.bot.leave()
            finally:
                await self.bot.stop()
            self.consent.expire()
            if options.briefing and not self.consent.may_retain:
                # Refused, withdrawn, or never answered: the transcript was never
                # retained, so there is nothing to summarise and nothing may be
                # archived. Saying so out loud beats a silently missing briefing.
                _print(f"[BRIEFING] skipped: {self.consent.describe()}; nothing was retained.")
                memory.remove(self.meeting_id)
                self._emit("meeting.not_retained", reason=self.consent.describe(), state=self.consent.state.value)
            elif options.briefing:
                state = memory.get(self.meeting_id)
                if state is not None:
                    if state.is_empty:
                        memory.remove(state.meeting_id)
                    else:
                        _print(f"[BRIEFING] concluding {state.meeting_id} via {reporter.describe()}...")
                        report = await reporter.conclude(state, registry=memory)
                        status = "delivered" if report.receipt.delivered else f"not delivered ({report.receipt.detail})"
                        _print(f"[BRIEFING] {status}\n" + "\n".join("  | " + line for line in report.text.splitlines()))
                        self._emit("meeting.concluded", report=report)
            if self.orchestrator is not None:
                _print(f"[SESSION] {self.orchestrator.stats.summary()}")
            self.exit_code = exit_code
            self.finished_at = _utc_now()
            self._emit(
                "session.finished",
                exit_code=exit_code, error=self.error, reason=self.stop_reason, phase=self.bot.status.value,
                stats=self.counters(),
            )
        return exit_code


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


async def _dry_run(bot: MeetingBot, url: str, *, platform: Platform) -> int:
    """Launch the browser, sign in if configured, inspect the pre-join screen, and report.

    Platform-generic: every bot's ``inspect_prejoin`` returns the same
    :class:`~src.gateway.bot_base.PrejoinReport` shape, so this one function
    covers Google Meet, Zoom, and Teams alike.
    """
    tag = platform.label.upper()
    await bot.start()
    signed_in = False
    if bot.settings.has_credentials:
        try:
            signed_in = await bot.login()
        except MeetLoginError as exc:
            _print(f"[{tag}] sign-in failed: {exc}")
    report = await bot.inspect_prejoin(url)
    _print(f"[{tag}] pre-join inspection of {url}")
    _print(f"       signed in       : {signed_in or report.signed_in}")
    _print(f"       join button     : {report.join_button_label!r}")
    _print(f"       name input      : {report.name_input_present}")
    _print(f"       mic toggle      : {report.mic_toggle_present}")
    _print(f"       camera toggle   : {report.camera_toggle_present}")
    _print(f"       blocking text   : {report.blocking_text!r}")
    _print(f"       screenshot      : {report.screenshot}")
    for note in report.notes:
        _print(f"       note            : {note}")
    _print(f"       ready to join   : {report.ready}")
    return 0 if report.ready else 1


async def run_meeting_session(options: SessionOptions) -> int:
    """``python -m src.main join``: own the whole process for one meeting.

    Exit codes: 0 clean, 1 runtime failure (could not join, voice loop crashed), 2 configuration problem.
    """
    # -- dry run needs only the browser -------------------------------------
    if options.dry_run:
        try:
            platform, url = _resolve_join_url(options.join_url)
            bot_settings = dataclasses.replace(_bot_settings_from_env(platform, require_url=False), join_url=url)
        except MeetConfigError as exc:
            _print(f"\nConfiguration problem: {exc}")
            return 2
        bot = _build_bot(platform, bot_settings)
        try:
            return await _dry_run(bot, url, platform=platform)
        except (MeetConfigError, MeetJoinError) as exc:
            _print(f"\n{exc}")
            return 2
        finally:
            await bot.stop()

    # -- am I a dispatched worker? -------------------------------------------
    # A container gets its tenant's policy and dossier from the control plane
    # over a one-meeting token rather than from a database connection, and
    # reports every pipeline event back the same way. Without a bundle it must
    # not join: a delegate with no Representation Policy has no notion of what
    # is RED, which is worse than not attending at all.
    bundle: MeetingBundle | None = None
    emitter = build_emitter()
    if is_worker():
        try:
            bundle = await fetch_bundle(callback_url() or "", job_token() or "")
        except BundleError as exc:
            _print(f"\n[WORKER] {exc}")
            return 2
        _print(
            f"[WORKER] representing {bundle.principal_name} ({bundle.profile.user_id}); "
            f"{len(bundle.profile.policy)} policy rules, consent mode {bundle.consent_mode}"
        )

    # -- configuration, before anything heavy is loaded ---------------------
    store: ProfileStore | None = BundleProfileStore(bundle.profile) if bundle is not None else None
    try:
        config = SessionConfig.from_options(
            options,
            store=store,
            user_id=bundle.profile.user_id if bundle is not None else None,
        )
    except (MeetConfigError, VoiceConfigError) as exc:
        _print(f"\nConfiguration problem: {exc}")
        return 2

    # -- components -----------------------------------------------------------
    # A worker has no Telegram token and no database, so both of those go home
    # over the job token instead: escalations and the briefing are delivered by
    # the control plane on the tenant's own channel, and the report is archived
    # there. Without this a containerised meeting would have no YELLOW tier and
    # would leave no trace of the briefing it sent.
    archive = (
        RelayReportArchive(callback_url() or "", job_token() or "")
        if bundle is not None
        else MeetingHistoryStore()
    )
    components = await SessionComponents.build(archive=archive, store=store)
    if bundle is not None:
        components.dossiers = BundleDossierStore(
            bundle.profile.user_id, bundle.dossier_markdown, bundle.dossier_built_by
        )
        components.turns.components.dossiers = components.dossiers
        relay = RelayEscalationBridge(
            callback_url() or "", job_token() or "", timeout_seconds=_escalation_timeout()
        )
        relay.note_availability(bundle.escalation_available)
        components.bridge = relay
        components.turns.bridge = relay
        components.reporter.bridge = relay
    if emitter is not None:
        emitter.start()
    session = MeetingSession(
        config,
        components,
        emit=emitter.emit if emitter is not None else None,
        consent_mode=mode_from_env(
            mode_from_value(bundle.consent_mode) if bundle is not None else ConsentMode.NOTIFY_ONLY
        ),
        on_consent=(
            (lambda decision: emitter.emit(
                "consent.decided",
                {
                    "meeting_id": config.meeting_id,
                    "state": decision.outcome.value,
                    "mode": decision.mode.value,
                    "method": decision.method,
                    "granted_by": decision.granted_by,
                    "evidence": decision.evidence,
                    "disclosure": decision.disclosure_text,
                },
            ))
            if emitter is not None
            else None
        ),
    )
    bridge, memory, reporter = components.bridge, components.memory, components.reporter

    async def conclude_command(args: str) -> str:
        reports = await reporter.conclude_all(memory)
        return "Concluded " + ", ".join(r.meeting_id for r in reports) + "." if reports else "No active meeting."

    async def leave_command(args: str) -> str:
        session.request_stop("/leave from Telegram")
        return f"Leaving {session.code} and sending the briefing."

    async def status_command(args: str) -> str:
        state = memory.get(session.meeting_id)
        topic = state.active_topic if state else None
        turns_seen = state.stats.utterances if state else 0
        return (
            f"{config.platform.label} {session.code}: {session.status.value}; "
            f"topic {topic or '-'}; {turns_seen} utterances heard."
        )

    if isinstance(bridge, TelegramEscalationBridge):
        bridge.register_command("conclude", conclude_command)
        bridge.register_command("leave", leave_command)
        bridge.register_command("status", status_command)

    # -- the brain must be alive before anything else is worth doing ---------
    # A retired model id used to degrade every component to a silent fallback
    # and produce a delegate that sat in a real meeting quoting fragments of
    # PDFs. Joining with a dead brain is worse than not joining.
    if not await components.check_llm() and not allow_no_llm():
        components.close_knowledge()
        return 2
    # A dispatched worker was handed its dossier in the bundle and has no
    # business recompiling it: it holds a read-only store, the documents it can
    # see are only the ones mounted for this meeting, and a rebuild would spend
    # the tenant's SMART model budget at the moment a call is starting.
    dossier_status = (
        f"dossier: supplied by the control plane ({len(bundle.dossier_markdown)} chars)"
        if bundle is not None
        else await components.ensure_dossier(config.voice.user_id, config.principal_name)
    )

    _print(f"DELEG8 meeting session: {config.platform.label} - {config.url}")
    _print(f"  escalation: {bridge.describe()}")
    _print(f"  knowledge:  {components.knowledge.describe()}")
    _print(f"  {dossier_status}")
    _print(f"  {components.turns.describe()}")
    _print(f"  briefing:   {reporter.describe()}")
    _print(f"  audio:      {config.describe_audio()} (run python -m src.audio.device_setup to wire the virtual cables)")

    try:
        await bridge.start()
    except EscalationConfigError as exc:
        _print(f"\n{exc}")
        components.close_knowledge()
        return 2

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, session.request_stop, f"signal {sig.name}")

    try:
        return await session.run()
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        if emitter is not None:
            await emitter.stop()
        await bridge.stop()
        components.close_knowledge()
