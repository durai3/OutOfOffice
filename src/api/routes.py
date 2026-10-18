"""
The ``/api/v1`` surface the web console talks to. Every route is tenant-scoped.

    GET    /api/v1/dashboard                       everything the Overview page shows
    GET    /api/v1/delegate                        this account's delegate (one meeting)
    GET    /api/v1/delegates                       every meeting this account has running
    POST   /api/v1/meetings/join                   dispatch a delegate (202)
    POST   /api/v1/meetings/abort                  recall: cut speech, leave, brief
    WS     /api/v1/meetings/live-feed              real-time events for this account only
    GET    /api/v1/meetings                        live meetings in memory
    GET    /api/v1/meetings/recent                 recent meetings from the database
    GET    /api/v1/meetings/{meeting_id}           one live meeting's memory
    POST   /api/v1/meetings/{meeting_id}/conclude  brief and close a meeting
    POST   /api/v1/meetings/{meeting_id}/consent   grant or refuse recording consent
    GET    /api/v1/consent/records                 the consent audit trail
    GET/PUT /api/v1/profile                        the Representation Policy
    POST   /api/v1/evaluate                        route one snippet (decision only)
    POST   /api/v1/process-turn                    route one snippet with escalation
    GET    /api/v1/knowledge                       index health + this account's documents
    GET    /api/v1/knowledge/chunks?source=        one document's stored chunks
    POST   /api/v1/knowledge/query                 search this account's index
    POST   /api/v1/knowledge/upload                .md / .txt / .pdf -> saved -> ingested
    POST   /api/v1/knowledge/ingest                re-ingest this account's folder
    DELETE /api/v1/knowledge/documents/{source}    drop a document
    GET    /api/v1/history                         archived briefs
    GET    /api/v1/history/{brief_id}              one full archived report
    GET/PUT /api/v1/dossier                        what the delegate knows about the principal
    POST   /api/v1/dossier/rebuild                 recompile it
    GET    /api/v1/commitments                     open action items
    POST   /api/v1/health/recheck                  re-run this account's model liveness check

Isolation is structural rather than enforced route by route: each handler
receives a :class:`~src.auth.deps.TenantContext` whose ``workspace`` holds
*that account's* index, memory, bridge and meetings, and there is no reference
on it that reaches another tenant's. Database reads go through
:mod:`src.db.repo`, every function of which takes a ``user_id`` and filters on
it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from src.api.delegate import (
    AbortRequest,
    DelegateBusyError,
    DelegateIdleError,
    DelegateStatus,
    DelegateUnavailableError,
    JoinRequest,
    idle_status,
)
from src.api.events import LiveEvent, LiveFeedHub
from src.audio.config import VoiceConfigError
from src.auth.deps import (
    ActiveContext,
    CurrentContext,
    TenantContext,
    resolve_websocket_context,
)
from src.brain.models import MeetingContext, PolicyDecision, PolicyTier, UserProfile
from src.compliance.consent import ConsentDecision, describe_modes
from src.db import repo
from src.db.base import CommittingRoute, run_db
from src.db.models import ConsentMode
from src.gateway.bot_base import MeetConfigError
from src.health import Degradation, health
from src.knowledge.config import KnowledgeSettings
from src.knowledge.dossier import SECTIONS, DossierBuilder
from src.knowledge.ingest import (
    SUPPORTED_SUFFIXES,
    DocumentIngestor,
    FileIngestResult,
    IngestReport,
    discover_documents,
)
from src.knowledge.retriever import DocumentKnowledge, RetrievalResult
from src.knowledge.store import KnowledgeStore
from src.llm import LlmError, preflight
from src.memory.commitments import Commitment, due_soon
from src.memory.history_store import MeetingBrief
from src.memory.meeting_state import MeetingSnapshot
from src.memory.post_meeting import PostMeetingReport
from src.turn_processor import TurnResult
from src.usage.meter import QuotaExceeded

logger = logging.getLogger("deleg8.api")

router = APIRouter(route_class=CommittingRoute)

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def upload_limit_bytes() -> int:
    """``DELEG8_UPLOAD_MAX_MB`` (default 25) in bytes."""
    raw = os.getenv("DELEG8_UPLOAD_MAX_MB", "").strip()
    try:
        megabytes = float(raw) if raw else 25.0
    except ValueError:
        megabytes = 25.0
    return int(max(1.0, megabytes) * 1024 * 1024)


def _documents(context: TenantContext) -> DocumentKnowledge:
    """This tenant's document index, or 503 when it could not be opened."""
    documents = context.workspace.documents if context.workspace is not None else None
    if documents is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "your document index could not be opened; answers fall back to your policy facts",
        )
    return documents


# --------------------------------------------------------------------------- #
# Response models
# --------------------------------------------------------------------------- #

class ProfileSummary(BaseModel):
    """Compact view of the Representation Policy for the dashboard."""

    user_id: str
    display_name: str
    aliases: list[str]
    rules_by_tier: dict[str, int] = Field(..., description="Rule counts keyed by GREEN / YELLOW / RED.")
    total_rules: int
    knowledge_entries: int

    @classmethod
    def from_profile(cls, profile: UserProfile) -> ProfileSummary:
        """Count rules per tier."""
        counts = {tier.value: 0 for tier in PolicyTier}
        for rule in profile.policy:
            counts[rule.tier.value] += 1
        return cls(
            user_id=profile.user_id,
            display_name=profile.display_name,
            aliases=list(profile.aliases),
            rules_by_tier=counts,
            total_rules=len(profile.policy),
            knowledge_entries=len(profile.knowledge_base),
        )


class DashboardResponse(BaseModel):
    """One round trip for the Overview page."""

    version: str
    generated_at: datetime
    delegate: DelegateStatus
    delegates: list[DelegateStatus] = Field(default_factory=list, description="Every running meeting.")
    profile: ProfileSummary
    meetings_attended: int = Field(..., description="Archived briefs.")
    pending_action_items: int
    live_meetings: list[str]
    knowledge_available: bool
    knowledge_index_size: int | None
    escalation_bridge: str
    knowledge: str
    summariser: str
    recent_briefs: list[MeetingBrief]
    plan: str
    usage_fractions: dict[str, float] = Field(default_factory=dict)
    quota_blocked: bool = False
    quota_reason: str | None = None
    consent_mode: str = ConsentMode.NOTIFY_ONLY.value
    onboarding_complete: bool = True
    healthy: bool = True
    banner: str | None = None


class IndexedDocument(BaseModel):
    """One source file as the index knows it."""

    source: str
    title: str
    chunks: int
    characters: int
    pages: int | None = None
    sha256: str | None = None
    on_disk: bool
    size_bytes: int | None = None
    modified_at: datetime | None = None


class UnindexedFile(BaseModel):
    """A supported file in the documents folder that has no chunks yet."""

    source: str
    size_bytes: int
    modified_at: datetime


class KnowledgeOverview(BaseModel):
    """Index health plus inventory, for this account only."""

    available: bool
    detail: str | None = None
    collection: str
    chroma_dir: str
    documents_dir: str
    embedding_model: str
    min_similarity: float
    top_k: int
    chunk_size: int
    chunk_overlap: int
    index_size: int
    documents: list[IndexedDocument]
    unindexed_files: list[UnindexedFile]
    supported_suffixes: list[str]
    upload_limit_bytes: int
    isolated: bool = Field(
        default=True, description="True when this index is this account's alone."
    )


class IndexedChunk(BaseModel):
    """One stored chunk."""

    chunk_id: str
    source: str
    title: str
    section: str
    page: int | None = None
    chunk_index: int
    characters: int
    text: str


class ChunkPage(BaseModel):
    """A page of one document's chunks."""

    source: str
    total: int
    offset: int
    limit: int
    chunks: list[IndexedChunk]


class UploadReport(BaseModel):
    """Outcome of an upload: each file saved and ingested."""

    documents_dir: str
    files: list[FileIngestResult]
    index_size: int


class RemoveDocumentResponse(BaseModel):
    """Outcome of removing a document."""

    source: str
    removed_chunks: int
    file_deleted: bool
    index_size: int


class KnowledgeQueryRequest(BaseModel):
    """Ad-hoc retrieval against this account's index."""

    query: str = Field(..., min_length=1, description="The question to search for.")
    top_k: int | None = Field(default=None, ge=1, le=20)
    min_similarity: float | None = Field(default=None, ge=0.0, le=1.0)


class IngestRequest(BaseModel):
    """Trigger re-ingestion of this account's documents folder."""

    force: bool = Field(default=False, description="Re-embed files even when unchanged.")


class MeetingListResponse(BaseModel):
    """Meetings with live memory in this process."""

    active: list[str]


class RecentMeeting(BaseModel):
    """One meeting from the database, whether or not it is still running."""

    id: str
    meeting_key: str
    platform: str
    status: str
    runtime: str
    phase: str
    join_url: str
    consent_mode: str
    consent_state: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    exit_code: int | None = None
    error: str | None = None
    stop_reason: str | None = None
    container: str | None = None


class ConsentRequest(BaseModel):
    """``POST /api/v1/meetings/{id}/consent`` - the principal answering for the room."""

    granted: bool = Field(..., description="True to allow retention, false to refuse and leave.")
    note: str | None = Field(default=None, max_length=500)


class ConsentRecordOut(BaseModel):
    """One entry in the consent audit trail."""

    id: str
    meeting_key: str
    mode: str
    outcome: str
    method: str
    granted_by: str | None = None
    evidence: str | None = None
    disclosure_text: str | None = None
    recorded_at: datetime


class ErrorResponse(BaseModel):
    """Uniform error body."""

    detail: str


# --------------------------------------------------------------------------- #
# Dashboard and delegate
# --------------------------------------------------------------------------- #

@router.get("/api/v1/dashboard", response_model=DashboardResponse, tags=["console"])
async def dashboard(request: Request, context: ActiveContext) -> DashboardResponse:
    """Status cards for the Overview page, for this account only."""
    workspace = context.workspace
    profile = workspace.profile_store.get_profile(context.principal_slug)
    briefs = await asyncio.to_thread(_briefs_for, workspace, 3)
    live = workspace.memory.active()

    from src.tenancy.credentials import resolve
    from src.usage.meter import quota_status

    resolved = resolve(context.session, context.user_id)
    own_keys = not (resolved.uses_platform_llm or resolved.uses_platform_voice)
    org_settings = context.org.settings if context.org is not None else None
    quota = quota_status(context.session, context.user_id, own_keys=own_keys, org_settings=org_settings)
    pending = repo.sum_action_items(context.session, context.user_id) + sum(
        len(m.pending_actions) for m in live
    )
    documents = workspace.documents
    index_size = await documents.retriever.index_size() if documents is not None else None
    from src.auth.service import account_response

    account = account_response(context.session, context.user)
    return DashboardResponse(
        version=request.app.version,
        generated_at=datetime.now(timezone.utc),
        delegate=workspace.delegate.status(),
        delegates=workspace.delegate.statuses(),
        profile=ProfileSummary.from_profile(profile),
        meetings_attended=repo.count_reports(context.session, context.user_id),
        pending_action_items=pending,
        live_meetings=[m.meeting_id for m in live],
        knowledge_available=documents is not None,
        knowledge_index_size=index_size,
        escalation_bridge=workspace.bridge.describe(),
        knowledge=workspace.components.engine.knowledge.describe(),
        summariser=workspace.components.reporter.describe(),
        recent_briefs=briefs,
        plan=quota.plan.name,
        usage_fractions=quota.fractions,
        quota_blocked=quota.blocked,
        quota_reason=quota.reason() if quota.blocked else None,
        consent_mode=workspace.default_consent_mode.value,
        onboarding_complete=account.onboarding_complete,
        healthy=health.healthy,
        banner=health.banner(),
    )


def _briefs_for(workspace: Any, limit: int | None = None) -> list[MeetingBrief]:
    """Archived briefs projected onto the list shape the console expects."""
    briefs: list[MeetingBrief] = []
    for row in workspace.history.list_rows(limit):
        try:
            report = PostMeetingReport.model_validate(row.payload)
        except Exception:  # noqa: BLE001 - one unreadable brief must not hide the rest
            logger.error("ignoring unreadable archived brief %s", row.brief_id)
            continue
        briefs.append(MeetingBrief.from_report(row.brief_id, report))
    return briefs


@router.get("/api/v1/delegate", response_model=DelegateStatus, tags=["console"])
def delegate_status(context: ActiveContext) -> DelegateStatus:
    """Where this account's delegate is, and what it is doing.

    Returns the oldest running meeting, or the last one that finished, so the
    single-meeting shape the Overview page renders stays meaningful. Use
    ``GET /api/v1/delegates`` when several are running.
    """
    if context.workspace is None or context.workspace.delegate is None:
        return idle_status()
    return context.workspace.delegate.status()


@router.get("/api/v1/delegates", response_model=list[DelegateStatus], tags=["console"])
def delegate_statuses(context: ActiveContext) -> list[DelegateStatus]:
    """Every meeting this account currently has running."""
    if context.workspace is None or context.workspace.delegate is None:
        return []
    return context.workspace.delegate.statuses()


@router.post(
    "/api/v1/meetings/join",
    response_model=DelegateStatus,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        400: {"model": ErrorResponse}, 402: {"model": ErrorResponse},
        409: {"model": ErrorResponse}, 503: {"model": ErrorResponse},
    },
    tags=["console"],
)
async def join_meeting(body: JoinRequest, context: ActiveContext) -> DelegateStatus:
    """Dispatch a delegate: a worker container joins the call and the voice loop starts.

    Returns as soon as the worker is launched; follow progress on the live feed.
    """
    try:
        return await context.workspace.delegate.launch(body)
    except DelegateBusyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except QuotaExceeded:
        raise
    except DelegateUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except (MeetConfigError, VoiceConfigError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc).splitlines()[0]) from exc


@router.post(
    "/api/v1/meetings/abort",
    response_model=DelegateStatus,
    responses={409: {"model": ErrorResponse}},
    tags=["console"],
)
async def abort_meeting(context: ActiveContext, body: AbortRequest | None = None) -> DelegateStatus:
    """Emergency recall: interrupt any speech, leave the call, send the briefing."""
    request_body = body or AbortRequest()
    try:
        return await context.workspace.delegate.abort(request_body.reason, request_body.meeting_id)
    except DelegateIdleError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


# --------------------------------------------------------------------------- #
# Live feed
# --------------------------------------------------------------------------- #

async def _pump(websocket: WebSocket, queue: asyncio.Queue[LiveEvent]) -> None:
    while True:
        event = await queue.get()
        await websocket.send_text(event.model_dump_json())


async def _listen(websocket: WebSocket) -> None:
    while True:
        message = await websocket.receive_text()
        if message.strip().lower() in ("ping", '{"type":"ping"}', '{"type": "ping"}'):
            await websocket.send_json({"type": "pong", "at": datetime.now(timezone.utc).isoformat()})


@router.websocket("/api/v1/meetings/live-feed")
async def live_feed(websocket: WebSocket) -> None:
    """Stream this account's pipeline events. Authenticated by ``?token=`` or a bearer header.

    The subscription is to the tenant's own :class:`~src.api.events.LiveFeedHub`,
    not a shared one, so there is no filtering step that could be got wrong: a
    socket opened by one account is physically attached to a queue only that
    account's meetings publish to.
    """
    resolved = await resolve_websocket_context(websocket)
    if resolved is None:
        await websocket.close(code=4401, reason="not authenticated")
        return
    _user, workspace = resolved
    hub: LiveFeedHub = workspace.live_feed
    await websocket.accept()
    queue, backlog = hub.subscribe()
    try:
        current = workspace.delegate.status() if workspace.delegate is not None else idle_status()
        snapshot: MeetingSnapshot | None = None
        if current.meeting_id:
            state = workspace.memory.get(current.meeting_id)
            snapshot = state.snapshot() if state is not None else None
        hello = {
            "seq": 0,
            "type": "hello",
            "at": datetime.now(timezone.utc),
            "payload": {
                "status": current,
                "statuses": workspace.delegate.statuses() if workspace.delegate is not None else [],
                "snapshot": snapshot,
                "backlog": backlog,
                "server_version": websocket.app.version,
            },
        }
        await websocket.send_json(jsonable_encoder(hello))
        sender = asyncio.create_task(_pump(websocket, queue), name="deleg8-ws-send")
        receiver = asyncio.create_task(_listen(websocket), name="deleg8-ws-recv")
        done, pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                logger.debug("live feed connection ended: %r", exc)
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(queue)


# --------------------------------------------------------------------------- #
# Representation Policy and the brain
# --------------------------------------------------------------------------- #

@router.get("/api/v1/profile", response_model=UserProfile, responses={404: {"model": ErrorResponse}}, tags=["console"])
def get_active_profile(context: ActiveContext) -> UserProfile:
    """This account's Representation Policy."""
    return context.workspace.profile_store.get_profile(context.principal_slug)


@router.put("/api/v1/profile", response_model=UserProfile, tags=["console"])
def save_active_profile(profile: UserProfile, context: ActiveContext) -> UserProfile:
    """Replace this account's policy.

    The principal id in the body is ignored in favour of the caller's own: a
    request may only ever rewrite its own policy, whatever it claims.
    """
    owned = profile.model_copy(update={"user_id": context.principal_slug})
    context.workspace.profile_store.save_profile(owned)
    return owned


@router.post(
    "/api/v1/evaluate",
    response_model=PolicyDecision,
    responses={404: {"model": ErrorResponse}},
    tags=["brain"],
)
async def evaluate(body: MeetingContext, context: ActiveContext) -> PolicyDecision:
    """Route one transcript snippet through the Policy Router (decision only; no memory update)."""
    scoped = body.model_copy(update={"user_id": context.principal_slug})
    return await context.workspace.components.engine.evaluate_async(scoped)


@router.post(
    "/api/v1/process-turn",
    response_model=TurnResult,
    responses={404: {"model": ErrorResponse}},
    tags=["brain"],
)
async def process_turn(body: MeetingContext, context: ActiveContext) -> TurnResult:
    """Decide, escalate to this account's own channel if required, update memory, and reply."""
    scoped = body.model_copy(update={"user_id": context.principal_slug})
    return await context.workspace.components.turns.process_turn(scoped)


# --------------------------------------------------------------------------- #
# Knowledge base
# --------------------------------------------------------------------------- #

def _inventory(store: KnowledgeStore) -> tuple[int, dict[str, dict[str, Any]]]:
    """Runs on the store's worker thread: every chunk's metadata grouped by source."""
    with store.lock:
        collection = store.collection
        size = collection.count()
        raw = collection.get(include=["documents", "metadatas"]) if size else {"ids": [], "documents": [], "metadatas": []}
    by_source: dict[str, dict[str, Any]] = {}
    for text, meta in zip(raw.get("documents") or [], raw.get("metadatas") or []):
        meta = meta or {}
        source = str(meta.get("source", "unknown"))
        entry = by_source.setdefault(source, {"title": str(meta.get("title", "")), "chunks": 0, "characters": 0,
                                              "sha256": meta.get("file_sha256"), "pages": set()})
        entry["chunks"] += 1
        entry["characters"] += len(text or "")
        if "page" in meta and isinstance(meta["page"], (int, float)):
            entry["pages"].add(int(meta["page"]))
    return size, by_source


def _file_info(path: Path) -> tuple[int, datetime]:
    stat = path.stat()
    return stat.st_size, datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)


@router.get("/api/v1/knowledge", response_model=KnowledgeOverview, tags=["console"])
async def knowledge_overview(context: ActiveContext) -> KnowledgeOverview:
    """This account's index health and document inventory."""
    workspace = context.workspace
    documents = workspace.documents
    settings = workspace.knowledge_settings or KnowledgeSettings.for_tenant(
        workspace.paths.documents, workspace.paths.collection, workspace.paths.chroma
    )
    if documents is None:
        return KnowledgeOverview(
            available=False, detail="your document index could not be opened",
            collection=settings.collection_name, chroma_dir=str(settings.chroma_dir),
            documents_dir=str(settings.documents_dir), embedding_model=settings.embedding_model,
            min_similarity=settings.min_similarity, top_k=settings.top_k, chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap, index_size=0, documents=[], unindexed_files=[],
            supported_suffixes=sorted(SUPPORTED_SUFFIXES), upload_limit_bytes=upload_limit_bytes(),
        )
    store = documents.retriever.store
    settings = store.settings
    size, by_source = await store.run(lambda: _inventory(store))
    root = settings.documents_dir
    root.mkdir(parents=True, exist_ok=True)
    on_disk = {path.relative_to(root).as_posix(): path for path in discover_documents(root)}

    indexed: list[IndexedDocument] = []
    for source, entry in sorted(by_source.items()):
        path = on_disk.get(source)
        size_bytes, modified = _file_info(path) if path is not None else (None, None)
        indexed.append(IndexedDocument(
            source=source, title=entry["title"], chunks=entry["chunks"], characters=entry["characters"],
            pages=len(entry["pages"]) or None, sha256=entry["sha256"], on_disk=path is not None,
            size_bytes=size_bytes, modified_at=modified,
        ))
    unindexed = [
        UnindexedFile(source=source, size_bytes=_file_info(path)[0], modified_at=_file_info(path)[1])
        for source, path in sorted(on_disk.items()) if source not in by_source
    ]
    return KnowledgeOverview(
        available=True, collection=settings.collection_name, chroma_dir=str(settings.chroma_dir),
        documents_dir=str(root), embedding_model=settings.embedding_model, min_similarity=settings.min_similarity,
        top_k=settings.top_k, chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap, index_size=size,
        documents=indexed, unindexed_files=unindexed, supported_suffixes=sorted(SUPPORTED_SUFFIXES),
        upload_limit_bytes=upload_limit_bytes(),
    )


@router.get("/api/v1/knowledge/chunks", response_model=ChunkPage, responses={503: {"model": ErrorResponse}}, tags=["console"])
async def knowledge_chunks(
    context: ActiveContext,
    source: str = Query(..., min_length=1, description="Path relative to your documents folder."),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ChunkPage:
    """The stored chunks of one of this account's documents, in document order."""
    store = _documents(context).retriever.store

    def _fetch() -> list[IndexedChunk]:
        with store.lock:
            raw = store.collection.get(where={"source": source}, include=["documents", "metadatas"])
        chunks: list[IndexedChunk] = []
        for chunk_id, text, meta in zip(raw["ids"], raw.get("documents") or [], raw.get("metadatas") or []):
            meta = meta or {}
            page = meta.get("page")
            index = meta.get("chunk_index", 0)
            chunks.append(IndexedChunk(
                chunk_id=chunk_id, source=source, title=str(meta.get("title", "")), section=str(meta.get("section", "")),
                page=int(page) if isinstance(page, (int, float)) else None,
                chunk_index=int(index) if isinstance(index, (int, float)) else 0,
                characters=len(text or ""), text=text or "",
            ))
        chunks.sort(key=lambda c: c.chunk_index)
        return chunks

    chunks = await store.run(_fetch)
    return ChunkPage(source=source, total=len(chunks), offset=offset, limit=limit, chunks=chunks[offset:offset + limit])


@router.post(
    "/api/v1/knowledge/query",
    response_model=RetrievalResult,
    responses={503: {"model": ErrorResponse}},
    tags=["knowledge"],
)
async def query_knowledge(body: KnowledgeQueryRequest, context: ActiveContext) -> RetrievalResult:
    """Search this account's index directly and see the similarity gate at work."""
    documents = _documents(context)
    return await documents.retriever.retrieve(
        body.query, top_k=body.top_k, min_similarity=body.min_similarity
    )


def _safe_filename(name: str | None) -> str:
    """Reduce an uploaded filename to a single safe path component."""
    base = Path(name or "").name.strip().replace(" ", "_")
    base = _UNSAFE_FILENAME.sub("_", base).strip(".")  # hidden files are skipped by discovery, so never create one
    return base or "upload"


async def _save_upload(upload: UploadFile, target: Path, limit: int) -> int:
    """Stream ``upload`` to ``target`` (atomically), enforcing ``limit`` bytes. Returns the size."""
    tmp = target.with_name(target.name + ".uploading")
    written = 0
    try:
        with tmp.open("wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise ValueError(f"file exceeds the {limit // (1024 * 1024)} MB upload limit")
                handle.write(chunk)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return written


@router.post(
    "/api/v1/knowledge/upload",
    response_model=UploadReport,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    tags=["console"],
)
async def upload_documents(
    context: ActiveContext,
    files: list[UploadFile] = File(..., description="One or more .md / .txt / .pdf files."),
) -> UploadReport:
    """Save each file under this account's own folder and ingest it into their own index."""
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no files were uploaded")
    documents = _documents(context)
    store = documents.retriever.store
    root = store.settings.documents_dir
    root.mkdir(parents=True, exist_ok=True)
    ingestor = DocumentIngestor(store)
    limit = upload_limit_bytes()
    results: list[FileIngestResult] = []
    for upload in files:
        name = _safe_filename(upload.filename)
        suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            results.append(FileIngestResult(
                source=name, status="failed",
                detail=f"unsupported file type {suffix or '(none)'}; use {', '.join(sorted(SUPPORTED_SUFFIXES))}",
            ))
            continue
        target = root / name
        try:
            size = await _save_upload(upload, target, limit)
        except ValueError as exc:
            results.append(FileIngestResult(source=name, status="failed", detail=str(exc)))
            continue
        except OSError as exc:
            results.append(FileIngestResult(source=name, status="failed", detail=f"could not save: {exc}"))
            continue
        logger.info("user %s uploaded %s (%d bytes); ingesting", context.user.email, name, size)
        result = await ingestor.ingest_file(target, root=root)
        results.append(result)
        repo.upsert_document(
            context.session,
            user_id=context.user_id,
            source=result.source,
            title=result.source,
            size_bytes=size,
            chunks=getattr(result, "chunks", 0) or 0,
            status=result.status,
            detail=result.detail,
        )
    repo.set_onboarding(context.session, context.user, documents=True)
    return UploadReport(documents_dir=str(root), files=results, index_size=await store.run(store.count))


@router.post(
    "/api/v1/knowledge/ingest",
    response_model=IngestReport,
    responses={503: {"model": ErrorResponse}},
    tags=["knowledge"],
)
async def ingest_documents(body: IngestRequest, context: ActiveContext) -> IngestReport:
    """(Re)ingest every supported file in this account's documents folder."""
    store = _documents(context).retriever.store
    report = await DocumentIngestor(store).ingest_directory(
        Path(store.settings.documents_dir), force=body.force
    )
    for result in report.files:
        repo.upsert_document(
            context.session,
            user_id=context.user_id,
            source=result.source,
            title=result.source,
            chunks=getattr(result, "chunks", 0) or 0,
            status=result.status,
            detail=result.detail,
        )
    return report


@router.delete(
    "/api/v1/knowledge/documents/{source:path}",
    response_model=RemoveDocumentResponse,
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    tags=["console"],
)
async def remove_document(source: str, context: ActiveContext) -> RemoveDocumentResponse:
    """Remove a document's chunks from this account's index and delete its file."""
    store = _documents(context).retriever.store

    def _remove() -> int:
        with store.lock:
            collection = store.collection
            ids = collection.get(where={"source": source}, include=[])["ids"]
            if ids:
                collection.delete(ids=ids)
            return len(ids)

    removed = await store.run(_remove)
    root = store.settings.documents_dir.resolve()
    path = (root / source).resolve()
    file_deleted = False
    if path.is_relative_to(root) and path.is_file():
        path.unlink()
        file_deleted = True
    repo.delete_document(context.session, context.user_id, source)
    if removed == 0 and not file_deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no document named {source!r} in your index or folder")
    logger.info("user %s removed %s: %d chunks", context.user.email, source, removed)
    return RemoveDocumentResponse(
        source=source, removed_chunks=removed, file_deleted=file_deleted, index_size=await store.run(store.count)
    )


# --------------------------------------------------------------------------- #
# Meetings and memory
# --------------------------------------------------------------------------- #

@router.get("/api/v1/meetings", response_model=MeetingListResponse, tags=["memory"])
def list_meetings(context: ActiveContext) -> MeetingListResponse:
    """This account's meetings that currently have live memory."""
    return MeetingListResponse(active=[m.meeting_id for m in context.workspace.memory.active()])


@router.get("/api/v1/meetings/recent", response_model=list[RecentMeeting], tags=["memory"])
def recent_meetings(
    context: CurrentContext,
    limit: int = Query(default=25, ge=1, le=200),
) -> list[RecentMeeting]:
    """Every meeting this account has dispatched, newest first."""
    return [
        RecentMeeting(
            id=str(m.id),
            meeting_key=m.meeting_key,
            platform=m.platform,
            status=m.status,
            runtime=m.runtime,
            phase=m.phase,
            join_url=m.join_url,
            consent_mode=m.consent_mode,
            consent_state=m.consent_state,
            started_at=m.started_at,
            ended_at=m.ended_at,
            exit_code=m.exit_code,
            error=m.error,
            stop_reason=m.stop_reason,
            container=m.container_name,
        )
        for m in repo.list_meetings(context.session, context.user_id, limit=limit)
    ]


@router.get(
    "/api/v1/meetings/{meeting_id}",
    response_model=MeetingSnapshot,
    responses={404: {"model": ErrorResponse}},
    tags=["memory"],
)
def get_meeting(meeting_id: str, context: ActiveContext) -> MeetingSnapshot:
    """Live state of one of this account's meetings."""
    state = context.workspace.memory.get(meeting_id)
    if state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no active meeting named {meeting_id!r}")
    return state.snapshot()


@router.post(
    "/api/v1/meetings/{meeting_id}/conclude",
    response_model=PostMeetingReport,
    responses={404: {"model": ErrorResponse}},
    tags=["memory"],
)
async def conclude_meeting(meeting_id: str, context: ActiveContext) -> PostMeetingReport:
    """End a meeting: generate the briefing, send it, archive it, release its memory."""
    workspace = context.workspace
    state = workspace.memory.get(meeting_id)
    if state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no active meeting named {meeting_id!r}")
    return await workspace.components.reporter.conclude(state, registry=workspace.memory)


@router.post(
    "/api/v1/meetings/{meeting_id}/consent",
    response_model=DelegateStatus,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    tags=["compliance"],
)
async def resolve_consent(meeting_id: str, body: ConsentRequest, context: ActiveContext) -> DelegateStatus:
    """Grant or refuse recording consent for a live meeting, as the principal.

    Only meaningful for a meeting running in this process: a containerised
    meeting's gate lives in the container, and consent there has to come from
    the room (or the meeting has to be recalled).
    """
    workspace = context.workspace
    delegate = workspace.delegate
    meeting = delegate.get(meeting_id) if delegate is not None else None
    if meeting is None or not meeting.active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no running meeting named {meeting_id!r}")
    session_obj = (
        workspace.inprocess_dispatcher.session_for(meeting_id)
        if workspace.inprocess_dispatcher is not None
        else None
    )
    if session_obj is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{meeting_id} runs in a container; consent must come from the room, or recall the delegate",
        )
    gate = session_obj.consent
    decision: ConsentDecision = (
        gate.grant_externally(by=context.display_name)
        if body.granted
        else gate.deny_externally(by=context.display_name)
    )
    delegate.record_consent_decision(meeting_id, decision)
    return delegate.status(meeting_id)


@router.get("/api/v1/consent/records", response_model=list[ConsentRecordOut], tags=["compliance"])
def consent_records(
    context: CurrentContext,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[ConsentRecordOut]:
    """This account's consent audit trail: what was announced, what was said, what was decided."""
    keys: dict[uuid.UUID, str] = {
        m.id: m.meeting_key for m in repo.list_meetings(context.session, context.user_id, limit=500)
    }
    return [
        ConsentRecordOut(
            id=str(row.id),
            meeting_key=keys.get(row.meeting_id, str(row.meeting_id)),
            mode=row.mode,
            outcome=row.outcome,
            method=row.method,
            granted_by=row.granted_by,
            evidence=row.evidence,
            disclosure_text=row.disclosure_text,
            recorded_at=row.recorded_at,
        )
        for row in repo.list_consent_records(context.session, context.user_id, limit)
    ]


@router.get("/api/v1/consent/modes", tags=["compliance"])
def consent_modes() -> list[dict[str, str]]:
    """The consent modes and what each one actually does."""
    return describe_modes()


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #

@router.get("/api/v1/history", response_model=list[MeetingBrief], tags=["console"])
async def list_history(
    context: ActiveContext,
    limit: int | None = Query(default=None, ge=1, le=500),
) -> list[MeetingBrief]:
    """This account's archived meeting briefs, newest first."""
    return await asyncio.to_thread(_briefs_for, context.workspace, limit)


@router.get(
    "/api/v1/history/{brief_id}",
    response_model=PostMeetingReport,
    responses={404: {"model": ErrorResponse}},
    tags=["console"],
)
async def get_brief(brief_id: str, context: CurrentContext) -> PostMeetingReport:
    """One archived report in full. Only this account's briefs are reachable."""
    payload = await run_db(lambda s: _report_payload(s, context.user_id, brief_id))
    if payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no archived brief named {brief_id!r}")
    return PostMeetingReport.model_validate(payload)


def _report_payload(session: Any, user_id: uuid.UUID, brief_id: str) -> dict | None:
    row = repo.get_report(session, user_id, brief_id)
    return dict(row.payload) if row is not None else None


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #

class DegradationReport(BaseModel):
    """What is running in a fallback mode for this account right now."""

    healthy: bool
    banner: str | None = Field(default=None, description="One line summarising every degradation, or null.")
    degradations: list[Degradation] = Field(default_factory=list)
    llm: str = Field(..., description="The provider and models this account's delegate uses.")
    planner_ready: bool = Field(..., description="True when the delegate can actually compose replies.")
    own_key: bool = Field(default=False, description="True when the model runs on this account's own key.")


@router.post("/api/v1/health/recheck", response_model=DegradationReport, tags=["console"])
async def recheck_llm(context: ActiveContext) -> DegradationReport:
    """Re-run the model liveness check for *this account's* configured model."""
    workspace = context.workspace
    llm = getattr(workspace.components, "llm", None)
    result = await preflight(llm)
    logger.info("user %s rechecked their model: %s", context.user.email, result.line())
    provider = workspace.credentials.llm_provider
    return DegradationReport(
        healthy=health.healthy and result.ok,
        banner=health.banner() or (None if result.ok else result.detail),
        degradations=health.current(),
        llm=(llm.describe() if llm is not None else "none configured"),
        planner_ready=getattr(workspace.components, "planner", None) is not None and result.ok,
        own_key=bool(provider and workspace.credentials.owns(provider)),
    )


# --------------------------------------------------------------------------- #
# Dossier
# --------------------------------------------------------------------------- #

class DossierResponse(BaseModel):
    """The principal's compiled dossier, as the console edits it."""

    user_id: str
    markdown: str
    describe: str = Field(..., description="One-line provenance for the header.")
    hand_edited: bool
    built_at: datetime | None = None
    built_by: str | None = None
    sources: list[str] = Field(default_factory=list)
    sections: list[str] = Field(default_factory=list, description="The fixed section headings.")


class DossierEdit(BaseModel):
    """A hand-edited dossier saved from the console."""

    markdown: str = Field(..., min_length=1, description="The full Markdown document.")


def _dossier_response(context: TenantContext) -> DossierResponse:
    dossier = context.workspace.dossiers.load(context.principal_slug)
    meta = dossier.meta
    return DossierResponse(
        user_id=context.principal_slug,
        markdown=dossier.markdown,
        describe=dossier.describe(),
        hand_edited=bool(meta and meta.hand_edited),
        built_at=meta.built_at if meta else None,
        built_by=meta.built_by if meta else None,
        sources=list(meta.sources) if meta else [],
        sections=list(SECTIONS),
    )


@router.get("/api/v1/dossier", response_model=DossierResponse, tags=["console"])
def get_dossier(context: ActiveContext) -> DossierResponse:
    """What this account's delegate knows about its principal, in full.

    The product's trust surface: the principal reads exactly what their
    delegate will say about them, and corrects it.
    """
    return _dossier_response(context)


@router.put("/api/v1/dossier", response_model=DossierResponse, tags=["console"])
def save_dossier(edit: DossierEdit, context: ActiveContext) -> DossierResponse:
    """Save a hand-edited dossier. A hand edit is never overwritten by a rebuild."""
    context.workspace.dossiers.save_edit(context.principal_slug, edit.markdown)
    logger.info("user %s hand-edited their dossier (%d chars)", context.user.email, len(edit.markdown))
    return _dossier_response(context)


@router.post(
    "/api/v1/dossier/rebuild",
    response_model=DossierResponse,
    responses={503: {"model": ErrorResponse}},
    tags=["console"],
)
async def rebuild_dossier(
    context: ActiveContext,
    force: bool = Query(default=True, description="Rebuild even when hand-edited or unchanged."),
) -> DossierResponse:
    """Recompile the dossier from this account's own ingested documents."""
    workspace = context.workspace
    settings = workspace.knowledge_settings or KnowledgeSettings.for_tenant(
        workspace.paths.documents, workspace.paths.collection, workspace.paths.chroma
    )
    builder = DossierBuilder(llm=getattr(workspace.components, "llm", None), store=workspace.dossiers)
    try:
        await builder.build(
            user_id=context.principal_slug,
            principal=context.display_name,
            settings=settings,
            force=force,
        )
    except LlmError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"could not compile the dossier: {exc}") from exc
    return _dossier_response(context)


# --------------------------------------------------------------------------- #
# Commitments
# --------------------------------------------------------------------------- #

class CommitmentsResponse(BaseModel):
    """Open action items for this account."""

    total: int
    due_soon: int = Field(..., description="Due today or tomorrow.")
    by_meeting: dict[str, list[Commitment]] = Field(default_factory=dict)


@router.get("/api/v1/commitments", response_model=CommitmentsResponse, tags=["console"])
def list_commitments(context: ActiveContext) -> CommitmentsResponse:
    """Everything the room has asked this principal to do, live and persisted.

    Live meetings are read from the in-memory ledgers (which are ahead of the
    database by up to one write), and everything else from the ``commitments``
    table, so an action item from a meeting that ended yesterday - or that ran
    in a container on another machine - still appears.
    """
    workspace = context.workspace
    by_meeting: dict[str, list[Commitment]] = {}
    urgent = 0
    live_meetings: set[str] = set()
    for state in workspace.memory.active():
        ledger = workspace.commitments.get(state.meeting_id)
        if ledger is None:
            continue
        items = ledger.open_items()
        live_meetings.add(state.meeting_id)
        if items:
            by_meeting[state.meeting_id] = items
            urgent += sum(1 for c in items if due_soon(c))

    keys = {m.id: m.meeting_key for m in repo.list_meetings(context.session, context.user_id, limit=200)}
    stored: dict[str, list[Commitment]] = {}
    for row in repo.list_commitments(context.session, context.user_id, open_only=True):
        key = keys.get(row.meeting_id, str(row.meeting_id))
        if key in live_meetings:
            continue
        stored.setdefault(key, []).append(_commitment_from_row(row))
    for key, items in stored.items():
        by_meeting[key] = items
        urgent += sum(1 for c in items if due_soon(c))
    return CommitmentsResponse(
        total=sum(len(v) for v in by_meeting.values()), due_soon=urgent, by_meeting=by_meeting
    )


def _commitment_from_row(row: Any) -> Commitment:
    """Project a persisted commitment back onto the in-memory contract."""
    from src.memory.commitments import CommitmentSource, CommitmentStatus

    return Commitment(
        id=row.local_id,
        description=row.description,
        owner=row.owner,
        requested_by=row.requested_by,
        due_iso=row.due_iso,
        due_text=row.due_text,
        status=CommitmentStatus(row.status),
        source=CommitmentSource(row.source),
        acknowledged=row.acknowledged,
        first_turn=row.first_turn,
        last_turn=row.last_turn,
        created_at=row.created_at,
        updated_at=row.updated_at,
        notified_at=row.notified_at,
    )
