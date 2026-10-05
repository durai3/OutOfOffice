"""
Meeting history: every concluded meeting's briefing, kept on disk.

The live :class:`~src.memory.meeting_state.MeetingStateRegistry` forgets a
meeting the moment it is concluded, and until now the executive briefing
only ever went to Telegram. :class:`MeetingHistoryStore` archives each
:class:`~src.memory.post_meeting.PostMeetingReport` as one JSON document
under ``DELEG8_HISTORY_DIR`` (default ``data/history/``) so the web console
can list past meetings, their participants, summaries, and action items.

It plugs into :class:`~src.memory.post_meeting.PostMeetingReporter` through
the :class:`~src.memory.post_meeting.ReportArchive` protocol, so both entry
points (the HTTP API and ``python -m src.main join``) archive automatically.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from src.memory.meeting_state import MeetingStats
from src.memory.post_meeting import ActionItemSummary, PostMeetingReport

logger = logging.getLogger("deleg8.memory.history")

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def history_dir_from_env() -> Path:
    """``DELEG8_HISTORY_DIR`` (default ``data/history``), resolved against the project root."""
    raw = os.getenv("DELEG8_HISTORY_DIR", "").strip() or "data/history"
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else (PROJECT_ROOT / candidate).resolve()


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "meeting"


class MeetingBrief(BaseModel):
    """The list-view projection of one archived meeting."""

    brief_id: str = Field(..., description="Stable id of the archived report (its file stem).")
    meeting_id: str
    user_id: str
    started_at: datetime
    concluded_at: datetime
    duration_minutes: float
    participants: list[str]
    executive_summary: str
    decisions_made: list[str]
    action_items: list[ActionItemSummary]
    unresolved_questions: list[str]
    summariser: str
    fallback_reason: str | None = None
    delivered: bool = Field(..., description="Whether the briefing reached the human's channel.")
    delivery_channel: str
    stats: MeetingStats

    @classmethod
    def from_report(cls, brief_id: str, report: PostMeetingReport) -> MeetingBrief:
        """Project a full report onto the list shape."""
        snapshot = report.snapshot
        return cls(
            brief_id=brief_id,
            meeting_id=report.meeting_id,
            user_id=snapshot.user_id,
            started_at=snapshot.started_at,
            concluded_at=report.concluded_at,
            duration_minutes=round(snapshot.duration_minutes, 1),
            participants=list(snapshot.participants),
            executive_summary=report.briefing.executive_summary,
            decisions_made=list(report.briefing.decisions_made),
            action_items=list(report.briefing.action_items),
            unresolved_questions=list(report.briefing.unresolved_questions),
            summariser=report.summariser,
            fallback_reason=report.fallback_reason,
            delivered=report.receipt.delivered,
            delivery_channel=report.receipt.channel,
            stats=snapshot.stats,
        )


class MeetingHistoryStore:
    """Append-only archive of post-meeting reports, one JSON file each."""

    def __init__(self, directory: Path | None = None) -> None:
        """``directory`` defaults to :func:`history_dir_from_env`."""
        self._directory = directory or history_dir_from_env()

    @property
    def directory(self) -> Path:
        """Where reports are archived."""
        return self._directory

    def describe(self) -> str:
        """Status line for health checks."""
        return f"history: {self.count()} brief(s) in {self._directory}"

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #

    def save(self, report: PostMeetingReport) -> str:
        """Archive ``report``; returns the new ``brief_id``. Never overwrites an earlier brief."""
        self._directory.mkdir(parents=True, exist_ok=True)
        stamp = report.concluded_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base = f"{stamp}-{_slug(report.meeting_id)}"
        brief_id, suffix = base, 1
        while (self._directory / f"{brief_id}.json").exists():
            suffix += 1
            brief_id = f"{base}-{suffix}"
        path = self._directory / f"{brief_id}.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        logger.info("archived briefing for %s as %s", report.meeting_id, path.name)
        return brief_id

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #

    def _paths(self) -> list[Path]:
        if not self._directory.exists():
            return []
        return sorted((p for p in self._directory.glob("*.json") if p.is_file()), reverse=True)

    def _read(self, path: Path) -> PostMeetingReport | None:
        try:
            return PostMeetingReport.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            logger.error("ignoring unreadable brief %s: %s", path, exc)
            return None

    def count(self) -> int:
        """Number of archived briefs."""
        return len(self._paths())

    def list_briefs(self, limit: int | None = None) -> list[MeetingBrief]:
        """Every archived brief, newest first."""
        briefs: list[MeetingBrief] = []
        for path in self._paths():
            report = self._read(path)
            if report is None:
                continue
            briefs.append(MeetingBrief.from_report(path.stem, report))
            if limit is not None and len(briefs) >= limit:
                break
        return briefs

    def get(self, brief_id: str) -> PostMeetingReport | None:
        """The full archived report, or ``None`` when unknown."""
        if not re.fullmatch(r"[A-Za-z0-9._-]+", brief_id):
            return None
        path = self._directory / f"{brief_id}.json"
        if not path.is_file():
            return None
        return self._read(path)

    def pending_action_items(self) -> int:
        """Action items recorded across every archived brief."""
        return sum(len(brief.action_items) for brief in self.list_briefs())
