"""
``/api/v1/usage`` - what this account has spent, and against which allowance.

    GET /api/v1/usage            this month's totals, the plan, and how close to the limits
    GET /api/v1/usage/events     the raw metered events, newest first
    GET /api/v1/usage/meetings   per-meeting consumption

The distinction that matters on this page is *whose key paid for it*. Calls on
a user's own key are shown - they are still their usage - but excluded from
the allowance, which is the whole reason to bring one.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from src.auth.deps import CurrentContext
from src.db import repo
from src.db.base import CommittingRoute
from src.tenancy.credentials import resolve
from src.usage.meter import month_start, quota_status

logger = logging.getLogger("deleg8.api.usage")

router = APIRouter(prefix="/api/v1/usage", tags=["usage"], route_class=CommittingRoute)


class AllowanceLine(BaseModel):
    """One metered dimension: used, allowed, and how close to the edge."""

    key: str
    label: str
    used: float
    allowed: float
    unit: str
    fraction: float = Field(..., description="0.0-1.0; 1.0 means the allowance is spent.")
    unlimited: bool = False


class UsageSummary(BaseModel):
    """This month's consumption for one account."""

    period_start: datetime
    plan: str
    plan_describe: str
    using_own_keys: bool
    blocked: bool = Field(..., description="True when a new meeting would be refused.")
    reason: str | None = Field(default=None, description="Why it would be refused.")
    near_limit: list[str] = Field(default_factory=list)
    estimated_cost_usd: float
    lines: list[AllowanceLine]
    providers: str = Field(..., description="Which keys this account's calls go out on.")


class UsageEventOut(BaseModel):
    """One metered call."""

    id: str
    service: str
    provider: str
    model: str
    unit: str
    input_units: float
    output_units: float
    units: float
    cost_usd: float
    own_key: bool
    meeting_id: str | None = None
    detail: str | None = None
    created_at: datetime


class MeetingUsage(BaseModel):
    """What one meeting consumed."""

    meeting_id: str
    meeting_key: str
    started_at: datetime | None
    ended_at: datetime | None
    status: str
    units: dict[str, float]


_LABELS: dict[str, tuple[str, str]] = {
    "llm_tokens": ("Language model", "tokens"),
    "stt_seconds": ("Speech to text", "seconds"),
    "tts_characters": ("Text to speech", "characters"),
    "meetings": ("Meetings", "meetings"),
}


@router.get("", response_model=UsageSummary)
def summary(context: CurrentContext) -> UsageSummary:
    """This month's totals against the plan."""
    resolved = resolve(context.session, context.user_id)
    own_keys = not (resolved.uses_platform_llm or resolved.uses_platform_voice)
    org_settings = context.org.settings if context.org is not None else None
    status = quota_status(context.session, context.user_id, own_keys=own_keys, org_settings=org_settings)
    used = {
        "llm_tokens": status.llm_tokens,
        "stt_seconds": status.stt_seconds,
        "tts_characters": status.tts_characters,
        "meetings": float(status.meetings),
    }
    allowed = {
        "llm_tokens": float(status.plan.llm_tokens),
        "stt_seconds": status.plan.stt_seconds,
        "tts_characters": float(status.plan.tts_characters),
        "meetings": float(status.plan.meetings),
    }
    fractions = status.fractions
    return UsageSummary(
        period_start=status.period_start,
        plan=status.plan.name,
        plan_describe=status.plan.describe(),
        using_own_keys=own_keys,
        blocked=status.blocked,
        reason=status.reason() if status.blocked else None,
        near_limit=status.near_limit,
        estimated_cost_usd=round(status.cost_usd, 4),
        lines=[
            AllowanceLine(
                key=key,
                label=_LABELS[key][0],
                used=round(used[key], 2),
                allowed=allowed[key],
                unit=_LABELS[key][1],
                fraction=round(fractions[key], 4),
                unlimited=own_keys or allowed[key] <= 0,
            )
            for key in _LABELS
        ],
        providers=resolved.describe(),
    )


@router.get("/events", response_model=list[UsageEventOut])
def events(
    context: CurrentContext,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[UsageEventOut]:
    """The raw metered events for this account, newest first."""
    return [
        UsageEventOut(
            id=str(row.id),
            service=row.service,
            provider=row.provider,
            model=row.model,
            unit=row.unit,
            input_units=row.input_units,
            output_units=row.output_units,
            units=row.units,
            cost_usd=round(row.cost_usd, 6),
            own_key=row.own_key,
            meeting_id=str(row.meeting_id) if row.meeting_id else None,
            detail=row.detail,
            created_at=row.created_at,
        )
        for row in repo.recent_usage(context.session, context.user_id, limit)
    ]


@router.get("/meetings", response_model=list[MeetingUsage])
def per_meeting(
    context: CurrentContext,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[MeetingUsage]:
    """What each recent meeting consumed."""
    out: list[MeetingUsage] = []
    for meeting in repo.list_meetings(context.session, context.user_id, limit=limit):
        out.append(
            MeetingUsage(
                meeting_id=str(meeting.id),
                meeting_key=meeting.meeting_key,
                started_at=meeting.started_at,
                ended_at=meeting.ended_at,
                status=meeting.status,
                units=repo.usage_by_meeting(context.session, context.user_id, meeting.id),
            )
        )
    return out


@router.get("/period-start", response_model=datetime)
def period_start() -> datetime:
    """When the current allowance period began."""
    return month_start()
