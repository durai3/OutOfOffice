"""
Metering and quotas: who spent what, and when to stop them.

Without this, the first stranger to sign up spends the operator's Gemini,
Deepgram and Cartesia allowance until it is gone, and every other user -
including the operator - silently loses their delegate mid-meeting. That is
not a billing problem, it is an availability problem, and it is why metering
had to exist before a second user did.

Three pieces:

* :class:`UsageMeter` - one per tenant. Receives a
  :class:`~src.llm.UsageReport` from the language model client, audio seconds
  from the transcriber, and characters from the synthesiser, and writes a row
  per event. Buffered and flushed on a background thread, because a turn in a
  live meeting must not wait on an INSERT.
* :class:`Quota` / :data:`PLANS` - what an account is allowed per month. A
  user who brings their own key is not counted against it, which is the whole
  incentive for bringing one.
* :func:`check_quota` - called before a dispatch and periodically during a
  meeting. The pre-dispatch check refuses cleanly; the mid-meeting check only
  warns and lets the meeting finish, because cutting a delegate out of a live
  call to save a few thousand tokens is a worse failure than the overage.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from src.db import repo
from src.db.base import session_scope
from src.db.models import UsageService
from src.llm import UsageReport

logger = logging.getLogger("deleg8.usage")


# --------------------------------------------------------------------------- #
# Pricing (for display only)
# --------------------------------------------------------------------------- #

_LLM_PRICES: dict[str, tuple[float, float]] = {
    # provider prefix -> (USD per million input tokens, USD per million output tokens)
    "gemini-3.6-flash": (0.30, 2.50),
    "gemini-3.5-flash-lite": (0.10, 0.40),
    "gemini": (0.30, 2.50),
    "claude-haiku": (1.00, 5.00),
    "claude-sonnet": (3.00, 15.00),
    "claude-opus": (15.00, 75.00),
    "claude": (3.00, 15.00),
}
_STT_USD_PER_MINUTE: float = 0.0077
"""Deepgram nova-3 streaming, list price at the time of writing."""
_TTS_USD_PER_1K_CHARS: float = 0.025
"""Cartesia sonic, list price at the time of writing."""


def estimate_llm_cost(model: str, input_tokens: float, output_tokens: float) -> float:
    """Rough USD cost of one model call.

    Deliberately an estimate shown to the user, never an invoice: list prices
    move, and the point is to make "this meeting cost about forty cents" legible
    rather than to bill anyone.
    """
    for prefix, (in_price, out_price) in _LLM_PRICES.items():
        if model.startswith(prefix):
            return (input_tokens / 1e6) * in_price + (output_tokens / 1e6) * out_price
    return 0.0


# --------------------------------------------------------------------------- #
# Quotas
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Quota:
    """What one plan allows per calendar month, on the platform's own keys."""

    name: str
    llm_tokens: int
    stt_seconds: float
    tts_characters: int
    meetings: int
    max_concurrent_meetings: int
    max_meeting_minutes: float

    def describe(self) -> str:
        """One line for the console."""
        return (
            f"{self.name}: {self.meetings} meetings/month, {self.llm_tokens:,} tokens, "
            f"{self.stt_seconds / 60:.0f} minutes of speech, {self.max_concurrent_meetings} at once"
        )


PLANS: dict[str, Quota] = {
    "beta": Quota(
        name="Beta",
        llm_tokens=2_000_000,
        stt_seconds=4 * 3600.0,
        tts_characters=200_000,
        meetings=25,
        max_concurrent_meetings=2,
        max_meeting_minutes=120.0,
    ),
    "demo": Quota(
        name="Judge demo",
        llm_tokens=300_000,
        stt_seconds=0.0,
        tts_characters=0,
        meetings=12,
        max_concurrent_meetings=1,
        max_meeting_minutes=30.0,
    ),
    "unlimited": Quota(
        name="Own keys",
        llm_tokens=0,
        stt_seconds=0.0,
        tts_characters=0,
        meetings=0,
        max_concurrent_meetings=5,
        max_meeting_minutes=480.0,
    ),
}
"""``unlimited`` is what an account using entirely its own keys gets: no
consumption limits, because nothing it consumes is the platform's, but still a
concurrency cap so one account cannot exhaust the machine."""

DEFAULT_PLAN: str = "beta"


def plan_for(user_settings: dict | None) -> Quota:
    """The quota for a user, from their settings blob."""
    key = str((user_settings or {}).get("plan") or DEFAULT_PLAN)
    return PLANS.get(key, PLANS[DEFAULT_PLAN])


def plan_for_context(user_settings: dict | None, org_settings: dict | None) -> Quota:
    """The quota for a tenant: an organization member's plan comes from the
    organization's settings, shared across every seat; a solo account's plan
    comes from its own settings exactly as :func:`plan_for` already resolved it.
    """
    return plan_for(org_settings if org_settings is not None else user_settings)


def month_start(now: datetime | None = None) -> datetime:
    """Midnight UTC on the first of the current month."""
    moment = now or datetime.now(timezone.utc)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@dataclass
class QuotaStatus:
    """Where one tenant stands against their plan this month."""

    plan: Quota
    llm_tokens: float = 0.0
    stt_seconds: float = 0.0
    tts_characters: float = 0.0
    meetings: int = 0
    cost_usd: float = 0.0
    own_keys: bool = False
    period_start: datetime = field(default_factory=month_start)

    def _ratio(self, used: float, allowed: float) -> float:
        if self.own_keys or allowed <= 0:
            return 0.0
        return min(1.0, used / allowed)

    @property
    def fractions(self) -> dict[str, float]:
        """Fraction of each allowance consumed, 0.0-1.0."""
        return {
            "llm_tokens": self._ratio(self.llm_tokens, self.plan.llm_tokens),
            "stt_seconds": self._ratio(self.stt_seconds, self.plan.stt_seconds),
            "tts_characters": self._ratio(self.tts_characters, self.plan.tts_characters),
            "meetings": self._ratio(self.meetings, self.plan.meetings),
        }

    @property
    def exceeded(self) -> list[str]:
        """Which allowances are spent."""
        return [name for name, value in self.fractions.items() if value >= 1.0]

    @property
    def near_limit(self) -> list[str]:
        """Which allowances are above 80% - worth telling the user before a meeting."""
        return [name for name, value in self.fractions.items() if 0.8 <= value < 1.0]

    @property
    def blocked(self) -> bool:
        """True when a new meeting must be refused."""
        return bool(self.exceeded)

    def reason(self) -> str:
        """Why a dispatch was refused, in the user's language."""
        labels = {
            "llm_tokens": "language model tokens",
            "stt_seconds": "speech-to-text minutes",
            "tts_characters": "text-to-speech characters",
            "meetings": "meetings",
        }
        spent = ", ".join(labels.get(name, name) for name in self.exceeded)
        return (
            f"Your {self.plan.name} allowance for this month is used up ({spent}). "
            "Add your own API keys in Settings to keep going - calls on your own keys "
            "are not counted against this - or wait for the allowance to reset."
        )


def quota_status(
    session: Session, user_id: uuid.UUID, *, own_keys: bool = False, org_settings: dict | None = None
) -> QuotaStatus:
    """Read one tenant's consumption for the current month.

    ``org_settings`` is the caller's organization's settings blob, when they
    belong to one - pass ``context.org.settings`` from a request's
    :class:`~src.auth.deps.TenantContext`. ``None`` for a solo account keeps
    today's per-user plan resolution unchanged.
    """
    user = repo.get_user(session, user_id)
    plan = plan_for_context(user.settings if user is not None else None, org_settings)
    if own_keys:
        plan = PLANS["unlimited"]
    start = month_start()
    totals = repo.usage_totals(session, user_id, since=start, platform_only=True)
    all_totals = repo.usage_totals(session, user_id, since=start, platform_only=False)
    return QuotaStatus(
        plan=plan,
        llm_tokens=totals.get(UsageService.LLM.value, {}).get("units", 0.0),
        stt_seconds=totals.get(UsageService.STT.value, {}).get("units", 0.0),
        tts_characters=totals.get(UsageService.TTS.value, {}).get("units", 0.0),
        meetings=repo.count_meetings_since(session, user_id, start),
        cost_usd=sum(entry.get("cost_usd", 0.0) for entry in all_totals.values()),
        own_keys=own_keys,
        period_start=start,
    )


class QuotaExceeded(RuntimeError):
    """A dispatch was refused because the tenant's allowance is spent."""

    def __init__(self, status: QuotaStatus) -> None:
        """Carry the status so the API can show exactly which limit was hit."""
        super().__init__(status.reason())
        self.status = status


def check_quota(
    session: Session, user_id: uuid.UUID, *, own_keys: bool = False, org_settings: dict | None = None
) -> QuotaStatus:
    """Raise :class:`QuotaExceeded` when this tenant may not start another meeting."""
    status = quota_status(session, user_id, own_keys=own_keys, org_settings=org_settings)
    if status.blocked:
        raise QuotaExceeded(status)
    return status


# --------------------------------------------------------------------------- #
# The meter
# --------------------------------------------------------------------------- #

@dataclass
class _Pending:
    """One buffered usage event."""

    service: str
    provider: str
    model: str
    unit: str
    input_units: float
    output_units: float
    units: float
    cost_usd: float
    own_key: bool
    meeting_id: uuid.UUID | None
    detail: str | None


class UsageMeter:
    """Records what one tenant consumes, buffered so a meeting never waits on the database.

    Every method here is called from the live path - the language model client
    on each turn, the transcriber on each utterance, the synthesiser on each
    spoken reply - so none of them touch the database inline. Events accumulate
    and are flushed when the buffer fills or when the meeting ends, and a
    failure to flush is logged rather than raised.
    """

    FLUSH_THRESHOLD: int = 12

    def __init__(
        self,
        user_id: uuid.UUID,
        *,
        meeting_id: uuid.UUID | None = None,
        own_llm_key: bool = False,
        own_stt_key: bool = False,
        own_tts_key: bool = False,
    ) -> None:
        """Bind to one tenant, and optionally to one meeting."""
        self.user_id = user_id
        self.meeting_id = meeting_id
        self.own_llm_key = own_llm_key
        self.own_stt_key = own_stt_key
        self.own_tts_key = own_tts_key
        self._pending: list[_Pending] = []
        self._lock = threading.Lock()
        self.totals: dict[str, float] = {"llm": 0.0, "stt": 0.0, "tts": 0.0}
        self.cost_usd: float = 0.0

    def bind_meeting(self, meeting_id: uuid.UUID | None) -> None:
        """Attribute subsequent events to a meeting."""
        self.flush()
        self.meeting_id = meeting_id

    # -- inputs ---------------------------------------------------------- #

    def record_llm(self, report: UsageReport) -> None:
        """Sink for :data:`src.llm.UsageCallback`."""
        total = report.input_tokens + report.output_tokens
        cost = estimate_llm_cost(report.model, report.input_tokens, report.output_tokens)
        self._append(
            _Pending(
                service=UsageService.LLM.value,
                provider=report.provider,
                model=report.model,
                unit="tokens",
                input_units=float(report.input_tokens),
                output_units=float(report.output_tokens),
                units=float(total),
                cost_usd=cost,
                own_key=self.own_llm_key,
                meeting_id=self.meeting_id,
                detail=report.role,
            )
        )
        self.totals["llm"] += total
        self.cost_usd += cost

    def record_stt(self, seconds: float, *, model: str = "", detail: str | None = None) -> None:
        """Record audio transcribed, in seconds."""
        if seconds <= 0:
            return
        cost = (seconds / 60.0) * _STT_USD_PER_MINUTE
        self._append(
            _Pending(
                service=UsageService.STT.value, provider="deepgram", model=model, unit="seconds",
                input_units=seconds, output_units=0.0, units=seconds, cost_usd=cost,
                own_key=self.own_stt_key, meeting_id=self.meeting_id, detail=detail,
            )
        )
        self.totals["stt"] += seconds
        self.cost_usd += cost

    def record_tts(self, characters: int, *, model: str = "", detail: str | None = None) -> None:
        """Record speech synthesised, in characters."""
        if characters <= 0:
            return
        cost = (characters / 1000.0) * _TTS_USD_PER_1K_CHARS
        self._append(
            _Pending(
                service=UsageService.TTS.value, provider="cartesia", model=model, unit="characters",
                input_units=float(characters), output_units=0.0, units=float(characters), cost_usd=cost,
                own_key=self.own_tts_key, meeting_id=self.meeting_id, detail=detail,
            )
        )
        self.totals["tts"] += characters
        self.cost_usd += cost

    def record_meeting_minutes(self, minutes: float) -> None:
        """Record wall-clock time a delegate spent in a call."""
        if minutes <= 0:
            return
        self._append(
            _Pending(
                service=UsageService.MEETING.value, provider="deleg8", model="", unit="minutes",
                input_units=minutes, output_units=0.0, units=minutes, cost_usd=0.0,
                own_key=False, meeting_id=self.meeting_id, detail=None,
            )
        )

    # -- persistence ------------------------------------------------------ #

    def _append(self, event: _Pending) -> None:
        with self._lock:
            self._pending.append(event)
            # Buffering exists to keep a database write out of a live meeting
            # turn. Outside a meeting - a dossier rebuild, a console query -
            # there is no such deadline, and a buffered event that is never
            # flushed is an event that never happened as far as the quota is
            # concerned, so write it straight through.
            should_flush = self.meeting_id is None or len(self._pending) >= self.FLUSH_THRESHOLD
        if should_flush:
            self.flush()

    def flush(self) -> int:
        """Write buffered events. Never raises. Returns how many were written."""
        with self._lock:
            batch, self._pending = self._pending, []
        if not batch:
            return 0
        try:
            with session_scope() as session:
                for event in batch:
                    repo.record_usage(
                        session,
                        user_id=self.user_id,
                        service=event.service,
                        provider=event.provider,
                        model=event.model,
                        unit=event.unit,
                        input_units=event.input_units,
                        output_units=event.output_units,
                        units=event.units,
                        cost_usd=event.cost_usd,
                        own_key=event.own_key,
                        meeting_id=event.meeting_id,
                        detail=event.detail,
                    )
        except Exception:  # noqa: BLE001 - metering must not break the caller
            logger.exception("could not persist %d usage event(s) for %s", len(batch), self.user_id)
            return 0
        return len(batch)

    def describe(self) -> str:
        """One line for logs and the session summary."""
        return (
            f"usage: {self.totals['llm']:.0f} tokens, {self.totals['stt']:.0f}s audio, "
            f"{self.totals['tts']:.0f} characters spoken (~${self.cost_usd:.3f})"
        )


class NullMeter(UsageMeter):
    """A meter that records nothing, for tests and the unauthenticated CLI path."""

    def __init__(self) -> None:
        """No tenant, no writes."""
        super().__init__(uuid.UUID(int=0))

    def flush(self) -> int:
        """Discard everything."""
        with self._lock:
            self._pending.clear()
        return 0


def usage_window(days: int = 30) -> datetime:
    """UTC timestamp ``days`` ago."""
    return datetime.now(timezone.utc) - timedelta(days=days)
