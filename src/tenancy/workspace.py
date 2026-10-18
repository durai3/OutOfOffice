"""
One tenant's entire runtime, and the manager that opens and closes them.

This is where multi-tenancy actually lives. A :class:`Workspace` holds
everything that used to be a process-wide singleton, one copy per account:

    profile store        this account's principals only
    knowledge index      this account's own Chroma collection and documents folder
    dossier store        this account's principal
    meeting memory       this account's live meetings
    commitments          this account's ledgers, mirrored to this account's rows
    escalation bridge    this account's Telegram chat, and no other
    language model       built on this account's key when they brought one
    usage meter          this account's consumption
    live feed            this account's console, so one user's transcript is
                         never delivered to another user's WebSocket
    delegate manager     this account's meetings

Nothing mutable is shared between two workspaces. The only things that are -
the embedding model, the Chroma client, the Telegram connection - are
stateless with respect to tenants: each is addressed with a key (a collection
name, a chat id) that the workspace holds and never hands out.

:class:`WorkspaceManager` opens them lazily on the first authenticated request
and evicts idle ones, because opening an index and a model chain for every
account that has ever signed up, at boot, does not scale and is not needed:
an account with no meeting running and nobody looking at its console costs
nothing to close and a fraction of a second to reopen.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from src.api.events import LiveFeedHub
from src.auth.security import AuthError, decrypt_secret
from src.brain.knowledge import KnowledgeSource, ProfileKnowledge
from src.compliance.consent import mode_from_value
from src.db import repo
from src.db.base import session_scope
from src.db.models import ConsentMode
from src.db.stores import (
    DbCommitmentSink,
    DbDossierStore,
    DbHistoryStore,
    DbProfileStore,
)
from src.dispatch.container import ContainerDispatcher
from src.dispatch.inprocess import InProcessDispatcher
from src.escalation.bridge import EscalationBridge, NullEscalationBridge
from src.escalation.hub import TelegramHub
from src.escalation.telegram_bot import TelegramEscalationBridge, TelegramSettings
from src.gateway.components import SessionComponents
from src.knowledge.config import KnowledgeSettings
from src.knowledge.retriever import DocumentKnowledge, build_knowledge_source
from src.knowledge.store import KnowledgeStore
from src.llm import build_llm
from src.memory.commitments import CommitmentRegistry
from src.memory.meeting_state import MeetingStateRegistry, memory_window_from_env
from src.tenancy.credentials import ResolvedCredentials, resolve
from src.tenancy.paths import TenantPaths
from src.usage.meter import PLANS, Quota, UsageMeter, plan_for_context

logger = logging.getLogger("deleg8.tenancy.workspace")

IDLE_EVICTION_SECONDS: float = 900.0
"""How long a workspace with no meeting and no console may sit before it is closed."""


def callback_base_url() -> str | None:
    """``DELEG8_CALLBACK_URL`` - where worker containers reach this control plane.

    On Docker Desktop the host is not ``localhost`` from inside a container, so
    the default points at ``host.docker.internal``; on Linux the operator sets
    this explicitly to the service address.
    """
    explicit = os.getenv("DELEG8_CALLBACK_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    port = os.getenv("DELEG8_PORT", "8000").strip() or "8000"
    return f"http://host.docker.internal:{port}"


class Workspace:
    """Everything one account's delegate runs on."""

    def __init__(
        self,
        *,
        user_id: uuid.UUID,
        principal_id: uuid.UUID | None,
        principal_slug: str,
        principal_name: str,
        hub: TelegramHub,
        manager: WorkspaceManager | None = None,
    ) -> None:
        """Bind the identity. Nothing heavy is loaded until :meth:`open`."""
        self.user_id = user_id
        self.principal_id = principal_id
        self.principal_slug = principal_slug
        self.principal_name = principal_name
        self.manager = manager
        self.paths = TenantPaths.for_user(user_id).ensure()
        self.live_feed = LiveFeedHub()
        self.profile_store = DbProfileStore(user_id)
        self.history = DbHistoryStore(user_id)
        self.dossiers = DbDossierStore(user_id)
        self.commitment_sink = DbCommitmentSink(user_id)
        self.commitments = CommitmentRegistry(sink=self.commitment_sink.persist)
        self.memory = MeetingStateRegistry(window=memory_window_from_env())
        self.credentials: ResolvedCredentials = ResolvedCredentials(user_id=user_id)
        self.plan: Quota = PLANS["beta"]
        self.default_consent_mode: ConsentMode = ConsentMode.NOTIFY_ONLY
        self.knowledge_settings: KnowledgeSettings | None = None
        self.knowledge_store: KnowledgeStore | None = None
        self.documents: DocumentKnowledge | None = None
        self.bridge: EscalationBridge = NullEscalationBridge("no notification channel linked yet")
        self.components: SessionComponents | None = None  # type: ignore[assignment]
        self.delegate = None  # type: ignore[assignment]
        self.container_dispatcher = ContainerDispatcher()
        self.inprocess_dispatcher: InProcessDispatcher | None = None
        self.callback_url = callback_base_url()
        self.opened_at: datetime | None = None
        self.last_used_at: datetime = datetime.now(timezone.utc)
        self._hub = hub
        self._lock = asyncio.Lock()
        self._closed = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    @property
    def is_open(self) -> bool:
        """True once the components are assembled."""
        return self.components is not None

    @property
    def fully_byo(self) -> bool:
        """True when nothing this tenant runs is on the platform's keys."""
        return not (self.credentials.uses_platform_llm or self.credentials.uses_platform_voice)

    def touch(self) -> None:
        """Mark the workspace as recently used, deferring eviction."""
        self.last_used_at = datetime.now(timezone.utc)

    async def open(self) -> Workspace:
        """Assemble everything. Idempotent and safe to await concurrently."""
        async with self._lock:
            if self.components is not None:
                self.touch()
                return self
            self._load_account()
            knowledge, store, documents = await self._open_knowledge()
            self.knowledge_store, self.documents = store, documents
            self.bridge = self._build_bridge()

            llm = build_llm(
                anthropic_key=self.credentials.value("anthropic"),
                gemini_key=self.credentials.value("gemini"),
            )
            self.meter = UsageMeter(
                self.user_id,
                own_llm_key=self.credentials.owns(self.credentials.llm_provider or "gemini"),
                own_stt_key=self.credentials.owns("deepgram"),
                own_tts_key=self.credentials.owns("cartesia"),
            )
            if llm is not None:
                llm.on_usage = self.meter.record_llm

            self.components = SessionComponents.assemble(
                bridge=self.bridge,
                knowledge=knowledge,
                knowledge_store=store,
                documents=documents,
                store=self.profile_store,
                archive=self.history,
                memory=self.memory,
                llm=llm,
                dossiers=self.dossiers,
                commitments=self.commitments,
                user_id=self.user_id,
            )

            from src.api.delegate import DelegateManager

            self.delegate = DelegateManager(self, self.live_feed)
            self.inprocess_dispatcher = InProcessDispatcher(
                self.components, emit=self.delegate.emit
            )
            self._register_commands()
            self.opened_at = datetime.now(timezone.utc)
            self.touch()
            logger.info(
                "workspace ready for %s (principal %s): %s; %s",
                self.user_id, self.principal_slug, self.bridge.describe(), self.credentials.describe(),
            )
            return self

    def _load_account(self, session: Session | None = None) -> None:
        """Refresh identity, credentials, plan and preferences from the database.

        ``session`` is the caller's own when there is one. That matters: a route
        that stores a key and then asks the workspace to rebuild its model runs
        both inside one request, and a fresh session would read the state from
        *before* that write commits - which is how a deleted key kept being used
        and a stored one did not take effect until the next request.
        """
        if session is not None:
            self._read_account(session)
            return
        with session_scope() as own:
            self._read_account(own)

    def _read_account(self, session: Session) -> None:
        """Populate identity, credentials, plan and preferences from one session."""
        user = repo.get_user(session, self.user_id)
        principal = repo.primary_principal(session, self.user_id)
        if principal is not None:
            self.principal_id = principal.id
            self.principal_slug = principal.slug
            self.principal_name = principal.display_name
        settings = dict(user.settings or {}) if user is not None else {}
        self.credentials = resolve(session, self.user_id)
        org_settings = repo.org_settings_for_user(session, self.user_id)
        self.plan = PLANS["unlimited"] if self.fully_byo else plan_for_context(settings, org_settings)
        self.default_consent_mode = mode_from_value(settings.get("consent_mode"))

    async def _open_knowledge(self) -> tuple[KnowledgeSource, KnowledgeStore | None, DocumentKnowledge | None]:
        """Open this tenant's own collection; fall back to profile facts if it cannot be opened."""
        settings = KnowledgeSettings.for_tenant(
            self.paths.documents, self.paths.collection, self.paths.chroma
        )
        self.knowledge_settings = settings
        store = KnowledgeStore(settings)
        try:
            await store.open_async()
        except Exception as exc:  # noqa: BLE001 - a broken index must not block sign-in
            logger.error(
                "document index unavailable for %s (%s); answers fall back to profile facts",
                self.user_id, exc,
            )
            store.close()
            return ProfileKnowledge(), None, None
        knowledge, documents = build_knowledge_source(store, settings)
        return knowledge, store, documents

    def _build_bridge(self, session: Session | None = None) -> EscalationBridge:
        """This tenant's escalation channel: their own bot, the platform bot, or none."""
        if session is None:
            with session_scope() as own:
                return self._read_bridge(own)
        return self._read_bridge(session)

    def _read_bridge(self, session: Session) -> EscalationBridge:
        """Build the bridge from one session's view of the channel row."""
        channel = repo.get_channel(session, self.user_id, "telegram")
        if channel is None or not channel.verified or not channel.address:
            return NullEscalationBridge(
                "no Telegram chat linked yet - link one in Settings to get approvals on your phone"
            )
        timeout = channel.timeout_seconds or _default_timeout()
        if channel.bot_token_encrypted:
            try:
                token = decrypt_secret(channel.bot_token_encrypted)
            except AuthError as exc:
                logger.error("could not decrypt %s's bot token: %s", self.user_id, exc)
                return NullEscalationBridge("your bot token could not be decrypted; re-enter it in Settings")
            return TelegramEscalationBridge(
                TelegramSettings(bot_token=token, chat_id=int(channel.address), timeout_seconds=timeout)
            )
        if not self._hub.started:
            return NullEscalationBridge("the platform Telegram bot is not running on this server")
        return self._hub.bridge_for(self.user_id, int(channel.address), timeout_seconds=timeout)

    def _register_commands(self) -> None:
        """Bind this tenant's ``/status``, ``/leave``, ``/conclude`` and ``/consent``."""
        hub = self._hub
        if not hub.started:
            return

        async def status_command(_args: str) -> str:
            statuses = self.delegate.statuses() if self.delegate is not None else []
            if not statuses:
                return "Your delegate is idle. Dispatch it from the web console."
            lines = []
            for status in statuses:
                counters = status.counters
                label = (status.platform or "meet").replace("_", " ").title()
                consent = "" if status.retaining else f" [{status.consent_state}: not retaining]"
                lines.append(
                    f"{label} {status.meeting_code}: {status.phase}, "
                    f"{status.activity.value.lower().replace('_', ' ')}{consent}; "
                    f"topic {status.active_topic or '-'}; {counters.utterances} heard, "
                    f"{counters.auto_replies} answered, {counters.escalations} escalated."
                )
            return "\n\n".join(lines)

        async def leave_command(args: str) -> str:
            if self.delegate is None or not self.delegate.is_active:
                return "No delegate is attending a meeting."
            status = await self.delegate.abort("/leave from Telegram", args.strip() or None)
            return f"Leaving {status.meeting_code} and sending the briefing."

        async def conclude_command(args: str) -> str:
            components = self.components
            if components is None:
                return "Your workspace is not running on this server."
            meeting_id = args.strip()
            if meeting_id:
                state = components.memory.get(meeting_id)
                if state is None:
                    active = ", ".join(s.meeting_id for s in components.memory.active()) or "none"
                    return f"No active meeting named '{meeting_id}'. Active: {active}."
                report = await components.reporter.conclude(state, registry=components.memory)
                delivered = "sent" if report.receipt.delivered else "not delivered"
                return f"Concluded {meeting_id}: briefing {delivered} ({report.summariser})."
            if not components.memory.active():
                return "No active meetings to conclude."
            reports = await components.reporter.conclude_all(components.memory)
            return "Concluded " + ", ".join(r.meeting_id for r in reports) + "."

        async def consent_command(args: str) -> str:
            return self.resolve_consent_from_chat(args)

        async def commitments_command(_args: str) -> str:
            rows = self.commitment_sink.open_rows(limit=20)
            if not rows:
                return "No open action items."
            lines = [
                f"• {row.description}" + (f" — due {row.due_text or row.due_iso}" if (row.due_text or row.due_iso) else "")
                for row in rows
            ]
            return "Open action items:\n" + "\n".join(lines)

        hub.register_commands(
            self.user_id,
            {
                "status": status_command,
                "leave": leave_command,
                "conclude": conclude_command,
                "consent": consent_command,
                "commitments": commitments_command,
            },
        )

    def resolve_consent_from_chat(self, args: str) -> str:
        """``/consent yes|no [meeting]`` - the principal answering for the room."""
        if self.delegate is None:
            return "Your workspace is not running on this server."
        parts = args.strip().split()
        if not parts or parts[0].lower() not in {"yes", "no", "grant", "deny"}:
            return "Use /consent yes or /consent no (optionally followed by the meeting id)."
        granted = parts[0].lower() in {"yes", "grant"}
        meeting_key = parts[1] if len(parts) > 1 else None
        running = self.delegate.running()
        if not running:
            return "No meeting is waiting on consent."
        target = next(
            (m for m in running if meeting_key is None or m.meeting_key == meeting_key), None
        )
        if target is None:
            return f"No running meeting named {meeting_key!r}."
        session_obj = (
            self.inprocess_dispatcher.session_for(target.meeting_key)
            if self.inprocess_dispatcher is not None
            else None
        )
        if session_obj is None:
            return (
                f"{target.meeting_key} is running in a container, so consent has to come from the room. "
                "Recall it with /leave if that is not possible."
            )
        gate = session_obj.consent
        if granted:
            gate.grant_externally(by=self.principal_name)
            return f"Recorded your consent for {target.meeting_key}; the delegate will take notes."
        gate.deny_externally(by=self.principal_name)
        return f"Recorded a refusal for {target.meeting_key}; the delegate will leave without retaining anything."

    async def ensure_dossier(self) -> str:
        """Recompile this tenant's dossier if their documents have changed.

        A no-op when nothing moved, and never overwrites a dossier the principal
        has edited by hand - their correction outranks the compiler.
        """
        if self.components is None or self.knowledge_settings is None:
            return "dossier: workspace not open"
        from src.knowledge.dossier import DossierBuilder

        builder = DossierBuilder(llm=getattr(self.components, "llm", None), store=self.dossiers)
        dossier = await builder.build_quietly(
            user_id=self.principal_slug,
            principal=self.principal_name,
            settings=self.knowledge_settings,
        )
        return dossier.describe()

    def new_meter(self, meeting_id: uuid.UUID) -> UsageMeter:
        """A meter for one meeting, attributed to this tenant."""
        meter = UsageMeter(
            self.user_id,
            meeting_id=meeting_id,
            own_llm_key=self.credentials.owns(self.credentials.llm_provider or "gemini"),
            own_stt_key=self.credentials.owns("deepgram"),
            own_tts_key=self.credentials.owns("cartesia"),
        )
        llm = getattr(self.components, "llm", None)
        if llm is not None:
            # Rebinding is intentional: usage is attributed to whichever meeting
            # is currently running, and the workspace-level meter picks up
            # anything outside a meeting (a dossier rebuild, a console query).
            llm.on_usage = meter.record_llm
        return meter

    def invalidate_profile(self, session: Session | None = None) -> None:
        """Force the next policy read to come from the database."""
        self.profile_store.invalidate()
        self._load_account(session)

    async def refresh_channel(self, session: Session | None = None) -> None:
        """Rebuild the escalation bridge after a channel change."""
        self.bridge = self._build_bridge(session)
        if self.components is not None:
            self.components.bridge = self.bridge
            self.components.turns.bridge = self.bridge
            self.components.reporter.bridge = self.bridge
        self._register_commands()
        logger.info("escalation channel for %s is now: %s", self.user_id, self.bridge.describe())

    async def refresh_credentials(self, session: Session | None = None) -> None:
        """Rebuild the language model after the tenant's keys changed."""
        self._load_account(session)
        if self.components is None:
            return
        llm = build_llm(
            anthropic_key=self.credentials.value("anthropic"),
            gemini_key=self.credentials.value("gemini"),
        )
        if llm is not None:
            llm.on_usage = self.meter.record_llm
        self.components.llm = llm
        from src.brain.planner import build_planner

        planner = build_planner(llm)
        self.components.planner = planner
        self.components.turns.components.planner = planner
        self.components.reporter.rebuild_summariser(llm)
        logger.info("credentials refreshed for %s: %s", self.user_id, self.credentials.describe())

    def status(self) -> dict[str, object]:
        """A line-item summary for the health page and the admin view."""
        return {
            "user_id": str(self.user_id),
            "principal": self.principal_slug,
            "open": self.is_open,
            "collection": self.paths.collection,
            "escalation": self.bridge.describe(),
            "credentials": self.credentials.describe(),
            "plan": self.plan.name,
            "active_meetings": self.delegate.active_count if self.delegate is not None else 0,
            "live_subscribers": self.live_feed.subscribers,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "last_used_at": self.last_used_at.isoformat(),
        }

    def evictable(self, *, idle_seconds: float = IDLE_EVICTION_SECONDS) -> bool:
        """True when nothing would be lost by closing this workspace."""
        if self.delegate is not None and self.delegate.is_active:
            return False
        if self.live_feed.subscribers > 0:
            return False
        if self.memory.active():
            return False
        return (datetime.now(timezone.utc) - self.last_used_at).total_seconds() > idle_seconds

    async def close(self) -> None:
        """Stop this tenant's meetings and release its index."""
        if self._closed:
            return
        self._closed = True
        if self.delegate is not None:
            await self.delegate.shutdown()
        if isinstance(self.bridge, TelegramEscalationBridge):
            await self.bridge.stop()
        self._hub.unregister_commands(self.user_id)
        meter = getattr(self, "meter", None)
        if meter is not None:
            meter.flush()
        if self.knowledge_store is not None:
            self.knowledge_store.close()
        logger.info("workspace closed for %s", self.user_id)

    async def purge(self) -> None:
        """Erase everything this tenant owns outside the database, then close."""
        await self.close()
        if self.knowledge_store is not None:
            self.knowledge_store.drop()
        else:
            KnowledgeStore(
                KnowledgeSettings.for_tenant(
                    self.paths.documents, self.paths.collection, self.paths.chroma
                )
            ).drop()
        removed = self.paths.purge()
        logger.warning("purged tenant data for %s (%d bytes of documents)", self.user_id, removed)


def _default_timeout() -> float:
    """``ESCALATION_TIMEOUT_SECONDS`` (default 30)."""
    raw = os.getenv("ESCALATION_TIMEOUT_SECONDS", "").strip()
    try:
        return float(raw) if raw else 30.0
    except ValueError:
        return 30.0


class WorkspaceManager:
    """Opens, caches and evicts tenant workspaces."""

    def __init__(self, hub: TelegramHub) -> None:
        """Bind to the shared Telegram hub."""
        self._hub = hub
        self._workspaces: dict[uuid.UUID, Workspace] = {}
        self._lock = asyncio.Lock()
        self._sweeper: asyncio.Task[None] | None = None

    def get(self, user_id: uuid.UUID) -> Workspace | None:
        """An already-open workspace, without opening one."""
        workspace = self._workspaces.get(user_id)
        if workspace is not None:
            workspace.touch()
        return workspace

    async def open(self, user_id: uuid.UUID) -> Workspace:
        """The tenant's workspace, opening it on first use."""
        existing = self._workspaces.get(user_id)
        if existing is not None and not existing._closed:
            existing.touch()
            return await existing.open()
        async with self._lock:
            existing = self._workspaces.get(user_id)
            if existing is not None and not existing._closed:
                return await existing.open()
            with session_scope() as session:
                principal = repo.primary_principal(session, user_id)
                user = repo.get_user(session, user_id)
                slug = principal.slug if principal is not None else str(user_id)
                name = (
                    principal.display_name
                    if principal is not None
                    else (user.display_name if user is not None else str(user_id))
                )
                principal_id = principal.id if principal is not None else None
            workspace = Workspace(
                user_id=user_id,
                principal_id=principal_id,
                principal_slug=slug,
                principal_name=name,
                hub=self._hub,
                manager=self,
            )
            self._workspaces[user_id] = workspace
        return await workspace.open()

    def opened(self) -> list[Workspace]:
        """Every workspace currently held."""
        return list(self._workspaces.values())

    def total_active_meetings(self) -> int:
        """Meetings running across every tenant on this server."""
        return sum(
            w.delegate.active_count for w in self._workspaces.values() if w.delegate is not None
        )

    def find_by_meeting(self, meeting_key: str) -> tuple[Workspace, object] | None:
        """Which tenant owns a running meeting - used by the worker callback route."""
        for workspace in self._workspaces.values():
            if workspace.delegate is None:
                continue
            meeting = workspace.delegate.get(meeting_key)
            if meeting is not None:
                return workspace, meeting
        return None

    def describe(self) -> str:
        """Status line for the health page."""
        return (
            f"workspaces: {len(self._workspaces)} open, "
            f"{self.total_active_meetings()} meeting(s) running"
        )

    async def close(self, user_id: uuid.UUID) -> None:
        """Close one tenant's workspace."""
        workspace = self._workspaces.pop(user_id, None)
        if workspace is not None:
            await workspace.close()

    async def purge(self, user_id: uuid.UUID) -> None:
        """Erase one tenant's files and vector collection, then forget them."""
        workspace = self._workspaces.pop(user_id, None)
        if workspace is None:
            workspace = Workspace(
                user_id=user_id,
                principal_id=None,
                principal_slug=str(user_id),
                principal_name=str(user_id),
                hub=self._hub,
                manager=self,
            )
        await workspace.purge()

    def start_sweeper(self) -> None:
        """Begin evicting idle workspaces in the background."""
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(self._sweep(), name="deleg8-workspace-sweeper")

    async def _sweep(self) -> None:
        while True:
            try:
                await asyncio.sleep(120.0)
                for user_id, workspace in list(self._workspaces.items()):
                    if workspace.evictable():
                        logger.info("evicting idle workspace for %s", user_id)
                        self._workspaces.pop(user_id, None)
                        await workspace.close()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the sweeper must not die on one bad workspace
                logger.exception("workspace sweep failed")

    async def shutdown(self) -> None:
        """Close every workspace before the server exits."""
        if self._sweeper is not None:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down regardless
                pass
            self._sweeper = None
        for workspace in list(self._workspaces.values()):
            await workspace.close()
        self._workspaces.clear()
