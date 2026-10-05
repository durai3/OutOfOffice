"""
Process-wide record of what is currently degraded.

The failure this module exists to prevent: every LLM-backed component used to
catch its own errors and quietly fall back to a deterministic path, so a dead
model id produced a product that looked healthy, answered every question, and
was wrong. Nothing surfaced it - not the console, not the logs anyone reads,
not the live feed. It was discoverable only by opening an archived briefing
and noticing a ``fallback_reason`` field.

So every fallback now also calls :func:`report_degraded`, and the console
shows a banner for as long as anything is degraded. A component that recovers
calls :func:`report_healthy`. The registry is deliberately global and
in-memory: it describes *this process right now*, and it must be reachable
from deep inside a retrieval call without threading a handle through six
constructors.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from pydantic import BaseModel, Field

logger = logging.getLogger("deleg8.health")


class Degradation(BaseModel):
    """One component running in a fallback mode."""

    component: str = Field(..., description="Stable id, e.g. 'answers' or 'briefing'.")
    summary: str = Field(..., description="One line a non-engineer can act on.")
    detail: str | None = Field(default=None, description="Underlying error text, for the logs.")
    since: datetime = Field(..., description="When this degradation was first reported (UTC).")
    count: int = Field(default=1, ge=1, description="How many times it has recurred since.")


class HealthRegistry:
    """Thread-safe map of component id to its current :class:`Degradation`."""

    def __init__(self) -> None:
        """Start healthy."""
        self._lock = threading.Lock()
        self._degraded: dict[str, Degradation] = {}

    def report_degraded(self, component: str, summary: str, detail: str | None = None) -> None:
        """Mark ``component`` degraded, or bump the count if it already was."""
        with self._lock:
            existing = self._degraded.get(component)
            if existing is not None and existing.summary == summary:
                self._degraded[component] = existing.model_copy(update={"count": existing.count + 1, "detail": detail})
                return
            self._degraded[component] = Degradation(
                component=component, summary=summary, detail=detail,
                since=datetime.now(timezone.utc), count=1,
            )
        logger.warning("DEGRADED %s: %s%s", component, summary, f" ({detail})" if detail else "")

    def report_healthy(self, component: str) -> None:
        """Clear any degradation recorded for ``component``."""
        with self._lock:
            if self._degraded.pop(component, None) is not None:
                logger.info("recovered: %s", component)

    def current(self) -> list[Degradation]:
        """Everything currently degraded, worst (oldest) first."""
        with self._lock:
            return sorted(self._degraded.values(), key=lambda d: d.since)

    @property
    def healthy(self) -> bool:
        """True when nothing is degraded."""
        with self._lock:
            return not self._degraded

    def banner(self) -> str | None:
        """One line summarising every degradation, or ``None`` when healthy."""
        items = self.current()
        if not items:
            return None
        return "; ".join(f"{d.component}: {d.summary}" for d in items)


health = HealthRegistry()
"""The process-wide registry. Import this, do not construct another."""


# Stable component ids, so the console can style them and so a typo in one
# call site cannot create a phantom second entry for the same subsystem.
COMPONENT_LLM: str = "llm"
COMPONENT_ANSWERS: str = "answers"
COMPONENT_PLANNER: str = "planner"
COMPONENT_BRIEFING: str = "briefing"
COMPONENT_DOSSIER: str = "dossier"
COMPONENT_COMMITMENTS: str = "commitments"
COMPONENT_KNOWLEDGE: str = "knowledge"


def report_degraded(component: str, summary: str, detail: str | None = None) -> None:
    """Shorthand for ``health.report_degraded``."""
    health.report_degraded(component, summary, detail)


def report_healthy(component: str) -> None:
    """Shorthand for ``health.report_healthy``."""
    health.report_healthy(component)
