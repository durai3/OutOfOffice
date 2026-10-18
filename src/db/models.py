"""
The DELEG8 schema: one row per thing that used to be a file or a dict.

Everything hangs off :class:`User`. A user owns one :class:`Principal` (the
person their delegate represents), that principal owns the Representation
Policy (:class:`PolicyRule`, :class:`KnowledgeEntry`, :class:`Dossier`), and
the user owns every meeting, brief, commitment, document, credential and
usage event produced on their behalf. There is no row in this schema that
cannot be attributed to exactly one user, which is what makes per-tenant
isolation checkable rather than hoped for: every repository function in
:mod:`src.db.repo` takes a ``user_id`` and filters on it.

Deletes cascade from the user down, so "delete my account" is one statement
and actually removes the data - a promise the privacy policy makes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def utcnow() -> datetime:
    """Timezone-aware UTC now, used for every timestamp default."""
    return datetime.now(timezone.utc)


class TimestampMixin:
    """``created_at`` / ``updated_at`` maintained by the ORM."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #

class UserStatus(str, Enum):
    """Whether an account may sign in and dispatch a delegate."""

    ACTIVE = "active"
    SUSPENDED = "suspended"


class UserRole(str, Enum):
    """Coarse authorisation. ``admin`` may read platform-wide usage."""

    USER = "user"
    ADMIN = "admin"


class AuthProvider(str, Enum):
    """How an account was FIRST created - not how it currently signs in.

    Immutable after the row is inserted. A password (``local``) account that
    later links a Google identity (see :class:`OAuthIdentity` and the
    auto-link rule in ``src.auth.service.provision_or_link_oauth_account``)
    stays ``local`` forever: this field answers "how did we acquire this
    account" for tracking/analytics, not "which methods can this account
    currently sign in with" - that question is answered by whether
    ``password_hash`` is set and/or there are rows in ``oauth_identities``.
    """

    LOCAL = "local"
    GOOGLE = "google"
    MICROSOFT = "microsoft"


class User(Base, TimestampMixin):
    """One account."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """``None`` for an account that has only ever signed in via SSO."""
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=UserStatus.ACTIVE.value, nullable=False)
    role: Mapped[str] = mapped_column(String(20), default=UserRole.USER.value, nullable=False)
    auth_provider: Mapped[str] = mapped_column(String(20), default=AuthProvider.LOCAL.value, nullable=False)
    """First-signup method; immutable. See :class:`AuthProvider`."""
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    onboarding: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    """Which onboarding steps are done; see :mod:`src.api.onboarding`."""
    settings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    """Per-user preferences: consent mode, escalation timeout, meeting limits."""

    principals: Mapped[list[Principal]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    channels: Mapped[list[NotificationChannel]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    credentials: Mapped[list[UserCredential]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def is_active(self) -> bool:
        """True when the account may sign in."""
        return self.status == UserStatus.ACTIVE.value

    @property
    def primary_principal(self) -> Principal | None:
        """The principal this user's delegate represents."""
        for principal in self.principals:
            if principal.is_primary:
                return principal
        return self.principals[0] if self.principals else None


class AuthSession(Base):
    """A refresh token, so sign-out and "revoke everywhere" actually revoke."""

    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)


class OAuthIdentity(Base):
    """One provider identity (Google or Microsoft) linked to a user.

    This is the lookup key for "does this Google/Microsoft account already
    have a DELEG8 account" (:func:`src.db.repo.get_oauth_identity`), and what
    lets one account accumulate a password plus multiple SSO identities over
    time. ``(provider, provider_subject)`` is globally unique - a given
    Google account can only ever point at one DELEG8 user.
    """

    __tablename__ = "oauth_identities"
    __table_args__ = (UniqueConstraint("provider", "provider_subject", name="uq_oauth_identity_subject"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    """The provider's stable, opaque subject id (Google's ``sub``, Microsoft's ``id``) - never the email."""
    provider_email: Mapped[str] = mapped_column(String(320), nullable=False)
    """The email the provider reported at link time. Tracking only - never re-trusted for lookups; ``provider_subject`` is."""
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# --------------------------------------------------------------------------- #
# Organizations - billing/seat/admin sits here, above the per-user tenancy.
# Nothing below User changes: every org member still owns their own
# Principal, Workspace, meetings and knowledge exactly as a solo user does.
# --------------------------------------------------------------------------- #

class OrganizationRole(str, Enum):
    """Org-scoped role. Deliberately distinct from :class:`UserRole` (the platform-wide admin flag)."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class Organization(Base, TimestampMixin):
    """A paying entity with seats. A user belongs to at most one, in this pass."""

    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    seats_limit: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    settings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    """Where ``plan`` lives for an org, mirroring :attr:`User.settings`'s convention."""


class OrganizationMember(Base, TimestampMixin):
    """One user's membership in one organization."""

    __tablename__ = "organization_members"
    __table_args__ = (UniqueConstraint("org_id", "user_id", name="uq_org_member"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), default=OrganizationRole.MEMBER.value, nullable=False)


class OrganizationInvite(Base, TimestampMixin):
    """An invitation to join an organization, redeemed via a link token.

    ``token_hash`` is a SHA-256 digest, the same pattern :class:`AuthSession`
    uses for refresh tokens - the raw token only ever exists in memory long
    enough to build the shareable link; it is never stored.
    """

    __tablename__ = "organization_invites"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), default=OrganizationRole.MEMBER.value, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_pending(self) -> bool:
        """True while the invite can still be redeemed."""
        return self.accepted_at is None and self.revoked_at is None and utcnow() < self.expires_at


# --------------------------------------------------------------------------- #
# The represented person and their Representation Policy
# --------------------------------------------------------------------------- #

class Principal(Base, TimestampMixin):
    """The person a delegate speaks for. ``slug`` is what the brain calls ``user_id``."""

    __tablename__ = "principals"
    __table_args__ = (UniqueConstraint("slug", name="uq_principal_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    """Stable, globally unique id used everywhere the brain says ``user_id``."""
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    aliases: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user: Mapped[User] = relationship(back_populates="principals")
    policy_rules: Mapped[list[PolicyRule]] = relationship(
        back_populates="principal", cascade="all, delete-orphan", lazy="selectin",
        order_by="PolicyRule.position",
    )
    knowledge_entries: Mapped[list[KnowledgeEntry]] = relationship(
        back_populates="principal", cascade="all, delete-orphan", lazy="selectin",
        order_by="KnowledgeEntry.position",
    )
    dossier: Mapped[Dossier | None] = relationship(
        back_populates="principal", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )


class PolicyRule(Base):
    """One line of a Representation Policy: a topic, its tier, and how to spot it."""

    __tablename__ = "policy_rules"
    __table_args__ = (UniqueConstraint("principal_id", "topic", name="uq_policy_rule_topic"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    topic: Mapped[str] = mapped_column(String(80), nullable=False)
    tier: Mapped[str] = mapped_column(String(10), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    triggers: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    principal: Mapped[Principal] = relationship(back_populates="policy_rules")


class KnowledgeEntry(Base):
    """A grounded fact the delegate may state verbatim for a GREEN topic."""

    __tablename__ = "knowledge_entries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    topic: Mapped[str] = mapped_column(String(80), nullable=False)
    subject: Mapped[str] = mapped_column(String(120), nullable=False)
    keywords: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    principal: Mapped[Principal] = relationship(back_populates="knowledge_entries")


class Dossier(Base):
    """The principal's compiled Markdown dossier plus its build provenance."""

    __tablename__ = "dossiers"

    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), primary_key=True
    )
    markdown: Mapped[str] = mapped_column(Text, default="", nullable=False)
    built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    built_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    sources: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    hand_edited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    principal: Mapped[Principal] = relationship(back_populates="dossier")


# --------------------------------------------------------------------------- #
# Knowledge base inventory (the vectors live in this user's own Chroma collection)
# --------------------------------------------------------------------------- #

class Document(Base, TimestampMixin):
    """One ingested source file, owned by exactly one user."""

    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("user_id", "source", name="uq_document_source"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(400), nullable=False)
    title: Mapped[str] = mapped_column(String(400), default="", nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    characters: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="indexed", nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


# --------------------------------------------------------------------------- #
# Meetings
# --------------------------------------------------------------------------- #

class MeetingStatus(str, Enum):
    """Lifecycle of one dispatched meeting."""

    PENDING = "pending"
    DISPATCHING = "dispatching"
    CONNECTING = "connecting"
    LIVE = "live"
    CONCLUDING = "concluding"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """True once nothing more will happen to this meeting."""
        return self in {MeetingStatus.FINISHED, MeetingStatus.FAILED, MeetingStatus.CANCELLED}


class MeetingRuntime(str, Enum):
    """Where the delegate for this meeting actually runs."""

    IN_PROCESS = "inprocess"
    """An asyncio task inside the API server (development, single machine)."""
    CONTAINER = "container"
    """One ``deleg8-worker`` container per meeting per user (the shipping path)."""


class ConsentMode(str, Enum):
    """How much consent the room must give before anything is retained."""

    NOTIFY_ONLY = "notify_only"
    """Announce the delegate and that it transcribes; do not wait for an answer."""
    ALL_PARTY = "all_party"
    """Announce, then retain nothing until someone in the room affirmatively agrees."""
    SINGLE_PARTY = "single_party"
    """The principal's own consent is the legal basis; still announce."""


class ConsentState(str, Enum):
    """Where the consent handshake for a meeting stands."""

    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    WITHDRAWN = "withdrawn"

    @property
    def may_retain(self) -> bool:
        """True when the transcript may be written to memory, the archive and the briefing."""
        return self in {ConsentState.NOT_REQUIRED, ConsentState.GRANTED}


class Meeting(Base, TimestampMixin):
    """One dispatch of a delegate into one call."""

    __tablename__ = "meetings"
    __table_args__ = (
        UniqueConstraint("user_id", "meeting_key", name="uq_meeting_key"),
        Index("ix_meetings_user_status", "user_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True
    )
    meeting_key: Mapped[str] = mapped_column(String(120), nullable=False)
    """What the brain, memory and briefing call ``meeting_id``."""
    platform: Mapped[str] = mapped_column(String(30), default="google_meet", nullable=False)
    join_url: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=MeetingStatus.PENDING.value, nullable=False)
    runtime: Mapped[str] = mapped_column(String(20), default=MeetingRuntime.IN_PROCESS.value, nullable=False)
    phase: Mapped[str] = mapped_column(String(30), default="IDLE", nullable=False)
    activity: Mapped[str] = mapped_column(String(30), default="IDLE", nullable=False)

    container_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    container_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    job_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    """SHA-256 of the bearer token the worker container uses to call home."""

    speaker_label: Mapped[str] = mapped_column(String(80), default="Meeting", nullable=False)
    half_duplex: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    briefing: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    max_call_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    consent_mode: Mapped[str] = mapped_column(String(20), default=ConsentMode.NOTIFY_ONLY.value, nullable=False)
    consent_state: Mapped[str] = mapped_column(String(20), default=ConsentState.NOT_REQUIRED.value, nullable=False)
    consent_decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    participants: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    commitments: Mapped[list[CommitmentRow]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", lazy="selectin"
    )
    consent_records: Mapped[list[ConsentRecord]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", lazy="selectin"
    )


class MeetingReport(Base):
    """An archived post-meeting briefing. Replaces ``data/history/*.json``."""

    __tablename__ = "meeting_reports"
    __table_args__ = (UniqueConstraint("user_id", "brief_id", name="uq_report_brief_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    meeting_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meetings.id", ondelete="SET NULL"), nullable=True
    )
    brief_id: Mapped[str] = mapped_column(String(120), nullable=False)
    meeting_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    concluded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    executive_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    summariser: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    delivery_channel: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    action_item_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    """The full :class:`~src.memory.post_meeting.PostMeetingReport`, as stored JSON."""
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class CommitmentRow(Base):
    """One thing the room asked the principal to do, persisted beyond the process."""

    __tablename__ = "commitments"
    __table_args__ = (UniqueConstraint("meeting_id", "local_id", name="uq_commitment_local_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    local_id: Mapped[str] = mapped_column(String(20), nullable=False)
    """The in-meeting id (``c1``, ``c2``) the ledger and the Telegram buttons use."""
    description: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    requested_by: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    due_iso: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    due_text: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="probable", nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="planner", nullable=False)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_turn: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_turn: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    meeting: Mapped[Meeting] = relationship(back_populates="commitments")


# --------------------------------------------------------------------------- #
# Escalation routing, credentials, metering, compliance
# --------------------------------------------------------------------------- #

class NotificationChannel(Base, TimestampMixin):
    """Where one user's escalations and briefings go. One row per channel kind."""

    __tablename__ = "notification_channels"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", name="uq_channel_kind"),
        Index("ix_channel_address", "kind", "address"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(30), default="telegram", nullable=False)
    address: Mapped[str | None] = mapped_column(String(120), nullable=True)
    """The Telegram chat id this user's alerts are sent to."""
    bot_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Set only when the user runs their own bot instead of the platform bot."""
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    link_code: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    link_code_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    timeout_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    user: Mapped[User] = relationship(back_populates="channels")


class CredentialProvider(str, Enum):
    """External services a user may bring their own key for."""

    GEMINI = "gemini"
    ANTHROPIC = "anthropic"
    DEEPGRAM = "deepgram"
    CARTESIA = "cartesia"


class UserCredential(Base, TimestampMixin):
    """A user-supplied API key, encrypted at rest."""

    __tablename__ = "user_credentials"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_credential_provider"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    hint: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    """Last four characters, so the console can show which key is stored."""
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verify_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship(back_populates="credentials")


class UsageService(str, Enum):
    """Which metered service a usage event belongs to."""

    LLM = "llm"
    STT = "stt"
    TTS = "tts"
    MEETING = "meeting"


class UsageEvent(Base):
    """One metered call. Rolled up per user per month for quota enforcement."""

    __tablename__ = "usage_events"
    __table_args__ = (Index("ix_usage_user_created", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    meeting_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meetings.id", ondelete="SET NULL"), nullable=True
    )
    service: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    model: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    unit: Mapped[str] = mapped_column(String(20), default="tokens", nullable=False)
    input_units: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    output_units: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    units: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    own_key: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    """True when the call used the user's own credential, so it is not billed to the platform quota."""
    detail: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ConnectorType(str, Enum):
    """Third-party systems a user may connect a briefing or task sync to."""

    SLACK = "slack"
    MS_TEAMS = "ms_teams"
    JIRA = "jira"
    LINEAR = "linear"
    ASANA = "asana"
    GOOGLE_CALENDAR = "google_calendar"
    EMAIL = "email"
    NOTION = "notion"


class Integration(Base, TimestampMixin):
    """One user's connection to a third-party system. One row per connector type."""

    __tablename__ = "integrations"
    __table_args__ = (UniqueConstraint("user_id", "connector_type", name="uq_integration_type"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    connector_type: Mapped[str] = mapped_column(String(30), nullable=False)
    auth_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    """OAuth token, webhook URL, or API key - whichever the connector's auth mode uses. Encrypted at rest."""
    config_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    """JSON config (e.g. channel id, board id), encrypted at rest since it can embed identifiers."""
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship()


class CalendarProvider(str, Enum):
    """Mailbox providers a user may connect for meeting auto-detection."""

    GOOGLE = "google"
    MICROSOFT = "microsoft"


class CalendarAccount(Base, TimestampMixin):
    """One connected calendar mailbox - independent of how the user signed in.

    Deliberately not an :class:`Integration` row: a user may connect several
    mailboxes of the same provider (a work and a personal Google account), and
    each is discovered only once the OAuth token is exchanged and the mailbox's
    own address is known - see :mod:`src.calendar.providers`.
    """

    __tablename__ = "calendar_accounts"
    __table_args__ = (UniqueConstraint("user_id", "provider", "email", name="uq_calendar_account"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    label: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    auth_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    """``refresh_token`` / ``expires_at`` - same shape as :class:`Integration`'s config, encrypted at rest."""
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship()


class CalendarEvent(Base, TimestampMixin):
    """One synced upcoming meeting from a connected calendar account."""

    __tablename__ = "calendar_events"
    __table_args__ = (
        UniqueConstraint("calendar_account_id", "provider_event_id", name="uq_calendar_event"),
        Index("ix_calendar_events_user_start", "user_id", "start_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    calendar_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("calendar_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider_event_id: Mapped[str] = mapped_column(String(300), nullable=False)
    title: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    join_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    """The video-call link extracted from the event, if it has one. Empty when the event cannot be delegated."""
    html_link: Mapped[str] = mapped_column(Text, default="", nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleg8_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    """The user asked in advance - console or Telegram - for the delegate to join this one automatically."""
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """When the T-15-minutes Telegram reminder went out, so it is only ever sent once."""
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    meeting_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("meetings.id", ondelete="SET NULL"), nullable=True)

    account: Mapped[CalendarAccount] = relationship()


class MeetingMetrics(Base):
    """Cached health scores for one meeting, computed from its archived report."""

    __tablename__ = "meeting_metrics"
    __table_args__ = (UniqueConstraint("meeting_id", name="uq_meeting_metrics"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    decision_clarity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    participation_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    agenda_adherence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    action_item_clarity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    health_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    flags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class LegalAcceptance(Base):
    """Proof that a user accepted a specific version of a legal document."""

    __tablename__ = "legal_acceptances"
    __table_args__ = (UniqueConstraint("user_id", "document", "version", name="uq_legal_acceptance"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    document: Mapped[str] = mapped_column(String(40), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)


class ConsentRecord(Base):
    """An auditable record of how recording consent was obtained - or refused."""

    __tablename__ = "consent_records"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    method: Mapped[str] = mapped_column(String(30), default="verbal", nullable=False)
    granted_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The exact utterance (or console action) the outcome was derived from."""
    disclosure_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    participants: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    meeting: Mapped[Meeting] = relationship(back_populates="consent_records")


__all__ = [
    "AuthProvider",
    "AuthSession",
    "CalendarAccount",
    "CalendarEvent",
    "CalendarProvider",
    "CommitmentRow",
    "ConnectorType",
    "ConsentMode",
    "ConsentRecord",
    "ConsentState",
    "CredentialProvider",
    "Document",
    "Dossier",
    "Integration",
    "KnowledgeEntry",
    "LegalAcceptance",
    "Meeting",
    "MeetingMetrics",
    "MeetingReport",
    "MeetingRuntime",
    "MeetingStatus",
    "NotificationChannel",
    "OAuthIdentity",
    "Organization",
    "OrganizationInvite",
    "OrganizationMember",
    "OrganizationRole",
    "PolicyRule",
    "Principal",
    "UsageEvent",
    "UsageService",
    "User",
    "UserCredential",
    "UserRole",
    "UserStatus",
    "utcnow",
]
