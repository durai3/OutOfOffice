"""
How a worker container tells the control plane what is happening.

A container running in its own namespace with its own Chromium is exactly
what makes concurrency work, and exactly what breaks the console: the live
feed used to work because the delegate ran inside the API server, publishing
to a hub the WebSocket endpoint held a reference to. Once the meeting is a
separate process that reference is gone, and without a way back the console
would show a meeting as "dispatching" until it silently finished.

So the worker posts its events home. :class:`CallbackEmitter` drops into the
same ``emit(event_type, payload)`` slot the in-process session uses, batches
what it is given, and POSTs to
``/api/v1/worker/events`` with the one-meeting job token it was launched
with. The control plane republishes each event on that tenant's live feed and
folds the interesting ones into the ``meetings`` row, so a container meeting
and an in-process meeting look identical to the console.

Design points worth stating:

* **Batched and fire-and-forget.** Events are queued and flushed by a
  background task. A slow or dead control plane must never add latency to a
  turn in a live meeting, and must never crash the worker - the meeting is
  more important than the telemetry.
* **Bounded.** The queue has a cap and drops oldest-first. A worker that
  cannot reach home for ten minutes must not consume memory until it dies.
* **Terminal events are flushed synchronously.** ``session.finished`` is the
  one event that must arrive, because it is what stops the console showing a
  meeting that ended half an hour ago as live.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("deleg8.dispatch.callback")

MAX_QUEUE: int = 500
FLUSH_INTERVAL_SECONDS: float = 1.0
BATCH_SIZE: int = 40
TERMINAL_EVENTS: frozenset[str] = frozenset(
    {"session.finished", "meeting.concluded", "meeting.not_retained"}
)
_SKIP: frozenset[str] = frozenset({"transcript.interim"})
"""Interim transcripts fire several times a second and are superseded by the
final utterance; sending them over HTTP would be most of the traffic for none
of the value."""


def callback_url() -> str | None:
    """``DELEG8_CALLBACK_URL`` - where a worker reports. Absent outside a container."""
    return os.getenv("DELEG8_CALLBACK_URL", "").strip() or None


def job_token() -> str | None:
    """``DELEG8_JOB_TOKEN`` - the bearer token authorising exactly one meeting."""
    return os.getenv("DELEG8_JOB_TOKEN", "").strip() or None


def is_worker() -> bool:
    """True when this process is a dispatched worker with somewhere to report."""
    return bool(callback_url() and job_token())


class CallbackEmitter:
    """Batches session events and posts them to the control plane."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        meeting_id: str | None = None,
        user_id: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        """Bind to a callback endpoint and a job token."""
        self._url = url.rstrip("/")
        self._token = token
        self._meeting_id = meeting_id or os.getenv("DELEG8_JOB_MEETING_ID", "")
        self._user_id = user_id or os.getenv("DELEG8_JOB_USER_ID", "")
        self._timeout = timeout
        self._queue: deque[dict[str, Any]] = deque(maxlen=MAX_QUEUE)
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._dropped = 0
        self._sent = 0
        self._failures = 0

    def describe(self) -> str:
        """One line for the worker's startup output."""
        return f"reporting to {self._url} ({self._sent} sent, {self._dropped} dropped)"

    def start(self) -> None:
        """Begin the background flush loop."""
        if self._task is None:
            self._task = asyncio.create_task(self._pump(), name="deleg8-callback")

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """The ``EventCallback`` slot: queue one event. Never blocks, never raises."""
        if event_type in _SKIP:
            return
        if len(self._queue) == MAX_QUEUE:
            self._dropped += 1
        self._queue.append(
            {
                "type": event_type,
                "at": datetime.now(timezone.utc).isoformat(),
                "payload": _jsonable(payload),
            }
        )
        if event_type in TERMINAL_EVENTS:
            # Schedule an immediate flush rather than awaiting one: emit() is
            # called from synchronous code deep in the pipeline.
            try:
                asyncio.get_running_loop().create_task(self.flush())
            except RuntimeError:  # pragma: no cover - no loop, e.g. during teardown
                pass

    async def _pump(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=FLUSH_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
            await self.flush()

    async def flush(self) -> None:
        """Send whatever is queued. Failures are logged and the batch is dropped."""
        if not self._queue:
            return
        batch = [self._queue.popleft() for _ in range(min(BATCH_SIZE, len(self._queue)))]
        try:
            import httpx

            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._url}/api/v1/worker/events",
                    headers={"Authorization": f"Bearer {self._token}"},
                    json={"meeting_id": self._meeting_id, "events": batch},
                )
            if response.status_code >= 400:
                self._failures += 1
                logger.warning(
                    "control plane rejected %d event(s): HTTP %s %s",
                    len(batch), response.status_code, response.text[:200],
                )
            else:
                self._sent += len(batch)
        except Exception as exc:  # noqa: BLE001 - telemetry must never break the meeting
            self._failures += 1
            logger.warning("could not deliver %d event(s): %s", len(batch), exc)

    async def stop(self) -> None:
        """Flush what is left and stop the loop."""
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down regardless
                pass
            self._task = None
        for _ in range(5):
            if not self._queue:
                break
            await self.flush()
        logger.info("callback emitter stopped: %s", self.describe())


def _jsonable(value: Any) -> Any:
    """Coerce a payload to something ``json`` can encode.

    Session payloads carry pydantic models (decisions, reports, snapshots) and
    enums, and this runs on the meeting's own thread of control, so it favours
    never raising over being exhaustive: anything unrecognised becomes its
    ``repr``.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except Exception:  # noqa: BLE001 - fall through to repr
            pass
    enum_value = getattr(value, "value", None)
    if enum_value is not None and isinstance(enum_value, (str, int, float, bool)):
        return enum_value
    return repr(value)


def build_emitter() -> CallbackEmitter | None:
    """The emitter for this process, or ``None`` when it is not a dispatched worker."""
    url, token = callback_url(), job_token()
    if not url or not token:
        return None
    emitter = CallbackEmitter(url, token)
    logger.info("worker callbacks enabled: %s", url)
    return emitter
