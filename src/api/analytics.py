"""
``/api/v1/analytics`` - meeting health trends and ROI.

    GET /api/v1/analytics/health?weeks=4   per-meeting scores + a weekly trend
    GET /api/v1/analytics/roi?days=30      meetings, decisions, completion, estimated value

Health scores are computed once per meeting from its archived report
(:mod:`src.analytics.scoring`) and cached in ``meeting_metrics`` - the source
data never changes after a meeting concludes, so there is nothing to
invalidate. ROI is computed fresh each call: it aggregates a rolling window,
which shifts on every call by definition.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from pydantic import BaseModel

from src.analytics.scoring import HealthScores, score_meeting
from src.auth.deps import CurrentContext
from src.db import repo
from src.db.base import CommittingRoute
from src.db.models import MeetingReport

logger = logging.getLogger("deleg8.api.analytics")

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"], route_class=CommittingRoute)

HOURLY_VALUE_USD = 150.0
"""Flat rate used to estimate the value of hours the principal did not have to spend in a meeting."""
DEFAULT_MEETING_HOURS = 0.5
"""Assumed duration when a meeting's start/end timestamps were not recorded."""


class MeetingHealth(BaseModel):
    """One meeting's scores and flags."""

    meeting_id: str
    meeting_key: str
    concluded_at: datetime
    decision_clarity: float
    participation_score: float
    agenda_adherence: float
    action_item_clarity: float
    health_score: float
    flags: list[str]


class WeeklyHealthPoint(BaseModel):
    """Average scores for one ISO week, for the trend chart."""

    week_start: str
    meetings: int
    decision_clarity: float
    participation_score: float
    agenda_adherence: float
    action_item_clarity: float
    health_score: float


class HealthResponse(BaseModel):
    """Everything the Health analytics page shows."""

    weekly_trend: list[WeeklyHealthPoint]
    meetings: list[MeetingHealth]


class RoiResponse(BaseModel):
    """Everything the ROI analytics page shows, for one trailing window."""

    days: int
    meetings_attended: int
    decisions_made: int
    completion_pct: float
    hours_saved: float
    estimated_value_usd: float


def _week_start(when: datetime) -> str:
    monday = when.date() - timedelta(days=when.weekday())
    return monday.isoformat()


def _scores_for(session, user_id, report: MeetingReport) -> HealthScores:  # noqa: ANN001
    """Cached score for one report, computing and caching it on first use."""
    if report.meeting_id is not None:
        cached = repo.get_meeting_metrics(session, report.meeting_id)
        if cached is not None:
            return HealthScores(
                decision_clarity=cached.decision_clarity,
                participation_score=cached.participation_score,
                agenda_adherence=cached.agenda_adherence,
                action_item_clarity=cached.action_item_clarity,
                health_score=cached.health_score,
                flags=list(cached.flags),
            )
    scores = score_meeting(report.payload)
    if report.meeting_id is not None:
        repo.upsert_meeting_metrics(
            session,
            user_id=user_id,
            meeting_id=report.meeting_id,
            decision_clarity=scores.decision_clarity,
            participation_score=scores.participation_score,
            agenda_adherence=scores.agenda_adherence,
            action_item_clarity=scores.action_item_clarity,
            health_score=scores.health_score,
            flags=scores.flags,
        )
    return scores


@router.get("/health", response_model=HealthResponse)
def health_analytics(context: CurrentContext, weeks: int = Query(default=4, ge=1, le=26)) -> HealthResponse:
    """Scored meetings over the trailing ``weeks`` weeks, plus a weekly trend."""
    since = datetime.now(timezone.utc) - timedelta(weeks=weeks)
    reports = [
        r for r in repo.list_reports(context.session, context.user_id, limit=500) if r.concluded_at >= since
    ]
    meeting_keys = {m.id: m.meeting_key for m in repo.list_meetings(context.session, context.user_id, limit=500)}

    meetings: list[MeetingHealth] = []
    buckets: dict[str, list[HealthScores]] = defaultdict(list)
    for report in reports:
        scores = _scores_for(context.session, context.user_id, report)
        meetings.append(MeetingHealth(
            meeting_id=str(report.meeting_id) if report.meeting_id else "",
            meeting_key=meeting_keys.get(report.meeting_id, report.meeting_key) if report.meeting_id else report.meeting_key,
            concluded_at=report.concluded_at,
            decision_clarity=scores.decision_clarity,
            participation_score=scores.participation_score,
            agenda_adherence=scores.agenda_adherence,
            action_item_clarity=scores.action_item_clarity,
            health_score=scores.health_score,
            flags=scores.flags,
        ))
        buckets[_week_start(report.concluded_at)].append(scores)

    trend = [
        WeeklyHealthPoint(
            week_start=week,
            meetings=len(scored),
            decision_clarity=round(sum(s.decision_clarity for s in scored) / len(scored), 3),
            participation_score=round(sum(s.participation_score for s in scored) / len(scored), 3),
            agenda_adherence=round(sum(s.agenda_adherence for s in scored) / len(scored), 3),
            action_item_clarity=round(sum(s.action_item_clarity for s in scored) / len(scored), 3),
            health_score=round(sum(s.health_score for s in scored) / len(scored), 3),
        )
        for week, scored in sorted(buckets.items())
    ]
    meetings.sort(key=lambda m: m.concluded_at, reverse=True)
    return HealthResponse(weekly_trend=trend, meetings=meetings)


@router.get("/roi", response_model=RoiResponse)
def roi_analytics(context: CurrentContext, days: int = Query(default=30, ge=1, le=365)) -> RoiResponse:
    """Meetings attended, decisions made, completion rate, and estimated value for the trailing ``days``."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    reports = [
        r for r in repo.list_reports(context.session, context.user_id, limit=1000) if r.concluded_at >= since
    ]
    meetings_by_id = {m.id: m for m in repo.list_meetings(context.session, context.user_id, limit=1000)}
    commitments = [
        c for c in repo.list_commitments(context.session, context.user_id, open_only=False, limit=2000)
        if c.created_at >= since
    ]

    decisions_made = sum(len((r.payload.get("briefing") or {}).get("decisions_made") or []) for r in reports)

    hours_saved = 0.0
    for report in reports:
        meeting = meetings_by_id.get(report.meeting_id) if report.meeting_id else None
        if meeting is not None and meeting.started_at and meeting.ended_at:
            hours_saved += max(0.0, (meeting.ended_at - meeting.started_at).total_seconds() / 3600.0)
        else:
            hours_saved += DEFAULT_MEETING_HOURS

    done = sum(1 for c in commitments if c.status == "done")
    decided_count = sum(1 for c in commitments if c.status in ("done", "dropped"))
    completion_pct = round(100.0 * done / decided_count, 1) if decided_count else 0.0

    return RoiResponse(
        days=days,
        meetings_attended=len(reports),
        decisions_made=decisions_made,
        completion_pct=completion_pct,
        hours_saved=round(hours_saved, 2),
        estimated_value_usd=round(hours_saved * HOURLY_VALUE_USD, 2),
    )
