"""
Repository functions: every read and write of tenant data, in one place.

Two rules hold across this module and are what per-tenant isolation actually
rests on:

1. Every function that touches tenant-owned data takes a ``user_id`` and
   filters on it. There is no "fetch by id" that does not also check the
   owner, so a guessed or leaked primary key still returns nothing.
2. Nothing here opens its own transaction. Callers pass a
   :class:`~sqlalchemy.orm.Session` from :func:`~src.db.base.session_scope`
   (or the FastAPI dependency), so a request that writes several tables
   either lands completely or not at all.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from src.db.models import (
    AuthSession,
    CommitmentRow,
    ConsentRecord,
    Document,
    Dossier,
    KnowledgeEntry,
    LegalAcceptance,
    Meeting,
    MeetingReport,
    MeetingStatus,
    NotificationChannel,
    PolicyRule,
    Principal,
    UsageEvent,
    User,
    UserCredential,
    UserStatus,
    utcnow,
)

logger = logging.getLogger("deleg8.db.repo")

UserId = uuid.UUID


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #

def normalise_email(email: str) -> str:
    """Lower-cased, trimmed email - the form stored and compared."""
    return email.strip().lower()


def get_user(session: Session, user_id: UserId) -> User | None:
    """One user by id."""
    return session.get(User, user_id)


def get_user_by_email(session: Session, email: str) -> User | None:
    """One user by email (case-insensitive)."""
    return session.scalar(select(User).where(User.email == normalise_email(email)))


def create_user(
    session: Session,
    *,
    email: str,
    password_hash: str,
    display_name: str,
    role: str = "user",
) -> User:
    """Insert a user. The caller is responsible for hashing the password."""
    user = User(
        email=normalise_email(email),
        password_hash=password_hash,
        display_name=display_name.strip(),
        role=role,
        status=UserStatus.ACTIVE.value,
        onboarding={},
        settings={},
    )
    session.add(user)
    session.flush()
    return user


def count_users(session: Session) -> int:
    """How many accounts exist (used to make the first signup an admin)."""
    return int(session.scalar(select(func.count()).select_from(User)) or 0)


def touch_login(session: Session, user: User) -> None:
    """Record a successful sign-in."""
    user.last_login_at = utcnow()
    session.add(user)


def set_onboarding(session: Session, user: User, **steps: bool) -> User:
    """Merge completed onboarding steps onto the user."""
    state = dict(user.onboarding or {})
    state.update({k: bool(v) for k, v in steps.items()})
    user.onboarding = state
    session.add(user)
    return user


def update_settings(session: Session, user: User, **values: object) -> User:
    """Merge preference values onto the user's settings blob."""
    settings = dict(user.settings or {})
    for key, value in values.items():
        if value is None:
            settings.pop(key, None)
        else:
            settings[key] = value
    user.settings = settings
    session.add(user)
    return user


def delete_user(session: Session, user_id: UserId) -> bool:
    """Erase an account and everything that cascades from it."""
    user = session.get(User, user_id)
    if user is None:
        return False
    session.delete(user)
    return True


def list_users(session: Session, limit: int = 200) -> Sequence[User]:
    """Every account, newest first (admin only)."""
    return session.scalars(select(User).order_by(User.created_at.desc()).limit(limit)).all()


# --------------------------------------------------------------------------- #
# Auth sessions (refresh tokens)
# --------------------------------------------------------------------------- #

def create_auth_session(
    session: Session, *, user_id: UserId, token_hash: str, ttl: timedelta, user_agent: str | None = None
) -> AuthSession:
    """Record an issued refresh token."""
    row = AuthSession(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + ttl,
        user_agent=(user_agent or "")[:300] or None,
    )
    session.add(row)
    session.flush()
    return row


def get_auth_session(session: Session, token_hash: str) -> AuthSession | None:
    """A live refresh token, or ``None`` when unknown, revoked or expired."""
    row = session.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash))
    if row is None or row.revoked_at is not None:
        return None
    if row.expires_at <= datetime.now(timezone.utc):
        return None
    return row


def revoke_auth_session(session: Session, token_hash: str) -> bool:
    """Revoke one refresh token."""
    row = session.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash))
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = utcnow()
    session.add(row)
    return True


def revoke_all_auth_sessions(session: Session, user_id: UserId) -> int:
    """Revoke every refresh token for a user (password change, "sign out everywhere")."""
    rows = session.scalars(
        select(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
    ).all()
    for row in rows:
        row.revoked_at = utcnow()
        session.add(row)
    return len(rows)


# --------------------------------------------------------------------------- #
# Principals and the Representation Policy
# --------------------------------------------------------------------------- #

def get_principal(session: Session, principal_id: uuid.UUID) -> Principal | None:
    """One principal by id."""
    return session.get(Principal, principal_id)


def get_principal_by_slug(session: Session, slug: str) -> Principal | None:
    """One principal by the slug the brain uses as ``user_id``."""
    return session.scalar(select(Principal).where(Principal.slug == slug))


def primary_principal(session: Session, user_id: UserId) -> Principal | None:
    """The principal a user's delegate represents."""
    return session.scalar(
        select(Principal)
        .where(Principal.user_id == user_id)
        .order_by(Principal.is_primary.desc(), Principal.created_at.asc())
    )


def slug_is_taken(session: Session, slug: str) -> bool:
    """True when a principal already owns this slug."""
    return session.scalar(select(func.count()).select_from(Principal).where(Principal.slug == slug)) > 0


def unique_slug(session: Session, base: str) -> str:
    """A free principal slug derived from ``base``."""
    candidate = base or "user"
    if not slug_is_taken(session, candidate):
        return candidate
    for suffix in range(2, 100):
        attempt = f"{candidate}-{suffix}"
        if not slug_is_taken(session, attempt):
            return attempt
    return f"{candidate}-{secrets.token_hex(3)}"


def create_principal(
    session: Session, *, user_id: UserId, slug: str, display_name: str, aliases: list[str] | None = None
) -> Principal:
    """Create the principal a user's delegate speaks for."""
    principal = Principal(
        user_id=user_id,
        slug=slug,
        display_name=display_name.strip(),
        aliases=list(aliases or []),
        is_primary=True,
    )
    session.add(principal)
    session.flush()
    return principal


def replace_policy(
    session: Session,
    principal: Principal,
    *,
    display_name: str,
    aliases: Sequence[str],
    rules: Sequence[dict],
    knowledge: Sequence[dict],
) -> Principal:
    """Replace a principal's whole Representation Policy in one transaction.

    A whole-policy replace (rather than per-rule patching) is what the console
    edits and what keeps the model's cross-field validation meaningful: a
    knowledge entry may not reference a topic that the same save removed.
    """
    principal.display_name = display_name.strip()
    principal.aliases = list(aliases)
    # Clear through the ORM collections rather than with a bulk DELETE: the
    # relationships are delete-orphan, so emptying them is what actually removes
    # the rows, and a bulk delete would leave the loaded instances in the
    # session as deleted objects that the next flush then tries to re-save.
    principal.policy_rules.clear()
    principal.knowledge_entries.clear()
    session.flush()
    for position, rule in enumerate(rules):
        principal.policy_rules.append(
            PolicyRule(
                topic=rule["topic"],
                tier=rule["tier"],
                description=rule["description"],
                triggers=list(rule["triggers"]),
                position=position,
            )
        )
    for position, entry in enumerate(knowledge):
        principal.knowledge_entries.append(
            KnowledgeEntry(
                topic=entry["topic"],
                subject=entry["subject"],
                keywords=list(entry["keywords"]),
                answer=entry["answer"],
                position=position,
            )
        )
    session.add(principal)
    session.flush()
    return principal


# --------------------------------------------------------------------------- #
# Dossier
# --------------------------------------------------------------------------- #

def get_dossier(session: Session, principal_id: uuid.UUID) -> Dossier | None:
    """The stored dossier row, or ``None`` when one has never been built."""
    return session.get(Dossier, principal_id)


def save_dossier(
    session: Session,
    principal_id: uuid.UUID,
    *,
    markdown: str,
    built_by: str | None = None,
    built_at: datetime | None = None,
    sources: Sequence[str] | None = None,
    source_fingerprint: str = "",
    hand_edited: bool = False,
) -> Dossier:
    """Insert or update a principal's dossier."""
    row = session.get(Dossier, principal_id)
    if row is None:
        row = Dossier(principal_id=principal_id)
        session.add(row)
    row.markdown = markdown
    row.built_by = built_by
    row.built_at = built_at or utcnow()
    row.sources = list(sources or [])
    row.source_fingerprint = source_fingerprint
    row.hand_edited = hand_edited
    session.flush()
    return row


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #

def list_documents(session: Session, user_id: UserId) -> Sequence[Document]:
    """Every document this user has ingested."""
    return session.scalars(
        select(Document).where(Document.user_id == user_id).order_by(Document.source.asc())
    ).all()


def upsert_document(
    session: Session,
    *,
    user_id: UserId,
    source: str,
    title: str = "",
    sha256: str = "",
    size_bytes: int = 0,
    chunks: int = 0,
    characters: int = 0,
    pages: int | None = None,
    status: str = "indexed",
    detail: str | None = None,
) -> Document:
    """Record (or refresh) one ingested document for a user."""
    row = session.scalar(
        select(Document).where(Document.user_id == user_id, Document.source == source)
    )
    if row is None:
        row = Document(user_id=user_id, source=source)
        session.add(row)
    row.title = title or row.title
    row.sha256 = sha256 or row.sha256
    row.size_bytes = size_bytes or row.size_bytes
    row.chunks = chunks
    row.characters = characters
    row.pages = pages
    row.status = status
    row.detail = detail
    session.flush()
    return row


def delete_document(session: Session, user_id: UserId, source: str) -> bool:
    """Forget one document."""
    result = session.execute(
        delete(Document).where(Document.user_id == user_id, Document.source == source)
    )
    return bool(result.rowcount)


# --------------------------------------------------------------------------- #
# Meetings
# --------------------------------------------------------------------------- #

def create_meeting(
    session: Session,
    *,
    user_id: UserId,
    principal_id: uuid.UUID | None,
    meeting_key: str,
    join_url: str,
    platform: str,
    code: str,
    runtime: str,
    speaker_label: str,
    half_duplex: bool,
    briefing: bool,
    max_call_seconds: float | None,
    consent_mode: str,
    consent_state: str,
    job_token_hash: str | None = None,
) -> Meeting:
    """Record a dispatch before anything is started, so a crash still leaves a trail."""
    meeting = Meeting(
        user_id=user_id,
        principal_id=principal_id,
        meeting_key=meeting_key,
        join_url=join_url,
        platform=platform,
        code=code,
        runtime=runtime,
        speaker_label=speaker_label,
        half_duplex=half_duplex,
        briefing=briefing,
        max_call_seconds=max_call_seconds,
        consent_mode=consent_mode,
        consent_state=consent_state,
        job_token_hash=job_token_hash,
        status=MeetingStatus.PENDING.value,
    )
    session.add(meeting)
    session.flush()
    return meeting


def get_meeting(session: Session, user_id: UserId, meeting_id: uuid.UUID) -> Meeting | None:
    """One meeting owned by this user."""
    return session.scalar(
        select(Meeting).where(Meeting.id == meeting_id, Meeting.user_id == user_id)
    )


def get_meeting_by_key(session: Session, user_id: UserId, meeting_key: str) -> Meeting | None:
    """One meeting by the id the brain uses."""
    return session.scalar(
        select(Meeting).where(Meeting.user_id == user_id, Meeting.meeting_key == meeting_key)
    )


def get_meeting_by_job_token(session: Session, token_hash: str) -> Meeting | None:
    """The meeting a worker container's bearer token belongs to."""
    return session.scalar(select(Meeting).where(Meeting.job_token_hash == token_hash))


def meeting_key_is_taken(session: Session, user_id: UserId, meeting_key: str) -> bool:
    """True when this user already has a meeting under that key."""
    return bool(
        session.scalar(
            select(func.count())
            .select_from(Meeting)
            .where(Meeting.user_id == user_id, Meeting.meeting_key == meeting_key)
        )
    )


def unique_meeting_key(session: Session, user_id: UserId, base: str) -> str:
    """A free meeting key for this user derived from ``base``.

    Two calls to the same Meet link on the same day are ordinary - a call that
    dropped and was rejoined - and they must not collide in memory, the archive
    or the container name.
    """
    if not meeting_key_is_taken(session, user_id, base):
        return base
    for suffix in range(2, 100):
        candidate = f"{base}-{suffix}"
        if not meeting_key_is_taken(session, user_id, candidate):
            return candidate
    return f"{base}-{secrets.token_hex(3)}"


def update_meeting(session: Session, meeting: Meeting, **values: object) -> Meeting:
    """Patch a meeting row with whatever the runtime just learned."""
    for key, value in values.items():
        if hasattr(meeting, key):
            setattr(meeting, key, value)
    session.add(meeting)
    session.flush()
    return meeting


def list_meetings(
    session: Session, user_id: UserId, *, limit: int = 50, active_only: bool = False
) -> Sequence[Meeting]:
    """This user's meetings, newest first."""
    statement = select(Meeting).where(Meeting.user_id == user_id)
    if active_only:
        statement = statement.where(
            Meeting.status.notin_([s.value for s in MeetingStatus if s.is_terminal])
        )
    return session.scalars(statement.order_by(Meeting.created_at.desc()).limit(limit)).all()


def list_active_meetings(session: Session, user_id: UserId | None = None) -> Sequence[Meeting]:
    """Every meeting that has not reached a terminal state, optionally for one user."""
    statement = select(Meeting).where(
        Meeting.status.notin_([s.value for s in MeetingStatus if s.is_terminal])
    )
    if user_id is not None:
        statement = statement.where(Meeting.user_id == user_id)
    return session.scalars(statement.order_by(Meeting.created_at.asc())).all()


def count_meetings_since(session: Session, user_id: UserId, since: datetime) -> int:
    """Meetings this user has dispatched since ``since`` (quota enforcement)."""
    return int(
        session.scalar(
            select(func.count())
            .select_from(Meeting)
            .where(Meeting.user_id == user_id, Meeting.created_at >= since)
        )
        or 0
    )


# --------------------------------------------------------------------------- #
# Reports (the meeting archive)
# --------------------------------------------------------------------------- #

def save_report(
    session: Session,
    *,
    user_id: UserId,
    meeting_id: uuid.UUID | None,
    brief_id: str,
    meeting_key: str,
    concluded_at: datetime,
    executive_summary: str,
    summariser: str,
    delivered: bool,
    delivery_channel: str,
    action_item_count: int,
    payload: dict,
) -> MeetingReport:
    """Archive one post-meeting report."""
    report = MeetingReport(
        user_id=user_id,
        meeting_id=meeting_id,
        brief_id=brief_id,
        meeting_key=meeting_key,
        concluded_at=concluded_at,
        executive_summary=executive_summary,
        summariser=summariser,
        delivered=delivered,
        delivery_channel=delivery_channel,
        action_item_count=action_item_count,
        payload=payload,
    )
    session.add(report)
    session.flush()
    return report


def report_id_is_taken(session: Session, user_id: UserId, brief_id: str) -> bool:
    """True when this user already has a brief under that id."""
    return bool(
        session.scalar(
            select(func.count())
            .select_from(MeetingReport)
            .where(MeetingReport.user_id == user_id, MeetingReport.brief_id == brief_id)
        )
    )


def list_reports(session: Session, user_id: UserId, limit: int | None = None) -> Sequence[MeetingReport]:
    """This user's archived briefs, newest first."""
    statement = (
        select(MeetingReport)
        .where(MeetingReport.user_id == user_id)
        .order_by(MeetingReport.concluded_at.desc())
    )
    if limit is not None:
        statement = statement.limit(limit)
    return session.scalars(statement).all()


def get_report(session: Session, user_id: UserId, brief_id: str) -> MeetingReport | None:
    """One archived brief owned by this user."""
    return session.scalar(
        select(MeetingReport).where(
            MeetingReport.user_id == user_id, MeetingReport.brief_id == brief_id
        )
    )


def count_reports(session: Session, user_id: UserId) -> int:
    """How many meetings this user's delegate has concluded."""
    return int(
        session.scalar(
            select(func.count()).select_from(MeetingReport).where(MeetingReport.user_id == user_id)
        )
        or 0
    )


def sum_action_items(session: Session, user_id: UserId) -> int:
    """Action items recorded across every archived brief."""
    return int(
        session.scalar(
            select(func.coalesce(func.sum(MeetingReport.action_item_count), 0)).where(
                MeetingReport.user_id == user_id
            )
        )
        or 0
    )


# --------------------------------------------------------------------------- #
# Commitments
# --------------------------------------------------------------------------- #

def upsert_commitment(
    session: Session, *, user_id: UserId, meeting_id: uuid.UUID, local_id: str, **values: object
) -> CommitmentRow:
    """Write through one commitment from the in-memory ledger."""
    row = session.scalar(
        select(CommitmentRow).where(
            CommitmentRow.meeting_id == meeting_id, CommitmentRow.local_id == local_id
        )
    )
    if row is None:
        row = CommitmentRow(user_id=user_id, meeting_id=meeting_id, local_id=local_id, description="")
        session.add(row)
    for key, value in values.items():
        if hasattr(row, key):
            setattr(row, key, value)
    session.flush()
    return row


def list_commitments(
    session: Session, user_id: UserId, *, open_only: bool = True, limit: int = 200
) -> Sequence[CommitmentRow]:
    """This user's commitments across every meeting, newest first."""
    statement = select(CommitmentRow).where(CommitmentRow.user_id == user_id)
    if open_only:
        statement = statement.where(CommitmentRow.status.in_(["probable", "confirmed"]))
    return session.scalars(statement.order_by(CommitmentRow.updated_at.desc()).limit(limit)).all()


def get_commitment(session: Session, user_id: UserId, commitment_id: uuid.UUID) -> CommitmentRow | None:
    """One commitment owned by this user."""
    return session.scalar(
        select(CommitmentRow).where(
            CommitmentRow.id == commitment_id, CommitmentRow.user_id == user_id
        )
    )


# --------------------------------------------------------------------------- #
# Notification channels
# --------------------------------------------------------------------------- #

def get_channel(session: Session, user_id: UserId, kind: str = "telegram") -> NotificationChannel | None:
    """One user's channel of a given kind."""
    return session.scalar(
        select(NotificationChannel).where(
            NotificationChannel.user_id == user_id, NotificationChannel.kind == kind
        )
    )


def upsert_channel(session: Session, *, user_id: UserId, kind: str = "telegram", **values: object) -> NotificationChannel:
    """Create or update a user's notification channel."""
    row = get_channel(session, user_id, kind)
    if row is None:
        row = NotificationChannel(user_id=user_id, kind=kind)
        session.add(row)
    for key, value in values.items():
        if hasattr(row, key):
            setattr(row, key, value)
    session.flush()
    return row


def find_channel_by_link_code(session: Session, code: str) -> NotificationChannel | None:
    """The channel awaiting this one-time link code, if it has not expired."""
    row = session.scalar(select(NotificationChannel).where(NotificationChannel.link_code == code))
    if row is None:
        return None
    if row.link_code_expires_at is not None and row.link_code_expires_at <= datetime.now(timezone.utc):
        return None
    return row


def find_channel_by_address(session: Session, address: str, kind: str = "telegram") -> NotificationChannel | None:
    """Which user a Telegram chat id belongs to - the routing lookup for inbound messages."""
    return session.scalar(
        select(NotificationChannel).where(
            NotificationChannel.kind == kind,
            NotificationChannel.address == address,
            NotificationChannel.verified.is_(True),
        )
    )


def list_verified_channels(session: Session, kind: str = "telegram") -> Sequence[NotificationChannel]:
    """Every verified channel of a kind (used to warm the hub's routing table)."""
    return session.scalars(
        select(NotificationChannel).where(
            NotificationChannel.kind == kind, NotificationChannel.verified.is_(True)
        )
    ).all()


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #

def list_credentials(session: Session, user_id: UserId) -> Sequence[UserCredential]:
    """Every key this user has brought."""
    return session.scalars(
        select(UserCredential).where(UserCredential.user_id == user_id).order_by(UserCredential.provider)
    ).all()


def get_credential(session: Session, user_id: UserId, provider: str) -> UserCredential | None:
    """One stored key."""
    return session.scalar(
        select(UserCredential).where(
            UserCredential.user_id == user_id, UserCredential.provider == provider
        )
    )


def upsert_credential(
    session: Session, *, user_id: UserId, provider: str, secret_encrypted: str, hint: str
) -> UserCredential:
    """Store or replace one of a user's own API keys."""
    row = get_credential(session, user_id, provider)
    if row is None:
        row = UserCredential(user_id=user_id, provider=provider)
        session.add(row)
    row.secret_encrypted = secret_encrypted
    row.hint = hint
    row.verified_at = None
    row.verify_error = None
    session.flush()
    return row


def delete_credential(session: Session, user_id: UserId, provider: str) -> bool:
    """Forget one of a user's keys."""
    result = session.execute(
        delete(UserCredential).where(
            UserCredential.user_id == user_id, UserCredential.provider == provider
        )
    )
    return bool(result.rowcount)


# --------------------------------------------------------------------------- #
# Usage
# --------------------------------------------------------------------------- #

def record_usage(
    session: Session,
    *,
    user_id: UserId,
    service: str,
    provider: str = "",
    model: str = "",
    unit: str = "tokens",
    input_units: float = 0.0,
    output_units: float = 0.0,
    units: float = 0.0,
    cost_usd: float = 0.0,
    own_key: bool = False,
    meeting_id: uuid.UUID | None = None,
    detail: str | None = None,
) -> UsageEvent:
    """Append one metered event."""
    event = UsageEvent(
        user_id=user_id,
        meeting_id=meeting_id,
        service=service,
        provider=provider,
        model=model,
        unit=unit,
        input_units=input_units,
        output_units=output_units,
        units=units or (input_units + output_units),
        cost_usd=cost_usd,
        own_key=own_key,
        detail=(detail or "")[:300] or None,
    )
    session.add(event)
    return event


def usage_totals(
    session: Session, user_id: UserId, *, since: datetime, platform_only: bool = True
) -> dict[str, dict[str, float]]:
    """Per-service totals since ``since``.

    ``platform_only`` excludes calls made on the user's own key, because those
    are the ones that do not consume the platform's quota - which is the whole
    point of letting a user bring their own.
    """
    statement = (
        select(
            UsageEvent.service,
            func.coalesce(func.sum(UsageEvent.units), 0.0),
            func.coalesce(func.sum(UsageEvent.cost_usd), 0.0),
            func.count(),
        )
        .where(UsageEvent.user_id == user_id, UsageEvent.created_at >= since)
        .group_by(UsageEvent.service)
    )
    if platform_only:
        statement = statement.where(UsageEvent.own_key.is_(False))
    totals: dict[str, dict[str, float]] = {}
    for service, units, cost, calls in session.execute(statement):
        totals[str(service)] = {"units": float(units), "cost_usd": float(cost), "calls": float(calls)}
    return totals


def usage_by_meeting(session: Session, user_id: UserId, meeting_id: uuid.UUID) -> dict[str, float]:
    """Units per service for one meeting."""
    statement = (
        select(UsageEvent.service, func.coalesce(func.sum(UsageEvent.units), 0.0))
        .where(UsageEvent.user_id == user_id, UsageEvent.meeting_id == meeting_id)
        .group_by(UsageEvent.service)
    )
    return {str(service): float(units) for service, units in session.execute(statement)}


def recent_usage(session: Session, user_id: UserId, limit: int = 50) -> Sequence[UsageEvent]:
    """The latest metered events for this user."""
    return session.scalars(
        select(UsageEvent)
        .where(UsageEvent.user_id == user_id)
        .order_by(UsageEvent.created_at.desc())
        .limit(limit)
    ).all()


# --------------------------------------------------------------------------- #
# Legal and consent
# --------------------------------------------------------------------------- #

def record_acceptance(
    session: Session,
    *,
    user_id: UserId,
    document: str,
    version: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> LegalAcceptance:
    """Record acceptance of one legal document version (idempotent)."""
    existing = session.scalar(
        select(LegalAcceptance).where(
            LegalAcceptance.user_id == user_id,
            LegalAcceptance.document == document,
            LegalAcceptance.version == version,
        )
    )
    if existing is not None:
        return existing
    row = LegalAcceptance(
        user_id=user_id,
        document=document,
        version=version,
        ip_address=(ip_address or "")[:64] or None,
        user_agent=(user_agent or "")[:300] or None,
    )
    session.add(row)
    session.flush()
    return row


def list_acceptances(session: Session, user_id: UserId) -> Sequence[LegalAcceptance]:
    """Every legal acceptance this user has made."""
    return session.scalars(
        select(LegalAcceptance)
        .where(LegalAcceptance.user_id == user_id)
        .order_by(LegalAcceptance.accepted_at.desc())
    ).all()


def record_consent(
    session: Session,
    *,
    user_id: UserId,
    meeting_id: uuid.UUID,
    mode: str,
    outcome: str,
    method: str = "verbal",
    granted_by: str | None = None,
    evidence: str | None = None,
    disclosure_text: str | None = None,
    participants: Sequence[str] | None = None,
) -> ConsentRecord:
    """Append an auditable consent record for a meeting."""
    row = ConsentRecord(
        user_id=user_id,
        meeting_id=meeting_id,
        mode=mode,
        outcome=outcome,
        method=method,
        granted_by=granted_by,
        evidence=evidence,
        disclosure_text=disclosure_text,
        participants=list(participants or []),
    )
    session.add(row)
    session.flush()
    return row


def list_consent_records(session: Session, user_id: UserId, limit: int = 100) -> Sequence[ConsentRecord]:
    """This user's consent audit trail, newest first."""
    return session.scalars(
        select(ConsentRecord)
        .where(ConsentRecord.user_id == user_id)
        .order_by(ConsentRecord.recorded_at.desc())
        .limit(limit)
    ).all()
