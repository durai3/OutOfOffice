"""
The in-process dispatcher: a meeting as an asyncio task in the API server.

This is what DELEG8 did before there was a dispatcher at all, kept because it
is genuinely the right thing on a laptop - no image to build, no daemon to
run, and the console sees the live transcript through the same objects the
REST endpoints hold.

It is not the right thing for more than a couple of concurrent meetings on one
machine: every session drives a Chromium instance and a pair of audio streams
through one process's default audio devices, and one crash takes down every
meeting and the API with it. :class:`~src.dispatch.container.ContainerDispatcher`
is the answer to that; this one keeps ``DELEG8_DISPATCH_MODE=inprocess``
honest for development.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from src.db.models import MeetingRuntime
from src.dispatch.base import DispatchError, DispatchHandle, MeetingJob
from src.gateway.components import SessionComponents, SessionOptions

logger = logging.getLogger("deleg8.dispatch.inprocess")

EventSink = Callable[[str, dict[str, Any]], None]


class InProcessDispatcher:
    """Runs meeting sessions as tasks inside this process, on a tenant's own components."""

    name = "inprocess"

    def __init__(
        self,
        components: SessionComponents,
        *,
        emit: EventSink | None = None,
        consent_mode_provider: Callable[[MeetingJob], Any] | None = None,
    ) -> None:
        """Bind to one tenant's components; every session launched here uses them."""
        self._components = components
        self._emit = emit
        self._consent_mode_provider = consent_mode_provider
        self._sessions: dict[str, Any] = {}
        self._tasks: dict[str, asyncio.Task[int]] = {}

    def describe(self) -> str:
        """One line for the health page."""
        return f"in-process dispatcher: {len(self._tasks)} session(s) in this server"

    async def available(self) -> tuple[bool, str]:
        """Whether the voice and browser stacks can be imported here."""
        try:
            import src.gateway.session  # noqa: F401
        except ImportError as exc:
            return False, f"the voice / browser stack is not importable here: {exc}"
        return True, "in-process sessions available"

    def session_for(self, meeting_key: str) -> Any | None:
        """The live :class:`~src.gateway.session.MeetingSession`, if this process owns it."""
        return self._sessions.get(meeting_key)

    async def launch(self, job: MeetingJob) -> DispatchHandle:
        """Start a session task for ``job``."""
        try:
            from src.gateway.session import MeetingSession, SessionConfig
        except ImportError as exc:  # pragma: no cover - depends on host libraries
            raise DispatchError(f"the voice / browser stack is not importable here: {exc}") from exc

        options = SessionOptions(
            join_url=job.join_url,
            meeting_id=job.meeting_key,
            speaker_label=job.speaker_label,
            half_duplex=job.half_duplex,
            briefing=job.briefing,
            max_call_seconds=job.max_call_seconds,
        )
        config = SessionConfig.from_options(
            options, store=self._components.store, user_id=job.principal_slug
        )
        session = MeetingSession(
            config,
            self._components,
            emit=self._emit,
            consent_mode=job.consent_mode,
        )
        self._sessions[job.meeting_key] = session
        task = asyncio.create_task(
            self._run(job.meeting_key, session), name=f"deleg8-meeting-{job.code}"
        )
        self._tasks[job.meeting_key] = task
        logger.info("meeting %s running in this process", job.meeting_key)
        return DispatchHandle(
            runtime=MeetingRuntime.IN_PROCESS,
            reference=task.get_name(),
            detail="running in the API server",
            extra={"meeting_key": job.meeting_key},
        )

    async def _run(self, meeting_key: str, session: Any) -> int:
        try:
            return await session.run()
        except asyncio.CancelledError:
            session.request_stop("server task cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - one meeting's crash must not take the server down
            logger.exception("meeting %s crashed", meeting_key)
            session.error = session.error or f"session crashed: {exc}"
            session.exit_code = session.exit_code if session.exit_code is not None else 1
            if session.finished_at is None:
                session.finished_at = datetime.now(timezone.utc)
                if self._emit is not None:
                    self._emit(
                        "session.finished",
                        {
                            "meeting_id": meeting_key,
                            "exit_code": session.exit_code,
                            "error": session.error,
                            "reason": session.stop_reason,
                            "phase": session.status.value,
                            "stats": session.counters(),
                        },
                    )
            return session.exit_code or 1

    async def stop(self, handle: DispatchHandle, reason: str) -> None:
        """Ask the session to cut any speech, leave the call and brief."""
        meeting_key = str(handle.extra.get("meeting_key", ""))
        session = self._sessions.get(meeting_key)
        if session is None:
            return
        await session.interrupt_speech(reason)
        session.request_stop(reason)

    async def poll(self, handle: DispatchHandle) -> tuple[bool, int | None]:
        """Whether the session task is still running, and its exit code once it is not."""
        meeting_key = str(handle.extra.get("meeting_key", ""))
        task = self._tasks.get(meeting_key)
        if task is None:
            return False, None
        if not task.done():
            return True, None
        session = self._sessions.get(meeting_key)
        return False, getattr(session, "exit_code", None)

    async def wait(self, handle: DispatchHandle, timeout: float) -> None:
        """Wait for a stopping session to finish, then forget it."""
        meeting_key = str(handle.extra.get("meeting_key", ""))
        task = self._tasks.get(meeting_key)
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error("meeting %s did not stop within %.0fs; cancelling", meeting_key, timeout)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down regardless
                pass
        finally:
            self.forget(meeting_key)

    def forget(self, meeting_key: str) -> None:
        """Drop a finished session's task and object."""
        self._tasks.pop(meeting_key, None)
        self._sessions.pop(meeting_key, None)
