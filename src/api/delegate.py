"""
Dispatch, tracking and recall of meeting delegates - many at once, per tenant.

What this replaces
------------------
The previous :class:`DelegateManager` held exactly one
:class:`~src.gateway.session.MeetingSession`, in one process, on one shared
bundle of components, and refused a second dispatch with "a delegate is
already attending". That is a single-user program: two customers cannot both
be in a meeting, and one customer cannot be in two.

Now each tenant's :class:`~src.tenancy.workspace.Workspace` owns one of these,
and it holds a dictionary of :class:`RunningMeeting` keyed by meeting id.
Where the work actually happens is a :class:`~src.dispatch.base.Dispatcher`
decision:

* :class:`~src.dispatch.container.ContainerDispatcher` runs one
  ``deleg8-worker`` container per meeting - separate Chromium, separate audio
  devices, separate address space. This is the shipping path and the reason
  concurrency is real rather than nominal.
* :class:`~src.dispatch.inprocess.InProcessDispatcher` runs it as a task here,
  for a laptop with no Docker.

Both report through the same ``emit(event_type, payload)`` callback - the
in-process session calls it directly, the container POSTs to
``/api/v1/worker/events`` and the route replays it through
:meth:`DelegateManager.ingest_event` - so the live feed, the activity state
machine and the ``meetings`` row are identical either way.

Three limits are enforced here rather than left to the machine to discover:
the tenant's own concurrency cap (from their plan), the deployment-wide cap
(``DELEG8_MAX_CONCURRENT_MEETINGS``), and their monthly quota. A refusal names
which one was hit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator

from src.api.events import INTERIM_EVENT, LiveFeedHub
from src.compliance.consent import ConsentDecision, mode_from_value
from src.db import repo
from src.db.base import session_scope
from src.db.models import ConsentMode, ConsentState, MeetingRuntime, MeetingStatus
from src.dispatch.base import (
    DispatchError,
    DispatchHandle,
    DispatchMode,
    DispatchUnavailable,
    MeetingJob,
    configured_mode,
)
from src.gateway.bot_base import (
    MeetConfigError,
    MeetStatus,
    detect_platform,
    meeting_identifier,
)
from src.usage.meter import QuotaExceeded, UsageMeter, check_quota

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.tenancy.workspace import Workspace

logger = logging.getLogger("deleg8.api.delegate")

_CONNECTING_PHASES: frozenset[str] = frozenset(
    {MeetStatus.STARTING.value, MeetStatus.LOGGING_IN.value, MeetStatus.JOINING.value,
     MeetStatus.LOBBY.value, MeetStatus.RECONNECTING.value}
)
_TERMINAL_PHASES: frozenset[str] = frozenset(
    {MeetStatus.REMOVED.value, MeetStatus.ENDED.value, MeetStatus.LEFT.value, MeetStatus.ERROR.value}
)
SHUTDOWN_GRACE_SECONDS: float = 90.0
"""How long the server waits for a delegate to leave the call and send its briefing."""

CONTAINER_POLL_SECONDS: float = 5.0
"""How often a container's liveness is checked. Events arrive by callback; this
is only the backstop that notices a worker that died without saying so."""


def max_concurrent_meetings() -> int:
    """``DELEG8_MAX_CONCURRENT_MEETINGS`` (default 8) - the whole deployment's cap."""
    raw = os.getenv("DELEG8_MAX_CONCURRENT_MEETINGS", "").strip()
    try:
        return max(1, int(raw)) if raw else 8
    except ValueError:
        return 8


class DelegateActivity(str, Enum):
    """What a delegate is doing right now, as shown on the Live Monitor."""

    IDLE = "IDLE"
    CONNECTING = "CONNECTING"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    AWAITING_CONSENT = "AWAITING_CONSENT"
    SPEAKING = "SPEAKING"
    LEAVING = "LEAVING"


class SessionCounters(BaseModel):
    """Voice-loop counters for one session."""

    utterances: int = 0
    auto_replies: int = 0
    escalations: int = 0
    silent: int = 0
    interruptions: int = 0
    brain_avg_ms: float | None = None
    first_audio_avg_ms: float | None = None


class DelegateStatus(BaseModel):
    """Everything the console shows about one delegate."""

    active: bool = Field(..., description="True while this meeting is running.")
    phase: str = Field(..., description="Meet lifecycle: IDLE, STARTING, LOGGING_IN, JOINING, LOBBY, IN_CALL, ...")
    activity: DelegateActivity
    meeting_id: str | None = None
    meeting_row_id: str | None = Field(default=None, description="Database id of the meeting.")
    meeting_code: str | None = None
    platform: str | None = Field(default=None, description="google_meet, zoom, or teams.")
    runtime: str | None = Field(default=None, description="container or inprocess.")
    container: str | None = Field(default=None, description="Container name, when containerised.")
    join_url: str | None = None
    speaker_label: str | None = None
    principal: str | None = None
    started_at: datetime | None = None
    joined_at: datetime | None = None
    finished_at: datetime | None = None
    detail: str | None = Field(default=None, description="Last reason reported by the meeting bot.")
    stop_reason: str | None = None
    exit_code: int | None = None
    error: str | None = None
    active_topic: str | None = None
    current_speaker: str | None = None
    last_utterance: str | None = None
    speaking_text: str | None = Field(default=None, description="What the delegate is saying, while SPEAKING.")
    pending_escalations: int = 0
    consent_mode: str = ConsentMode.NOTIFY_ONLY.value
    consent_state: str = ConsentState.NOT_REQUIRED.value
    retaining: bool = Field(default=True, description="False while consent blocks transcript retention.")
    counters: SessionCounters = Field(default_factory=SessionCounters)
    live_subscribers: int = 0
    updated_at: datetime


class JoinRequest(BaseModel):
    """``POST /api/v1/meetings/join`` body."""

    url: str = Field(
        ..., min_length=1,
        description="Meeting link - Google Meet, Zoom, or Microsoft Teams, e.g. https://meet.google.com/abc-defg-hij",
    )
    meeting_id: str | None = Field(
        default=None, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="Memory / briefing id; defaults to <platform>-<code>.",
    )
    speaker_label: str = Field(default="Meeting", min_length=1, max_length=40, description="Label for whoever is heard in the call.")
    half_duplex: bool = Field(default=False, description="Mute the bot's mic while it speaks (disables barge-in).")
    briefing: bool = Field(default=True, description="Send the executive briefing when the session ends.")
    max_minutes: float | None = Field(default=None, gt=0, le=600, description="Leave automatically after this long.")
    consent_mode: str | None = Field(
        default=None,
        description="Override the account default: notify_only, all_party, or single_party.",
    )

    @field_validator("url")
    @classmethod
    def _valid_meeting_link(cls, url: str) -> str:
        url = url.strip()
        try:
            detect_platform(url)
        except MeetConfigError as exc:
            raise ValueError(str(exc)) from exc
        return url


class AbortRequest(BaseModel):
    """``POST /api/v1/meetings/abort`` body."""

    reason: str = Field(default="Emergency recall from the web console", min_length=1, max_length=200)
    meeting_id: str | None = Field(
        default=None, description="Which meeting to recall; omit when only one is running."
    )


class DelegateBusyError(RuntimeError):
    """A concurrency limit was reached."""


class DelegateIdleError(RuntimeError):
    """No delegate is attending the named meeting."""


class DelegateUnavailableError(RuntimeError):
    """No dispatcher can run a meeting on this machine."""


class RunningMeeting:
    """One dispatched meeting and everything known about it."""

    def __init__(
        self,
        *,
        job: MeetingJob,
        handle: DispatchHandle,
        dispatcher: Any,
        principal_name: str,
    ) -> None:
        """Bind a launched job to the dispatcher that owns it."""
        self.job = job
        self.handle = handle
        self.dispatcher = dispatcher
        self.principal_name = principal_name
        self.activity = DelegateActivity.CONNECTING
        self.phase = MeetStatus.STARTING.value
        self.started_at = datetime.now(timezone.utc)
        self.joined_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.exit_code: int | None = None
        self.error: str | None = None
        self.stop_reason: str | None = None
        self.detail: str | None = None
        self.current_speaker: str | None = None
        self.last_utterance: str | None = None
        self.speaking_text: str | None = None
        self.active_topic: str | None = None
        self.counters = SessionCounters()
        self.consent_state = (
            ConsentState.PENDING if job.consent_mode is ConsentMode.ALL_PARTY else ConsentState.NOT_REQUIRED
        )
        self.monitor: asyncio.Task[None] | None = None
        self.meter: UsageMeter | None = None
        self.stopping = False

    @property
    def meeting_key(self) -> str:
        """The id memory, briefings and the console use."""
        return self.job.meeting_key

    @property
    def active(self) -> bool:
        """True until the meeting reaches a terminal state."""
        return self.finished_at is None

    @property
    def retaining(self) -> bool:
        """True when the transcript is actually being kept."""
        return self.consent_state.may_retain

    def status(self, *, pending_escalations: int, subscribers: int) -> DelegateStatus:
        """Project onto the console's shape."""
        return DelegateStatus(
            active=self.active,
            phase=self.phase,
            activity=self.activity if self.active else DelegateActivity.IDLE,
            meeting_id=self.meeting_key,
            meeting_row_id=str(self.job.meeting_id),
            meeting_code=self.job.code,
            platform=self.job.platform,
            runtime=self.handle.runtime.value,
            container=str(self.handle.extra.get("container_name") or "") or None,
            join_url=self.job.join_url,
            speaker_label=self.job.speaker_label,
            principal=self.principal_name,
            started_at=self.started_at,
            joined_at=self.joined_at,
            finished_at=self.finished_at,
            detail=self.detail,
            stop_reason=self.stop_reason,
            exit_code=self.exit_code,
            error=self.error,
            active_topic=self.active_topic,
            current_speaker=self.current_speaker if self.active else None,
            last_utterance=self.last_utterance if self.active else None,
            speaking_text=self.speaking_text if self.active else None,
            pending_escalations=pending_escalations,
            consent_mode=self.job.consent_mode.value,
            consent_state=self.consent_state.value,
            retaining=self.retaining,
            counters=self.counters,
            live_subscribers=subscribers,
            updated_at=datetime.now(timezone.utc),
        )


def idle_status(*, pending_escalations: int = 0, subscribers: int = 0) -> DelegateStatus:
    """The status shown when a tenant has no meeting running."""
    return DelegateStatus(
        active=False,
        phase=MeetStatus.IDLE.value,
        activity=DelegateActivity.IDLE,
        pending_escalations=pending_escalations,
        live_subscribers=subscribers,
        updated_at=datetime.now(timezone.utc),
    )


class DelegateManager:
    """Every meeting one tenant currently has running, whatever runtime it is on."""

    def __init__(self, workspace: Workspace, hub: LiveFeedHub) -> None:
        """Bind to a tenant's workspace and their live feed."""
        self._workspace = workspace
        self._hub = hub
        self._meetings: dict[str, RunningMeeting] = {}
        self._last: RunningMeeting | None = None
        self._last_status_key: dict[str, tuple[str, str]] = {}

    # ------------------------------------------------------------------ #
    # Read side
    # ------------------------------------------------------------------ #

    @property
    def user_id(self) -> uuid.UUID:
        """The tenant this manager belongs to."""
        return self._workspace.user_id

    @property
    def is_active(self) -> bool:
        """True while any meeting is running."""
        return any(m.active for m in self._meetings.values())

    @property
    def active_count(self) -> int:
        """How many meetings this tenant has running."""
        return sum(1 for m in self._meetings.values() if m.active)

    def running(self) -> list[RunningMeeting]:
        """Every live meeting, oldest first."""
        return sorted((m for m in self._meetings.values() if m.active), key=lambda m: m.started_at)

    def get(self, meeting_key: str) -> RunningMeeting | None:
        """One meeting by id."""
        return self._meetings.get(meeting_key)

    def _pending_escalations(self) -> int:
        bridge = self._workspace.bridge
        return int(getattr(bridge, "pending_count", 0) or 0)

    def status(self, meeting_key: str | None = None) -> DelegateStatus:
        """One meeting's status: the named one, the oldest running one, or the last finished one.

        The console's Overview and the ``/status`` Telegram command both want
        "what is my delegate doing", singular, which stays meaningful for the
        common case of one meeting at a time; :meth:`statuses` is the honest
        answer when there are several.
        """
        meeting = self._meetings.get(meeting_key) if meeting_key else None
        if meeting is None:
            running = self.running()
            meeting = running[0] if running else self._last
        if meeting is None:
            return idle_status(
                pending_escalations=self._pending_escalations(), subscribers=self._hub.subscribers
            )
        if meeting.active:
            state = self._workspace.components.memory.get(meeting.meeting_key)
            meeting.active_topic = state.active_topic if state is not None else meeting.active_topic
        return meeting.status(
            pending_escalations=self._pending_escalations(), subscribers=self._hub.subscribers
        )

    def statuses(self) -> list[DelegateStatus]:
        """Every meeting this tenant has running."""
        return [
            m.status(pending_escalations=self._pending_escalations(), subscribers=self._hub.subscribers)
            for m in self.running()
        ]

    # ------------------------------------------------------------------ #
    # Launch
    # ------------------------------------------------------------------ #

    async def launch(self, request: JoinRequest) -> DelegateStatus:
        """Dispatch a delegate to ``request.url``.

        Raises:
            DelegateBusyError: a concurrency limit was reached, or that meeting is already running.
            QuotaExceeded: the tenant's monthly allowance is spent.
            DelegateUnavailableError: no dispatcher can run here.
            MeetConfigError, VoiceConfigError: missing or malformed configuration.
        """
        workspace = self._workspace
        platform = detect_platform(request.url)
        code = meeting_identifier(request.url, platform=platform)
        base_key = (request.meeting_id or "").strip() or f"{platform.value.split('_')[-1]}-{code}"

        self._reap()
        if base_key in self._meetings and self._meetings[base_key].active:
            raise DelegateBusyError(f"a delegate is already attending {base_key}; recall it first")

        credentials = workspace.credentials
        plan_limit = workspace.plan.max_concurrent_meetings
        if self.active_count >= plan_limit:
            raise DelegateBusyError(
                f"your plan allows {plan_limit} meeting(s) at once; recall one before starting another"
            )
        global_active = workspace.manager.total_active_meetings() if workspace.manager else self.active_count
        if global_active >= max_concurrent_meetings():
            raise DelegateBusyError(
                "this server is at its meeting capacity right now; try again in a few minutes"
            )
        if not credentials.voice_ready:
            missing = [p for p in ("deepgram", "cartesia") if not credentials.get(p).present]
            raise DelegateUnavailableError(
                "the delegate has no voice: no "
                + " or ".join(missing)
                + " key is configured. Add your own in Settings."
            )

        consent_mode = mode_from_value(request.consent_mode or workspace.default_consent_mode.value)

        with session_scope() as session:
            check_quota(
                session,
                self.user_id,
                own_keys=workspace.fully_byo,
                org_settings=repo.org_settings_for_user(session, self.user_id),
            )
            meeting_key = repo.unique_meeting_key(session, self.user_id, base_key)
            from src.auth.security import new_job_token

            token, token_hash = new_job_token()
            row = repo.create_meeting(
                session,
                user_id=self.user_id,
                principal_id=workspace.principal_id,
                meeting_key=meeting_key,
                join_url=request.url,
                platform=platform.value,
                code=code,
                runtime=MeetingRuntime.IN_PROCESS.value,
                speaker_label=request.speaker_label,
                half_duplex=request.half_duplex,
                briefing=request.briefing,
                max_call_seconds=request.max_minutes * 60 if request.max_minutes else None,
                consent_mode=consent_mode.value,
                consent_state=(
                    ConsentState.PENDING.value
                    if consent_mode is ConsentMode.ALL_PARTY
                    else ConsentState.NOT_REQUIRED.value
                ),
                job_token_hash=token_hash,
            )
            meeting_row_id = row.id

        job = MeetingJob(
            meeting_id=meeting_row_id,
            user_id=self.user_id,
            meeting_key=meeting_key,
            principal_slug=workspace.principal_slug,
            principal_name=workspace.principal_name,
            join_url=request.url,
            platform=platform.value,
            code=code,
            speaker_label=request.speaker_label,
            half_duplex=request.half_duplex,
            briefing=request.briefing,
            max_call_seconds=request.max_minutes * 60 if request.max_minutes else None,
            consent_mode=consent_mode,
            collection=workspace.paths.collection,
            credentials=credentials.environment(),
            job_token=token,
            callback_url=workspace.callback_url,
        )

        # Compile the dossier here rather than in the worker. The worker holds a
        # read-only copy and could not build one anyway, and this is the moment
        # it matters: someone who uploads their CV and immediately dispatches
        # would otherwise get a delegate that knows nothing about them. Cheap
        # when the documents have not changed (a hash comparison, no model call).
        await workspace.ensure_dossier()

        dispatcher, why = await self._choose_dispatcher()
        if dispatcher is None:
            self._fail_meeting(meeting_row_id, why)
            raise DelegateUnavailableError(why)

        workspace.commitment_sink.bind(meeting_key, meeting_row_id)
        meter = workspace.new_meter(meeting_row_id)
        try:
            handle = await dispatcher.launch(job)
        except (DispatchUnavailable, DispatchError) as exc:
            self._fail_meeting(meeting_row_id, str(exc))
            raise DelegateUnavailableError(str(exc)) from exc

        meeting = RunningMeeting(
            job=job, handle=handle, dispatcher=dispatcher, principal_name=workspace.principal_name
        )
        meeting.meter = meter
        self._meetings[meeting_key] = meeting
        self._hub.clear_backlog()

        with session_scope() as session:
            row = repo.get_meeting(session, self.user_id, meeting_row_id)
            if row is not None:
                repo.update_meeting(
                    session, row,
                    status=MeetingStatus.DISPATCHING.value,
                    runtime=handle.runtime.value,
                    container_id=handle.reference if handle.runtime is MeetingRuntime.CONTAINER else None,
                    container_name=str(handle.extra.get("container_name") or "") or None,
                    started_at=meeting.started_at,
                    detail=handle.detail,
                )

        meeting.monitor = asyncio.create_task(
            self._monitor(meeting), name=f"deleg8-monitor-{meeting_key}"
        )
        logger.info(
            "user %s dispatched a delegate to %s as %s (%s)",
            self.user_id, job.join_url, meeting_key, handle.detail,
        )
        self._publish(meeting, force=True)
        return self.status(meeting_key)

    async def _choose_dispatcher(self) -> tuple[Any | None, str]:
        """The dispatcher this deployment should use, and why if there is none."""
        workspace = self._workspace
        mode = configured_mode()
        container, inprocess = workspace.container_dispatcher, workspace.inprocess_dispatcher
        if mode is DispatchMode.CONTAINER:
            ok, why = await container.available()
            return (container, "") if ok else (None, why)
        if mode is DispatchMode.IN_PROCESS:
            ok, why = await inprocess.available()
            return (inprocess, "") if ok else (None, why)
        ok, container_why = await container.available()
        if ok:
            return container, ""
        ok, inprocess_why = await inprocess.available()
        if ok:
            logger.info("falling back to in-process dispatch: %s", container_why)
            return inprocess, ""
        return None, f"no way to run a meeting here. Docker: {container_why}. In-process: {inprocess_why}"

    def _fail_meeting(self, meeting_row_id: uuid.UUID, error: str) -> None:
        """Mark a meeting that never started as failed, so it is not left pending forever."""
        with session_scope() as session:
            row = repo.get_meeting(session, self.user_id, meeting_row_id)
            if row is not None:
                repo.update_meeting(
                    session, row,
                    status=MeetingStatus.FAILED.value,
                    error=error[:2000],
                    ended_at=datetime.now(timezone.utc),
                    job_token_hash=None,
                )

    # ------------------------------------------------------------------ #
    # Control
    # ------------------------------------------------------------------ #

    async def abort(self, reason: str, meeting_key: str | None = None) -> DelegateStatus:
        """Recall a delegate: cut any speech, leave the call, send the briefing.

        Raises:
            DelegateIdleError: nothing matching is running.
        """
        running = self.running()
        if not running:
            raise DelegateIdleError("no delegate is attending a meeting")
        if meeting_key:
            meeting = self._meetings.get(meeting_key)
            if meeting is None or not meeting.active:
                raise DelegateIdleError(f"no delegate is attending {meeting_key!r}")
        elif len(running) == 1:
            meeting = running[0]
        else:
            names = ", ".join(m.meeting_key for m in running)
            raise DelegateIdleError(f"several meetings are running; name one of: {names}")

        meeting.stopping = True
        meeting.activity = DelegateActivity.LEAVING
        await meeting.dispatcher.stop(meeting.handle, reason)
        meeting.stop_reason = meeting.stop_reason or reason
        logger.warning("recall requested for %s: %s", meeting.meeting_key, reason)
        self._publish(meeting, force=True)
        return self.status(meeting.meeting_key)

    async def shutdown(self, timeout: float = SHUTDOWN_GRACE_SECONDS) -> None:
        """Stop every running meeting cleanly before the server exits.

        Container meetings are deliberately *not* stopped: they are separate
        processes that keep attending their calls and briefing their principals
        whether or not this control plane is up, and they are re-adopted on the
        next start. Killing them on a deploy would drop live meetings for no
        reason.
        """
        running = self.running()
        if not running:
            return
        in_process = [m for m in running if m.handle.runtime is MeetingRuntime.IN_PROCESS]
        for meeting in in_process:
            meeting.stopping = True
            await meeting.dispatcher.stop(meeting.handle, "server shutting down")
        for meeting in in_process:
            waiter = getattr(meeting.dispatcher, "wait", None)
            if waiter is not None:
                await waiter(meeting.handle, timeout)
        for meeting in running:
            if meeting.monitor is not None and not meeting.monitor.done():
                meeting.monitor.cancel()
        detached = [m.meeting_key for m in running if m.handle.runtime is MeetingRuntime.CONTAINER]
        if detached:
            logger.info("leaving %d containerised meeting(s) running: %s", len(detached), ", ".join(detached))

    def _reap(self) -> None:
        """Forget finished meetings so the dictionary does not grow without bound."""
        for key, meeting in list(self._meetings.items()):
            if not meeting.active and meeting is not self._last:
                self._meetings.pop(key, None)

    # ------------------------------------------------------------------ #
    # Monitoring
    # ------------------------------------------------------------------ #

    async def _monitor(self, meeting: RunningMeeting) -> None:
        """Watch one meeting until it ends, then finalise its row.

        For a container this is the only liveness signal there is: a worker
        that is OOM-killed or crashes before its callback fires would otherwise
        show as live forever.
        """
        try:
            while True:
                await asyncio.sleep(CONTAINER_POLL_SECONDS)
                running, exit_code = await meeting.dispatcher.poll(meeting.handle)
                if not running:
                    if meeting.active:
                        meeting.exit_code = meeting.exit_code if meeting.exit_code is not None else exit_code
                        await self._finish(meeting, reason=meeting.stop_reason or "the worker exited")
                    return
                if meeting.job.max_call_seconds:
                    elapsed = (datetime.now(timezone.utc) - meeting.started_at).total_seconds()
                    if elapsed > meeting.job.max_call_seconds + 120 and not meeting.stopping:
                        logger.warning(
                            "%s overran its limit by 2 minutes; stopping it", meeting.meeting_key
                        )
                        meeting.stopping = True
                        await meeting.dispatcher.stop(meeting.handle, "maximum call length exceeded")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a monitor crash must not take the server down
            logger.exception("monitor for %s failed", meeting.meeting_key)

    async def _finish(self, meeting: RunningMeeting, *, reason: str) -> None:
        """Record the end of a meeting exactly once."""
        if meeting.finished_at is not None:
            return
        meeting.finished_at = datetime.now(timezone.utc)
        meeting.activity = DelegateActivity.IDLE
        meeting.speaking_text = None
        meeting.stop_reason = meeting.stop_reason or reason
        self._last = meeting

        if meeting.meter is not None:
            minutes = (meeting.finished_at - meeting.started_at).total_seconds() / 60.0
            meeting.meter.record_meeting_minutes(minutes)
            meeting.meter.flush()

        failed = bool(meeting.error) or (meeting.exit_code not in (None, 0))
        if failed and meeting.handle.runtime is MeetingRuntime.CONTAINER and not meeting.error:
            # The container is about to be removed, taking its logs with it.
            # Without this, a failed join shows as "exit 1" and nothing else -
            # which is exactly the state that makes a dispatch problem take an
            # afternoon to diagnose instead of a minute.
            meeting.error = await self._container_tail(meeting)
        with session_scope() as session:
            row = repo.get_meeting(session, self.user_id, meeting.job.meeting_id)
            if row is not None:
                repo.update_meeting(
                    session, row,
                    status=MeetingStatus.FAILED.value if failed else MeetingStatus.FINISHED.value,
                    phase=meeting.phase,
                    activity=DelegateActivity.IDLE.value,
                    ended_at=meeting.finished_at,
                    exit_code=meeting.exit_code,
                    error=(meeting.error or "")[:2000] or None,
                    stop_reason=(meeting.stop_reason or "")[:2000] or None,
                    stats=meeting.counters.model_dump(),
                    consent_state=meeting.consent_state.value,
                    # The job token dies with the meeting: a container that
                    # outlives its call cannot post anything more.
                    job_token_hash=None,
                )
        if meeting.handle.runtime is MeetingRuntime.CONTAINER:
            remover = getattr(meeting.dispatcher, "remove", None)
            if remover is not None:
                try:
                    await remover(meeting.handle)
                except Exception:  # noqa: BLE001 - a leftover container is not worth an error
                    logger.debug("could not remove container %s", meeting.handle.reference[:12])
        else:
            forget = getattr(meeting.dispatcher, "forget", None)
            if forget is not None:
                forget(meeting.meeting_key)
        logger.info(
            "meeting %s finished (exit %s): %s", meeting.meeting_key, meeting.exit_code, meeting.stop_reason
        )
        self._publish(meeting, force=True)

    async def _container_tail(self, meeting: RunningMeeting) -> str | None:
        """The last few meaningful lines of a failed worker's output."""
        reader = getattr(meeting.dispatcher, "logs", None)
        if reader is None:
            return None
        try:
            text = await reader(meeting.handle, 60)
        except Exception:  # noqa: BLE001 - diagnostics must never fail a teardown
            return None
        markers = (
            "-> ERROR", "Traceback", "cannot join", "could not", "refused",
            "Configuration problem", "[WORKER]", "[STOP]", "ERROR",
        )
        interesting = [line for line in text.splitlines() if any(m in line for m in markers)]
        tail = interesting[-5:] or text.splitlines()[-5:]
        return "\n".join(tail)[:2000] or None

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """The ``EventCallback`` slot for in-process sessions."""
        meeting_key = str(payload.get("meeting_id") or "")
        self.ingest_event(meeting_key, event_type, payload)

    def ingest_event(self, meeting_key: str, event_type: str, payload: dict[str, Any]) -> None:
        """Fold one pipeline event into the meeting's state and republish it.

        The single entry point for both runtimes: an in-process session calls
        it through :meth:`emit`, and a worker container's POST is replayed
        through it by the callback route, so the two are indistinguishable
        downstream.
        """
        meeting = self._meetings.get(meeting_key)
        if meeting is not None:
            self._apply(meeting, event_type, payload)
        self._hub.publish(event_type, payload)
        if meeting is not None and event_type != INTERIM_EVENT:
            self._publish(
                meeting,
                force=event_type in ("utterance", "turn", "speech.end", "session.finished", "consent.signal"),
            )
        if meeting is not None and event_type == "session.finished":
            asyncio.create_task(
                self._finish(meeting, reason=str(payload.get("reason") or "the session ended")),
                name=f"deleg8-finish-{meeting_key}",
            )

    def _apply(self, meeting: RunningMeeting, event_type: str, payload: dict[str, Any]) -> None:
        """Derive activity, phase and counters from one event."""
        if event_type == "session.phase":
            phase = str(payload.get("phase", ""))
            meeting.phase = phase or meeting.phase
            meeting.detail = payload.get("reason") or meeting.detail
            if phase in _CONNECTING_PHASES:
                meeting.activity = DelegateActivity.CONNECTING
            elif phase == MeetStatus.IN_CALL.value:
                meeting.joined_at = meeting.joined_at or datetime.now(timezone.utc)
                if meeting.activity in (DelegateActivity.CONNECTING, DelegateActivity.IDLE):
                    meeting.activity = (
                        DelegateActivity.AWAITING_CONSENT
                        if meeting.consent_state is ConsentState.PENDING
                        else DelegateActivity.LISTENING
                    )
                self._record_phase(meeting, MeetingStatus.LIVE)
            elif phase in _TERMINAL_PHASES:
                meeting.activity = DelegateActivity.LEAVING
        elif event_type == "voice.ready":
            meeting.activity = (
                DelegateActivity.AWAITING_CONSENT
                if meeting.consent_state is ConsentState.PENDING
                else DelegateActivity.LISTENING
            )
        elif event_type == INTERIM_EVENT:
            if payload.get("text"):
                meeting.current_speaker = payload.get("speaker")
        elif event_type == "utterance":
            meeting.activity = DelegateActivity.THINKING
            meeting.current_speaker = payload.get("speaker")
            meeting.last_utterance = payload.get("text")
            meeting.counters.utterances += 1
        elif event_type == "escalation.requested":
            meeting.activity = DelegateActivity.AWAITING_APPROVAL
            meeting.counters.escalations += 1
        elif event_type == "turn":
            if not payload.get("response"):
                meeting.activity = DelegateActivity.LISTENING
            state = str(payload.get("state") or "")
            if state == "AUTO_REPLY":
                meeting.counters.auto_replies += 1
            elif state == "SILENT":
                meeting.counters.silent += 1
        elif event_type == "speech.start":
            meeting.activity = DelegateActivity.SPEAKING
            meeting.speaking_text = payload.get("text")
            meeting.current_speaker = "DELEG8"
        elif event_type in ("speech.end", "speech.interrupted"):
            meeting.activity = DelegateActivity.LISTENING
            meeting.speaking_text = None
            if event_type == "speech.interrupted":
                meeting.counters.interruptions += 1
        elif event_type in ("consent.signal", "consent.decided"):
            self._apply_consent(meeting, payload)
        elif event_type == "session.stopping":
            meeting.activity = DelegateActivity.LEAVING
            meeting.stop_reason = meeting.stop_reason or payload.get("reason")
        elif event_type == "voice.stopped":
            meeting.activity = DelegateActivity.LEAVING
        elif event_type == "session.finished":
            meeting.activity = DelegateActivity.IDLE
            meeting.exit_code = payload.get("exit_code", meeting.exit_code)
            meeting.error = payload.get("error") or meeting.error
            meeting.stop_reason = payload.get("reason") or meeting.stop_reason
            meeting.phase = str(payload.get("phase") or meeting.phase)
            stats = payload.get("stats")
            if isinstance(stats, dict):
                meeting.counters = SessionCounters(**{k: v for k, v in stats.items() if k in SessionCounters.model_fields})

    def _apply_consent(self, meeting: RunningMeeting, payload: dict[str, Any]) -> None:
        """Track a consent transition and persist it."""
        raw = str(payload.get("state") or "")
        try:
            state = ConsentState(raw)
        except ValueError:
            return
        if state is meeting.consent_state:
            return
        meeting.consent_state = state
        if state is ConsentState.PENDING:
            meeting.activity = DelegateActivity.AWAITING_CONSENT
        elif meeting.activity is DelegateActivity.AWAITING_CONSENT:
            meeting.activity = DelegateActivity.LISTENING
        with session_scope() as session:
            row = repo.get_meeting(session, self.user_id, meeting.job.meeting_id)
            if row is not None:
                repo.update_meeting(
                    session, row,
                    consent_state=state.value,
                    consent_decided_at=datetime.now(timezone.utc),
                )
        logger.info("meeting %s consent is now %s", meeting.meeting_key, state.value)

    def _record_phase(self, meeting: RunningMeeting, status: MeetingStatus) -> None:
        with session_scope() as session:
            row = repo.get_meeting(session, self.user_id, meeting.job.meeting_id)
            if row is not None and row.status != status.value:
                repo.update_meeting(
                    session, row,
                    status=status.value,
                    phase=meeting.phase,
                    joined_at=meeting.joined_at or row.joined_at,
                )

    def record_consent_decision(self, meeting_key: str, decision: ConsentDecision) -> None:
        """Write one consent decision to the audit trail."""
        meeting = self._meetings.get(meeting_key)
        if meeting is None:
            return
        with session_scope() as session:
            repo.record_consent(
                session,
                user_id=self.user_id,
                meeting_id=meeting.job.meeting_id,
                mode=decision.mode.value,
                outcome=decision.outcome.value,
                method=decision.method,
                granted_by=decision.granted_by,
                evidence=decision.evidence,
                disclosure_text=decision.disclosure_text,
                participants=[meeting.principal_name, meeting.job.speaker_label],
            )
        self.ingest_event(
            meeting_key,
            "consent.decided",
            {
                "meeting_id": meeting_key,
                "state": decision.outcome.value,
                "mode": decision.mode.value,
                "method": decision.method,
                "granted_by": decision.granted_by,
            },
        )

    def _publish(self, meeting: RunningMeeting, *, force: bool = False) -> None:
        status = meeting.status(
            pending_escalations=self._pending_escalations(), subscribers=self._hub.subscribers
        )
        key = (status.phase, status.activity.value)
        if force or self._last_status_key.get(meeting.meeting_key) != key:
            self._last_status_key[meeting.meeting_key] = key
            self._hub.publish("status", status.model_dump(mode="json"))


__all__ = [
    "AbortRequest",
    "DelegateActivity",
    "DelegateBusyError",
    "DelegateIdleError",
    "DelegateManager",
    "DelegateStatus",
    "DelegateUnavailableError",
    "JoinRequest",
    "QuotaExceeded",
    "RunningMeeting",
    "SessionCounters",
    "idle_status",
]
