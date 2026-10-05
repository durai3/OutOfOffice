"""
What a dispatcher is asked to do, and what it reports back.

The container image was already built and verified: ``docker/Dockerfile``
plus ``docker/entrypoint.sh`` produce a worker that joins exactly one meeting
and exits. What was missing was anything that *ran* it. Nothing in the product
ever called ``docker run``; the only way to attend a meeting was an asyncio
task inside the API server, which caps the whole platform at one machine's
microphone-free Chromium processes and puts every tenant's meeting in one
process.

:class:`MeetingJob` is the contract between the two halves: everything one
worker needs to attend one meeting for one tenant, including the credentials
it should use and the token it calls home with. :class:`Dispatcher` is the
seam: :class:`~src.dispatch.inprocess.InProcessDispatcher` keeps the old
behaviour for a laptop, :class:`~src.dispatch.container.ContainerDispatcher`
launches the image. Both produce a :class:`DispatchHandle` the delegate
manager tracks identically.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from src.db.models import ConsentMode, MeetingRuntime


class DispatchMode(str, Enum):
    """How this deployment runs meetings."""

    IN_PROCESS = "inprocess"
    CONTAINER = "container"
    AUTO = "auto"
    """Container when Docker is usable, in-process otherwise. The default."""


def configured_mode() -> DispatchMode:
    """``DELEG8_DISPATCH_MODE`` (default ``auto``)."""
    raw = os.getenv("DELEG8_DISPATCH_MODE", "").strip().lower() or DispatchMode.AUTO.value
    try:
        return DispatchMode(raw)
    except ValueError:
        return DispatchMode.AUTO


@dataclass(frozen=True)
class MeetingJob:
    """Everything one worker needs to attend one meeting for one tenant.

    Deliberately self-contained and serialisable: a container gets this as
    environment variables and nothing else, which is what makes the boundary
    between control plane and worker real rather than notional.
    """

    meeting_id: uuid.UUID
    """The ``meetings`` row this job belongs to."""
    user_id: uuid.UUID
    meeting_key: str
    """What memory, the briefing and the console call ``meeting_id``."""
    principal_slug: str
    principal_name: str
    join_url: str
    platform: str
    code: str
    speaker_label: str = "Meeting"
    half_duplex: bool = False
    briefing: bool = True
    max_call_seconds: float | None = None
    consent_mode: ConsentMode = ConsentMode.NOTIFY_ONLY
    collection: str = ""
    """The tenant's Chroma collection name.

    Passed explicitly because the worker's ``/data`` mount gives it the right
    *directory* but not the right collection: without this it opens the default
    ``deleg8_documents``, finds it empty, and joins the meeting knowing nothing
    the tenant has uploaded - a failure that looks exactly like a retrieval miss.
    """
    credentials: dict[str, str] = field(default_factory=dict)
    """Provider API keys, already resolved to this tenant's own or the platform's."""
    job_token: str | None = None
    """Bearer token the worker uses to report events back to the control plane."""
    callback_url: str | None = None
    """Where the worker posts events; ``None`` disables reporting."""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def environment(self) -> dict[str, str]:
        """The job as environment variables for a worker container."""
        env: dict[str, str] = {
            "DELEG8_JOB_MEETING_ID": str(self.meeting_id),
            "DELEG8_JOB_USER_ID": str(self.user_id),
            "DELEG8_VOICE_MEETING_ID": self.meeting_key,
            "DELEG8_VOICE_USER_ID": self.principal_slug,
            "DELEG8_VOICE_SPEAKER": self.speaker_label,
            "DELEG8_CONSENT_MODE": self.consent_mode.value,
            "DELEG8_RAG_COLLECTION": self.collection,
            "DELEG8_HALF_DUPLEX": "true" if self.half_duplex else "false",
            "MEET_JOIN_URL": self.join_url,
        }
        if self.platform == "zoom":
            env["ZOOM_JOIN_URL"] = self.join_url
            env.pop("MEET_JOIN_URL", None)
        elif self.platform == "teams":
            env["TEAMS_JOIN_URL"] = self.join_url
            env.pop("MEET_JOIN_URL", None)
        if self.job_token:
            env["DELEG8_JOB_TOKEN"] = self.job_token
        if self.callback_url:
            env["DELEG8_CALLBACK_URL"] = self.callback_url
        env.update(self.credentials)
        return env

    def command(self) -> list[str]:
        """The worker's command line."""
        argv = [
            "python", "-m", "src.main", "join",
            "--url", self.join_url,
            "--meeting-id", self.meeting_key,
            "--speaker", self.speaker_label,
        ]
        if self.half_duplex:
            argv.append("--half-duplex")
        if not self.briefing:
            argv.append("--no-briefing")
        if self.max_call_seconds:
            argv += ["--max-minutes", f"{self.max_call_seconds / 60:.2f}"]
        return argv

    def container_name(self) -> str:
        """A stable, collision-free container name for this job."""
        return f"deleg8-{str(self.user_id)[:8]}-{str(self.meeting_id)[:8]}"


@dataclass
class DispatchHandle:
    """What a dispatcher hands back so the manager can watch and stop the work."""

    runtime: MeetingRuntime
    reference: str
    """Container id, or the asyncio task name."""
    detail: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    extra: dict[str, Any] = field(default_factory=dict)


class DispatchError(RuntimeError):
    """A meeting could not be launched."""


class DispatchUnavailable(DispatchError):
    """The runtime this dispatcher needs is not usable on this machine."""


@runtime_checkable
class Dispatcher(Protocol):
    """Launches and stops one meeting worker."""

    name: str

    def describe(self) -> str:
        """One line for the health page."""
        ...

    async def available(self) -> tuple[bool, str]:
        """Whether this dispatcher can run right now, and why not if it cannot."""
        ...

    async def launch(self, job: MeetingJob) -> DispatchHandle:
        """Start the worker. Raises :class:`DispatchError` on failure."""
        ...

    async def stop(self, handle: DispatchHandle, reason: str) -> None:
        """Ask the worker to leave the call and wind down."""
        ...

    async def poll(self, handle: DispatchHandle) -> tuple[bool, int | None]:
        """Whether the worker is still running, and its exit code once it is not."""
        ...
