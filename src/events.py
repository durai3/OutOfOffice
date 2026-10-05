"""
Process-local event plumbing shared by the voice loop, the Meet bot, the
session runner, and the HTTP API's live feed.

Components that produce events depend only on :data:`EventCallback`, a plain
callable, so the audio and gateway packages stay free of any web-framework
import. The API layer supplies a callback that fans events out to WebSocket
subscribers; the command line supplies nothing and the events are simply
not emitted.

Payload values may be primitives, dicts, lists, dataclasses, or Pydantic
models; the consumer is responsible for serialising them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

EventCallback = Callable[[str, dict[str, Any]], None]
"""``callback(event_type, payload)``: synchronous, called on the event-loop thread."""

logger = logging.getLogger("deleg8.events")


def safe_emit(callback: EventCallback | None, event_type: str, payload: dict[str, Any]) -> None:
    """Invoke ``callback`` if present; a failing observer must never break the producer."""
    if callback is None:
        return
    try:
        callback(event_type, payload)
    except Exception:
        logger.exception("event observer failed for %s", event_type)
