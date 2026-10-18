"""
``/api/v1/search`` - full-text search across archived meetings and action items.

    GET /api/v1/search?q={query}&type={meeting|commitment}

Postgres matches against a ``tsvector`` expression backed by a GIN index
(:func:`src.db.base.ensure_search_indexes`); the excerpt around the first hit
is built in Python so it looks the same on SQLite in development.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from src.auth.deps import CurrentContext
from src.db import repo
from src.db.base import CommittingRoute

router = APIRouter(prefix="/api/v1", tags=["search"], route_class=CommittingRoute)

EXCERPT_RADIUS = 80


class SearchResult(BaseModel):
    """One match, with an excerpt around the query term."""

    type: str
    id: str
    title: str
    excerpt: str
    meeting_key: str | None = None
    occurred_at: str | None = None


class SearchResponse(BaseModel):
    """Every match for one query, across both searched kinds."""

    query: str
    total: int
    results: list[SearchResult]


def _excerpt(haystack: str, query: str, *, radius: int = EXCERPT_RADIUS) -> str:
    """The text around the first occurrence of ``query`` in ``haystack``, or its start."""
    if not haystack:
        return ""
    idx = haystack.lower().find(query.lower())
    if idx == -1:
        snippet = haystack[: radius * 2].strip()
        return snippet + ("…" if len(haystack) > radius * 2 else "")
    start, end = max(0, idx - radius), min(len(haystack), idx + len(query) + radius)
    prefix, suffix = ("…" if start > 0 else ""), ("…" if end < len(haystack) else "")
    return f"{prefix}{haystack[start:end].strip()}{suffix}"


@router.get("/search", response_model=SearchResponse)
def search(
    context: CurrentContext,
    q: str = Query(..., min_length=1, max_length=200),
    type: str | None = Query(default=None, description="'meeting' or 'commitment'; omit for both."),
) -> SearchResponse:
    """Search this account's meetings, decisions, topics, and action items."""
    if type not in (None, "meeting", "commitment"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "type must be 'meeting' or 'commitment'")

    results: list[SearchResult] = []
    if type in (None, "meeting"):
        for report in repo.search_reports(context.session, context.user_id, q, limit=20):
            haystack = f"{report.executive_summary}\n{json.dumps(report.payload)}"
            results.append(SearchResult(
                type="meeting",
                id=report.brief_id,
                title=report.meeting_key,
                excerpt=_excerpt(haystack, q),
                meeting_key=report.meeting_key,
                occurred_at=report.concluded_at.isoformat(),
            ))
    if type in (None, "commitment"):
        for row in repo.search_commitments(context.session, context.user_id, q, limit=20):
            results.append(SearchResult(
                type="commitment",
                id=str(row.id),
                title=row.description,
                excerpt=_excerpt(f"{row.description} — requested by {row.requested_by or 'the room'}", q),
                meeting_key=None,
                occurred_at=row.updated_at.isoformat(),
            ))
    return SearchResponse(query=q, total=len(results), results=results)
