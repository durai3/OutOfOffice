"""
Database-backed implementations of the stores the rest of DELEG8 already talks to.

Each class here keeps the exact shape of the single-tenant store it replaces,
so nothing in the brain, the turn processor, the reporter or the dossier
builder had to learn about SQL:

    JsonFileProfileStore  -> DbProfileStore       (ProfileStore protocol)
    MeetingHistoryStore   -> DbHistoryStore       (ReportArchive protocol)
    DossierStore          -> DbDossierStore       (load / save / save_edit)
    CommitmentRegistry    -> DbCommitmentSink     (write-through, see src.memory.commitments)

:class:`DbProfileStore` caches. It has to: ``get_profile`` is called on every
single transcript turn from synchronous code inside the policy engine, and a
round trip to Postgres per utterance would put database latency inside the
delegate's response time. The cache is per (process, principal) and is
invalidated on every save, including saves made by other processes, through a
short freshness window.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from src.brain.models import KnowledgeEntry, PolicyRule, PolicyTier, UserProfile
from src.brain.profile_store import ProfileNotFoundError
from src.db import repo
from src.db.base import session_scope
from src.db.models import (
    CommitmentRow,
    MeetingReport,
    Principal,
)
from src.db.models import (
    Dossier as DossierRow,
)

logger = logging.getLogger("deleg8.db.stores")

_SLUG_RE = re.compile(r"[^a-z0-9]+")

PROFILE_CACHE_SECONDS: float = 5.0
"""How long a cached profile is trusted before it is re-read.

Short enough that a policy edit made in one process (the web console) reaches
a delegate already running in another within a turn or two; long enough that a
busy meeting does not query the database on every utterance."""


def slugify(text: str, *, fallback: str = "user") -> str:
    """A principal slug: lower-case, ``a-z0-9-._`` only, never empty.

    Must satisfy ``UserProfile.user_id``'s pattern, because the slug *is* what
    the brain calls ``user_id`` everywhere downstream.
    """
    candidate = _SLUG_RE.sub("-", text.strip().lower()).strip("-.")
    candidate = re.sub(r"-{2,}", "-", candidate)
    if not candidate or not candidate[0].isalnum():
        candidate = f"{fallback}-{candidate}".strip("-")
    return candidate[:60] or fallback


# --------------------------------------------------------------------------- #
# Representation Policy
# --------------------------------------------------------------------------- #

def profile_from_principal(principal: Principal) -> UserProfile:
    """Project the relational rows onto the pydantic contract the brain uses."""
    return UserProfile(
        user_id=principal.slug,
        display_name=principal.display_name,
        aliases=list(principal.aliases or []),
        policy=[
            PolicyRule(
                topic=rule.topic,
                tier=PolicyTier(rule.tier),
                description=rule.description,
                triggers=list(rule.triggers or []),
            )
            for rule in principal.policy_rules
        ],
        knowledge_base=[
            KnowledgeEntry(
                topic=entry.topic,
                subject=entry.subject,
                keywords=list(entry.keywords or []),
                answer=entry.answer,
            )
            for entry in principal.knowledge_entries
        ],
    )


class DbProfileStore:
    """:class:`~src.brain.profile_store.ProfileStore` backed by ``principals`` + ``policy_rules``.

    Scoped to one user when ``user_id`` is given (the API path: a request may
    only ever see its own principals) and unscoped when it is not (the worker
    path: a container is launched for exactly one principal and looks it up by
    slug).
    """

    def __init__(self, user_id: uuid.UUID | None = None, *, cache_seconds: float = PROFILE_CACHE_SECONDS) -> None:
        """``user_id`` restricts every lookup to that account's principals."""
        self._user_id = user_id
        self._cache: dict[str, tuple[float, UserProfile]] = {}
        self._cache_seconds = cache_seconds
        self._lock = threading.RLock()

    @property
    def user_id(self) -> uuid.UUID | None:
        """The account this store is scoped to, if any."""
        return self._user_id

    def _now(self) -> float:
        return datetime.now(timezone.utc).timestamp()

    def _load(self, slug: str) -> UserProfile:
        with session_scope() as session:
            principal = repo.get_principal_by_slug(session, slug)
            if principal is None or (self._user_id is not None and principal.user_id != self._user_id):
                raise ProfileNotFoundError(slug)
            return profile_from_principal(principal)

    def get_profile(self, user_id: str) -> UserProfile:
        """Return the profile for a principal slug or raise :class:`ProfileNotFoundError`."""
        now = self._now()
        with self._lock:
            cached = self._cache.get(user_id)
            if cached is not None and now - cached[0] < self._cache_seconds:
                return cached[1]
        profile = self._load(user_id)
        with self._lock:
            self._cache[user_id] = (now, profile)
        return profile

    def list_user_ids(self) -> list[str]:
        """Every principal slug this store can see."""
        with session_scope() as session:
            if self._user_id is not None:
                principal = repo.primary_principal(session, self._user_id)
                return [principal.slug] if principal is not None else []
            from sqlalchemy import select

            return [slug for (slug,) in session.execute(select(Principal.slug).order_by(Principal.created_at))]

    def save_profile(self, profile: UserProfile) -> None:
        """Replace a principal's whole Representation Policy."""
        with session_scope() as session:
            principal = repo.get_principal_by_slug(session, profile.user_id)
            if principal is None or (self._user_id is not None and principal.user_id != self._user_id):
                raise ProfileNotFoundError(profile.user_id)
            repo.replace_policy(
                session,
                principal,
                display_name=profile.display_name,
                aliases=profile.aliases,
                rules=[
                    {
                        "topic": rule.topic,
                        "tier": rule.tier.value,
                        "description": rule.description,
                        "triggers": list(rule.triggers),
                    }
                    for rule in profile.policy
                ],
                knowledge=[
                    {
                        "topic": entry.topic,
                        "subject": entry.subject,
                        "keywords": list(entry.keywords),
                        "answer": entry.answer,
                    }
                    for entry in profile.knowledge_base
                ],
            )
        self.invalidate(profile.user_id)
        logger.info(
            "saved Representation Policy for %s (%d rules, %d facts)",
            profile.user_id, len(profile.policy), len(profile.knowledge_base),
        )

    def invalidate(self, slug: str | None = None) -> None:
        """Drop one (or every) cached profile."""
        with self._lock:
            if slug is None:
                self._cache.clear()
            else:
                self._cache.pop(slug, None)


# --------------------------------------------------------------------------- #
# Meeting archive
# --------------------------------------------------------------------------- #

def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "meeting"


class DbHistoryStore:
    """:class:`~src.memory.post_meeting.ReportArchive` backed by ``meeting_reports``.

    One instance per user. ``save`` is called from the meeting session, which
    is why it is synchronous and opens its own transaction: the briefing path
    must not depend on a request-scoped session that has long since closed.
    """

    def __init__(self, user_id: uuid.UUID, meeting_id: uuid.UUID | None = None) -> None:
        """Archive every report under ``user_id``, optionally linked to one meeting row."""
        self._user_id = user_id
        self._meeting_id = meeting_id

    @property
    def meeting_id(self) -> uuid.UUID | None:
        """The meeting row new reports are linked to."""
        return self._meeting_id

    @meeting_id.setter
    def meeting_id(self, value: uuid.UUID | None) -> None:
        self._meeting_id = value

    def describe(self) -> str:
        """Status line for health checks."""
        return f"history: {self.count()} brief(s) in the database"

    def save(self, report: Any) -> str:
        """Archive ``report``; returns the new ``brief_id``."""
        payload = report.model_dump(mode="json")
        stamp = report.concluded_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base = f"{stamp}-{_slug(report.meeting_id)}"
        with session_scope() as session:
            brief_id, suffix = base, 1
            while repo.report_id_is_taken(session, self._user_id, brief_id):
                suffix += 1
                brief_id = f"{base}-{suffix}"
            meeting_id = self._meeting_id
            if meeting_id is None:
                meeting = repo.get_meeting_by_key(session, self._user_id, report.meeting_id)
                meeting_id = meeting.id if meeting is not None else None
            repo.save_report(
                session,
                user_id=self._user_id,
                meeting_id=meeting_id,
                brief_id=brief_id,
                meeting_key=report.meeting_id,
                concluded_at=report.concluded_at,
                executive_summary=report.briefing.executive_summary,
                summariser=report.summariser,
                delivered=report.receipt.delivered,
                delivery_channel=report.receipt.channel,
                action_item_count=len(report.briefing.action_items),
                payload=payload,
            )
        logger.info("archived briefing for %s as %s", report.meeting_id, brief_id)
        return brief_id

    def count(self) -> int:
        """How many briefs this user has."""
        with session_scope() as session:
            return repo.count_reports(session, self._user_id)

    def list_rows(self, limit: int | None = None) -> list[MeetingReport]:
        """Raw archive rows, newest first."""
        with session_scope() as session:
            rows = repo.list_reports(session, self._user_id, limit)
            session.expunge_all()
            return list(rows)

    def get_payload(self, brief_id: str) -> dict | None:
        """The stored report JSON for one brief, or ``None``."""
        with session_scope() as session:
            row = repo.get_report(session, self._user_id, brief_id)
            return dict(row.payload) if row is not None else None

    def pending_action_items(self) -> int:
        """Action items recorded across every archived brief."""
        with session_scope() as session:
            return repo.sum_action_items(session, self._user_id)


# --------------------------------------------------------------------------- #
# Dossier
# --------------------------------------------------------------------------- #

class DbDossierStore:
    """Duck-compatible replacement for :class:`~src.knowledge.dossier.DossierStore`.

    Same three methods the builder and the console call (``load`` / ``save`` /
    ``save_edit``), keyed by principal slug instead of by file path.
    """

    def __init__(self, user_id: uuid.UUID | None = None) -> None:
        """``user_id`` restricts lookups to one account's principals."""
        self._user_id = user_id

    def _principal(self, session: Any, slug: str) -> Principal | None:
        principal = repo.get_principal_by_slug(session, slug)
        if principal is None:
            return None
        if self._user_id is not None and principal.user_id != self._user_id:
            return None
        return principal

    def load(self, user_id: str) -> Any:
        """Load the dossier for a principal slug, returning an empty one when none exists."""
        from src.knowledge.dossier import Dossier, DossierMeta

        with session_scope() as session:
            principal = self._principal(session, user_id)
            row: DossierRow | None = (
                repo.get_dossier(session, principal.id) if principal is not None else None
            )
            if row is None or not row.markdown:
                return Dossier(user_id=user_id, markdown="", meta=None)
            meta = DossierMeta(
                user_id=user_id,
                built_at=row.built_at or datetime.now(timezone.utc),
                built_by=row.built_by or "unknown",
                sources=list(row.sources or []),
                source_fingerprint=row.source_fingerprint or "",
                hand_edited=row.hand_edited,
            )
            return Dossier(user_id=user_id, markdown=row.markdown, meta=meta)

    def save(self, user_id: str, markdown: str, meta: Any) -> None:
        """Write the dossier and its provenance."""
        with session_scope() as session:
            principal = self._principal(session, user_id)
            if principal is None:
                raise ProfileNotFoundError(user_id)
            repo.save_dossier(
                session,
                principal.id,
                markdown=markdown,
                built_by=meta.built_by,
                built_at=meta.built_at,
                sources=list(meta.sources),
                source_fingerprint=meta.source_fingerprint,
                hand_edited=meta.hand_edited,
            )
        logger.info("dossier saved for %s: %d chars (%s)", user_id, len(markdown), meta.built_by)

    def save_edit(self, user_id: str, markdown: str) -> None:
        """Save a human edit, preserving provenance but marking it hand-edited."""
        from src.knowledge.dossier import DossierMeta

        existing = self.load(user_id)
        meta = existing.meta or DossierMeta(
            user_id=user_id, built_at=datetime.now(timezone.utc), built_by="hand-edited"
        )
        meta = meta.model_copy(update={"hand_edited": True, "built_at": datetime.now(timezone.utc)})
        self.save(user_id, markdown, meta)


# --------------------------------------------------------------------------- #
# Commitments
# --------------------------------------------------------------------------- #

class DbCommitmentSink:
    """Write-through persistence for the in-memory commitments ledger.

    The ledger itself stays in memory - merging a restatement into an open
    commitment is a hot-path operation that has no business being a SQL
    round trip - but every change it makes is mirrored here, so an action item
    survives the process that heard it and is visible on another machine's
    console while the meeting is still running.
    """

    def __init__(self, user_id: uuid.UUID) -> None:
        """Mirror commitments for one account."""
        self._user_id = user_id
        self._meetings: dict[str, uuid.UUID] = {}

    def bind(self, meeting_key: str, meeting_id: uuid.UUID) -> None:
        """Associate an in-memory meeting id with its database row."""
        self._meetings[meeting_key] = meeting_id

    def persist(self, meeting_key: str, commitment: Any) -> None:
        """Mirror one commitment. Never raises: a meeting must not fail on a write."""
        meeting_id = self._meetings.get(meeting_key)
        if meeting_id is None:
            return
        try:
            with session_scope() as session:
                repo.upsert_commitment(
                    session,
                    user_id=self._user_id,
                    meeting_id=meeting_id,
                    local_id=commitment.id,
                    description=commitment.description,
                    owner=commitment.owner,
                    requested_by=commitment.requested_by,
                    due_iso=commitment.due_iso,
                    due_text=commitment.due_text,
                    status=commitment.status.value,
                    source=commitment.source.value,
                    acknowledged=commitment.acknowledged,
                    first_turn=commitment.first_turn,
                    last_turn=commitment.last_turn,
                    notified_at=commitment.notified_at,
                )
        except Exception:  # noqa: BLE001 - persistence must never break a live meeting
            logger.exception("could not persist commitment %s for %s", commitment.id, meeting_key)

    def open_rows(self, limit: int = 200) -> list[CommitmentRow]:
        """Every open commitment for this user."""
        with session_scope() as session:
            rows = repo.list_commitments(session, self._user_id, open_only=True, limit=limit)
            session.expunge_all()
            return list(rows)


def report_window(days: int = 30) -> datetime:
    """UTC timestamp ``days`` ago, for usage and quota windows."""
    return datetime.now(timezone.utc) - timedelta(days=days)


__all__ = [
    "DbCommitmentSink",
    "DbDossierStore",
    "DbHistoryStore",
    "DbProfileStore",
    "profile_from_principal",
    "report_window",
    "slugify",
]
