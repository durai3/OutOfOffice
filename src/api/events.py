"""
The live feed: one hub, many WebSocket subscribers.

Producers (the voice loop, the Meet bot, the session runner, the delegate
manager) call :meth:`LiveFeedHub.publish` on the event-loop thread. Each
subscriber gets its own bounded queue; a subscriber that stops draining is
dropped rather than allowed to grow the process without limit. A short
backlog is kept so a page that (re)connects mid-meeting sees the recent
transcript at once instead of a blank screen.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

logger = logging.getLogger("deleg8.api.live")

INTERIM_EVENT: str = "transcript.interim"
"""High-frequency caption updates; delivered live but never kept in the backlog."""


class LiveEvent(BaseModel):
    """One message on the live feed."""

    seq: int = Field(..., description="Monotonic sequence number within this server process.")
    type: str = Field(..., description="Event type, e.g. 'utterance', 'decision', 'status'.")
    at: datetime = Field(..., description="When the server published it (UTC).")
    payload: dict[str, Any] = Field(default_factory=dict)


class LiveFeedHub:
    """Fan-out of :class:`LiveEvent` to WebSocket subscribers, with a bounded backlog."""

    def __init__(self, *, backlog: int = 400, queue_size: int = 2000) -> None:
        """``backlog`` events are replayed to new subscribers; ``queue_size`` bounds each subscriber."""
        self._seq = 0
        self._backlog: deque[LiveEvent] = deque(maxlen=backlog)
        self._subscribers: set[asyncio.Queue[LiveEvent]] = set()
        self._queue_size = queue_size

    @property
    def subscribers(self) -> int:
        """Connected clients."""
        return len(self._subscribers)

    def publish(self, event_type: str, payload: Mapping[str, Any] | None = None) -> LiveEvent:
        """Encode ``payload`` to JSON-safe values and deliver to every subscriber."""
        self._seq += 1
        event = LiveEvent(
            seq=self._seq,
            type=event_type,
            at=datetime.now(timezone.utc),
            payload=jsonable_encoder(dict(payload or {})),
        )
        if event_type != INTERIM_EVENT:
            self._backlog.append(event)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("live feed subscriber is not draining; dropping it")
                self._subscribers.discard(queue)
        return event

    def subscribe(self) -> tuple[asyncio.Queue[LiveEvent], list[LiveEvent]]:
        """Register a subscriber; returns its queue and the current backlog."""
        queue: asyncio.Queue[LiveEvent] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue, list(self._backlog)

    def unsubscribe(self, queue: asyncio.Queue[LiveEvent]) -> None:
        """Forget a subscriber."""
        self._subscribers.discard(queue)

    def backlog(self) -> list[LiveEvent]:
        """Recent events, oldest first."""
        return list(self._backlog)

    def clear_backlog(self) -> None:
        """Drop the replay buffer (a new meeting should not replay the previous one)."""
        self._backlog.clear()
