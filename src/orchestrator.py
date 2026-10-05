"""
Voice pipeline orchestrator: the master loop that ties the audio layer to the brain.

    mic -> AudioIO.mic_queue -> SttStream -> Utterance queue
                                                |
                                                v
                              PolicyEngine.evaluate_async()  (via TurnProcessor)
                                |            |            |
                            AUTO_REPLY    ESCALATE      SILENT
                                |            |            |
                                |     Telegram, wait      |
                                |            |            |
                                v            v            v
                              TtsStream.speak()        (nothing)
                                     |
                              AudioIO.playback_queue -> speaker

    STT interim words while the bot is speaking -> BargeInHandler.interrupt()
        -> playback flushed, Cartesia context cancelled, "[INTERRUPTED]".

Turns are processed one at a time. Barge-in is independent of the loop: it
is driven straight from STT events, so it works even while the loop is
blocked inside ``speak()``.

Every turn also updates the live meeting memory through the TurnProcessor;
the session runner concludes the meeting (executive briefing to Telegram)
once the loop stops.

Sprint 6: every step of a turn is also reported through an optional
:data:`~src.events.EventCallback` so the web console can show the delegate
listening, thinking, waiting on Telegram, and speaking in real time. The
console output below is unchanged; the events are additive.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.audio.audio_io import AudioIO
from src.audio.barge_in_handler import BargeInHandler
from src.audio.config import VoiceSettings
from src.audio.fillers import FillerPicker
from src.audio.stt_stream import SttStream, Utterance
from src.audio.tts_stream import TtsStream
from src.brain.models import DecisionState, MeetingContext
from src.brain.planner import GuardVerdict
from src.brain.policy_engine import PolicyEngine
from src.events import EventCallback, safe_emit
from src.turn_processor import FollowUp, TurnProcessor

if TYPE_CHECKING:  # pragma: no cover - typing only, to keep this module importable
    from src.compliance.consent import ConsentGate
    from src.usage.meter import UsageMeter

logger = logging.getLogger("deleg8.orchestrator")

_FILLER_AFTER_SECONDS: float = 0.45
"""How long a turn may think in silence before a holding phrase is played."""
_FILLER_FLOOR_SECONDS: float = 0.5
"""How long to wait for the turn to reach the planner at all. A turn silenced by
the floor check never gets here, so it never emits a stray holding phrase."""


@dataclass
class SessionStats:
    """Counters printed at the end of a voice session."""

    utterances: int = 0
    auto_replies: int = 0
    escalations: int = 0
    silent: int = 0
    interruptions: int = 0
    brain_ms: list[float] = field(default_factory=list)
    first_audio_ms: list[float] = field(default_factory=list)

    def summary(self) -> str:
        """One-line human summary."""
        avg = lambda xs: f"{sum(xs) / len(xs):.0f}ms" if xs else "n/a"  # noqa: E731
        return (
            f"{self.utterances} utterances, {self.auto_replies} auto replies, {self.escalations} escalations, "
            f"{self.silent} silent, {self.interruptions} interruptions; brain avg {avg(self.brain_ms)}, "
            f"first audio avg {avg(self.first_audio_ms)}"
        )

    def as_dict(self) -> dict[str, Any]:
        """Counters plus averages, for the API."""
        avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
        return {
            "utterances": self.utterances,
            "auto_replies": self.auto_replies,
            "escalations": self.escalations,
            "silent": self.silent,
            "interruptions": self.interruptions,
            "brain_avg_ms": avg(self.brain_ms),
            "first_audio_avg_ms": avg(self.first_audio_ms),
        }


class VoiceOrchestrator:
    """Runs the live voice loop until cancelled."""

    def __init__(
        self,
        settings: VoiceSettings,
        audio_io: AudioIO,
        stt: SttStream,
        tts: TtsStream,
        barge_in: BargeInHandler,
        engine: PolicyEngine,
        turn_processor: TurnProcessor,
        utterances: asyncio.Queue[Utterance],
        emit: EventCallback | None = None,
        participant_count_provider: Callable[[], int | None] | None = None,
        consent: "ConsentGate | None" = None,
        meter: "UsageMeter | None" = None,
    ) -> None:
        """Wire every component. Nothing starts until :meth:`run`.

        ``emit`` (optional) receives ``(event_type, payload)`` for every step of
        every turn; see the module docstring. ``participant_count_provider``
        (optional) is polled once per turn for a live headcount (e.g. read from
        the Google Meet UI); when absent, ``MeetingContext.participant_count``
        stays ``None`` and the planner falls back to one-on-one behaviour.

        ``consent`` (optional) gates retention: while it is shut, an utterance
        is classified for agreement and then discarded, so nothing reaches the
        policy engine, meeting memory, the commitments ledger or the briefing.
        ``meter`` (optional) records transcribed seconds and synthesised
        characters against the tenant's quota.
        """
        self._settings = settings
        self._audio = audio_io
        self._stt = stt
        self._tts = tts
        self._barge_in = barge_in
        self._engine = engine
        self._turns = turn_processor
        self._utterances = utterances
        self._emit_cb = emit
        self._participant_count_provider = participant_count_provider
        self._consent = consent
        self._meter = meter
        self._fillers = FillerPicker()
        self.stats = SessionStats()
        self._stt_task: asyncio.Task[None] | None = None
        self.ready = False
        """True once audio, TTS and STT are all up and the loop is listening."""
        self.is_speaking = False
        self.awaiting_human = False
        self._leave_requested = False
        self.leave_reason: str | None = None
        """Set once a leave has been confirmed; :class:`~src.gateway.session.MeetingSession`
        reads this after :meth:`run` returns to give the session a descriptive stop reason."""

    def _emit(self, event_type: str, **payload: Any) -> None:
        safe_emit(self._emit_cb, event_type, payload)

    async def run(self) -> None:
        """Start audio, STT and TTS, then process utterances until cancelled.

        Everything after opening the mic lives inside the try block: if TTS
        or STT fails to start, ``shutdown()`` still runs and closes the mic
        stream. Leaving PortAudio's background thread running past process
        exit is what causes a native crash (SIGTRAP) during interpreter
        shutdown, so audio.start() must never be left unpaired.
        """
        await self._audio.start()
        try:
            await self._tts.start()
            self._stt_task = asyncio.create_task(self._stt.run(), name="deleg8-stt")
            await self._wait_for_stt()
            self.ready = True
            print("[READY] Listening. Speak into the microphone. Ctrl+C to stop.", flush=True)
            self._emit(
                "voice.ready",
                meeting_id=self._settings.meeting_id,
                speaker_label=self._settings.speaker_label,
                voice=self._tts.voice_name,
                half_duplex=self._settings.half_duplex,
            )
            while True:
                utterance_task = asyncio.ensure_future(self._utterances.get())
                followup_task = asyncio.ensure_future(self._turns.followups.get())
                waiters = {utterance_task, followup_task, self._stt_task}
                done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                if self._stt_task in done:
                    self._discard(utterance_task, followup_task)
                    self._stt_task.result()  # raises the STT failure (e.g. bad API key)
                    raise RuntimeError("STT stream ended unexpectedly")

                # Both may have completed in the same wait. Cancel only the one
                # that has not: cancelling a queue getter that already holds an
                # item silently drops it, which would lose an utterance.
                if followup_task in done:
                    await self._handle_followup(followup_task.result())
                else:
                    followup_task.cancel()
                if utterance_task in done:
                    await self._handle(utterance_task.result())
                else:
                    utterance_task.cancel()
                if self._leave_requested:
                    print("[MEET] leaving the call at the caller's request.", flush=True)
                    self._emit("voice.leave_requested", meeting_id=self._settings.meeting_id)
                    break
        finally:
            await self.shutdown()

    @staticmethod
    def _discard(*tasks: asyncio.Task[Any]) -> None:
        """Cancel tasks that are still pending; leave completed ones alone."""
        for task in tasks:
            if not task.done():
                task.cancel()

    async def _wait_for_stt(self) -> None:
        """Wait until Deepgram is connected, surfacing a startup failure immediately."""
        assert self._stt_task is not None
        waiter = asyncio.ensure_future(self._stt.wait_connected(timeout=15.0))
        done, _ = await asyncio.wait({waiter, self._stt_task}, return_when=asyncio.FIRST_COMPLETED)
        if self._stt_task in done:
            waiter.cancel()
            self._stt_task.result()
            raise RuntimeError("STT stream ended before connecting")
        waiter.result()

    async def shutdown(self) -> None:
        """Stop everything in dependency order. Idempotent."""
        if self._barge_in.is_bot_speaking:
            await self._barge_in.interrupt("shutdown")
        await self._stt.stop()
        if self._stt_task is not None and not self._stt_task.done():
            self._stt_task.cancel()
            try:
                await self._stt_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down regardless
                pass
        await self._tts.stop()
        await self._audio.stop()
        self.stats.interruptions = self._barge_in.interruptions
        self.ready = False
        self.is_speaking = False
        self.awaiting_human = False
        logger.info("session: %s", self.stats.summary())
        self._emit("voice.stopped", stats=self.stats.as_dict())

    # ------------------------------------------------------------------ #
    # One turn
    # ------------------------------------------------------------------ #

    async def _handle(self, utterance: Utterance) -> None:
        """Plan the turn, cover the thinking time, speak."""
        self.stats.utterances += 1
        tag = " (interrupted the bot)" if utterance.interrupted_bot else ""
        print(f"\n[YOU] {utterance.text}{tag}", flush=True)
        if self._meter is not None:
            self._meter.record_stt(utterance.duration_seconds, detail="utterance")

        if self._consent is not None and not await self._pass_consent_gate(utterance):
            return

        participant_count = self._participant_count_provider() if self._participant_count_provider is not None else None
        context = MeetingContext(
            meeting_id=self._settings.meeting_id,
            user_id=self._settings.user_id,
            transcript=utterance.text,
            speaker=self._settings.speaker_label,
            participants=list(self._settings.participants),
            participant_count=participant_count,
        )
        self._emit(
            "utterance",
            meeting_id=context.meeting_id,
            speaker=context.speaker,
            text=context.transcript,
            confidence=round(utterance.confidence, 3),
            interrupted_bot=utterance.interrupted_bot,
            duration_seconds=round(utterance.duration_seconds, 2),
            timestamp=context.timestamp,
        )

        t0 = time.monotonic()
        planning = asyncio.Event()
        turn_task = asyncio.create_task(
            self._turns.process_turn(context, on_planning=planning.set), name="deleg8-turn"
        )
        await self._cover_thinking_time(context, planning, turn_task)

        try:
            result = await turn_task
        except Exception as exc:  # noqa: BLE001 - one bad turn must not end the meeting
            logger.exception("turn failed")
            print(f"[ERROR] turn failed: {exc}", flush=True)
            return
        brain_ms = (time.monotonic() - t0) * 1000
        self.stats.brain_ms.append(brain_ms)

        decision = result.decision
        tier = decision.tier.value if decision.tier else "-"
        kind = result.turn_type.value if result.turn_type else "-"
        print(
            f"[BRAIN] {decision.state.value} type={kind} topic={decision.topic or '-'} tier={tier} "
            f"({brain_ms:.0f}ms): {decision.reason}",
            flush=True,
        )
        if result.guard_verdict is not None and result.guard_verdict is not GuardVerdict.PASSED:
            print(f"[GUARD] {result.guard_verdict.value}: {decision.trace[-2].outcome if len(decision.trace) > 1 else ''}", flush=True)
        if decision.grounding is not None:
            print(f"[GROUNDING] {decision.grounding.describe()}", flush=True)
        self._emit(
            "decision",
            meeting_id=context.meeting_id,
            speaker=context.speaker,
            transcript=context.transcript,
            decision=decision,
            brain_ms=round(brain_ms, 1),
            turn_type=kind,
            guard_verdict=result.guard_verdict.value if result.guard_verdict else None,
        )

        if decision.state is DecisionState.SILENT:
            self.stats.silent += 1
        elif decision.state is DecisionState.ESCALATE:
            self.stats.escalations += 1
            self._emit(
                "escalation.requested",
                meeting_id=context.meeting_id,
                speaker=context.speaker,
                transcript=context.transcript,
                topic=decision.topic,
                tier=decision.tier,
                reason=decision.reason,
            )
        else:
            self.stats.auto_replies += 1

        self._report_memory(result)
        self._emit(
            "turn",
            meeting_id=context.meeting_id,
            speaker=context.speaker,
            transcript=context.transcript,
            state=result.state,
            response=result.response,
            proposed_response=result.proposed_response,
            escalation=result.escalation,
            memory=result.memory,
            turn_type=kind,
            commitment_ids=result.commitment_ids,
        )
        if result.response:
            await self._say(result.response, context.meeting_id, label=f"turn-{self.stats.utterances}")
        if result.should_leave:
            self._leave_requested = True
            self.leave_reason = f"asked to leave by {context.speaker or 'the caller'}"

    async def _pass_consent_gate(self, utterance: Utterance) -> bool:
        """Run one utterance past the consent gate. ``False`` means "discard it".

        While the gate is shut this is the *only* thing that happens to an
        utterance: it is read for a yes or a no, the delegate answers if there
        is something to say, and the text is dropped. It never reaches the
        policy engine, so it is never written to memory, never extracted into a
        commitment, and never appears in a briefing.
        """
        gate = self._consent
        assert gate is not None
        was_open = gate.may_retain
        signal, line = gate.observe(utterance.text, self._settings.speaker_label)
        if line:
            await self._say(line, self._settings.meeting_id, label="consent")
        self._emit(
            "consent.signal",
            meeting_id=self._settings.meeting_id,
            signal=signal.value,
            state=gate.state.value,
            may_retain=gate.may_retain,
            discarded=gate.discarded_utterances,
        )
        if gate.is_refused:
            self._leave_requested = True
            self.leave_reason = f"recording consent {gate.state.value}"
            print(f"[CONSENT] {gate.describe()}; leaving without retaining anything.", flush=True)
            return False
        if not gate.may_retain:
            print(f"[CONSENT] holding: {gate.describe()} (utterance discarded)", flush=True)
            return False
        if not was_open:
            # The utterance that granted consent was itself heard while the gate
            # was shut, so it stays discarded; the meeting starts from the next one.
            print(f"[CONSENT] {gate.describe()}; transcript retention starts now.", flush=True)
            return False
        return True

    async def _cover_thinking_time(
        self, context: MeetingContext, planning: asyncio.Event, turn_task: asyncio.Task[Any]
    ) -> None:
        """Play a short holding phrase if the reply will not be ready quickly.

        Measured end to end, a grounded reply takes 1.6-2.4s: model call plus
        time-to-first-audio. That is long enough on a live call that people start
        talking over the delegate or assume it is broken. Rather than chase a
        latency number the model cannot hit, the gap is filled - the way a person
        says "sure, one moment" while they look something up.

        The phrase only plays once the turn has actually reached the planner
        (``planning``), so a turn that the floor check silences never produces a
        stray "let me check" into the room. The phrase itself is picked to fit
        what was actually asked (:mod:`src.audio.fillers`) rather than a fixed
        list, and avoids repeating one just used.

        This ``await``s the filler's speech to completion before returning, so
        the caller (:meth:`_handle`) never starts the real answer while the
        filler is still being spoken - the two can never overlap because they
        are the same sequential call chain, not concurrent tasks.
        """
        try:
            await asyncio.wait_for(asyncio.shield(planning.wait()), timeout=_FILLER_FLOOR_SECONDS)
        except asyncio.TimeoutError:
            return
        if turn_task.done():
            return
        done, _ = await asyncio.wait({turn_task}, timeout=_FILLER_AFTER_SECONDS)
        if done or self.is_speaking:
            return
        await self._say(self._fillers.pick(context.transcript), context.meeting_id, label="filler", filler=True)

    async def say(self, text: str, *, label: str) -> None:
        """Speak one line the loop did not plan - the consent disclosure, for instance."""
        await self._say(text, self._settings.meeting_id, label=label)

    async def _say(self, text: str, meeting_id: str, *, label: str, filler: bool = False) -> None:
        """Speak one line, muting the mic first in half-duplex mode."""
        print(f"[BOT]{' (holding)' if filler else ''} {text}", flush=True)
        if self._meter is not None:
            self._meter.record_tts(len(text), model=self._settings.cartesia_model_id, detail=label)
        if self._settings.half_duplex:
            self._audio.set_muted(True)
        self.is_speaking = True
        self._emit("speech.start", meeting_id=meeting_id, text=text, filler=filler)
        try:
            spoken = await self._tts.speak(text, label=label)
        finally:
            self.is_speaking = False
            if self._settings.half_duplex:
                self._audio.set_muted(False)
        if spoken.first_audio_ms is not None and not filler:
            self.stats.first_audio_ms.append(spoken.first_audio_ms)
        status = "interrupted" if spoken.interrupted else "done"
        first = f"{spoken.first_audio_ms:.0f}ms" if spoken.first_audio_ms is not None else "n/a"
        print(f"[TTS] {status}: first audio {first}, {spoken.audio_seconds:.1f}s of audio in {spoken.elapsed_seconds:.1f}s", flush=True)
        self._emit(
            "speech.end",
            meeting_id=meeting_id,
            text=text,
            filler=filler,
            interrupted=spoken.interrupted,
            first_audio_ms=round(spoken.first_audio_ms, 1) if spoken.first_audio_ms is not None else None,
            audio_seconds=round(spoken.audio_seconds, 2),
            elapsed_seconds=round(spoken.elapsed_seconds, 2),
        )

    async def _handle_followup(self, followup: FollowUp) -> None:
        """Speak an answer that arrived from the principal after its turn had ended."""
        print(f"[TELEGRAM] {followup.reason}; speaking the follow-up", flush=True)
        self._emit("escalation.resolved", meeting_id=followup.meeting_id, text=followup.text, reason=followup.reason)
        await self._say(followup.text, followup.meeting_id, label="followup")

    @staticmethod
    def _report_memory(result: Any) -> None:
        """Print what the turn changed in meeting memory, if anything."""
        memory = result.memory
        if memory is None or not memory.changed:
            return
        parts = [f"topic={memory.active_topic or '-'}"]
        parts += [f"decision: {d.text}" for d in memory.new_decisions]
        parts += [f"action: {a.description} ({a.owner}{', due ' + a.due_label if a.due_label else ''})" for a in memory.new_actions]
        parts += [f"open question: {q.text}" for q in memory.new_questions]
        if result.commitment_ids:
            parts.append("commitments: " + ", ".join(result.commitment_ids))
        print("[MEMORY] " + " | ".join(parts), flush=True)
