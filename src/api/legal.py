"""
``/api/v1/legal`` - the Terms of Service and Privacy Policy, and who accepted them.

    GET  /api/v1/legal/documents          list every document with its current version
    GET  /api/v1/legal/documents/{key}    one document's full Markdown
    GET  /api/v1/legal/status             which required documents this account still owes
    POST /api/v1/legal/accept             record acceptance of the current versions

The list and fetch endpoints are unauthenticated on purpose: the signup form
has to render the terms before an account exists to attach them to.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from src.auth.deps import CurrentContext
from src.compliance import legal
from src.db.base import CommittingRoute

logger = logging.getLogger("deleg8.api.legal")

router = APIRouter(prefix="/api/v1/legal", tags=["legal"], route_class=CommittingRoute)


class LegalSummary(BaseModel):
    """One document, without its body."""

    key: str
    title: str
    version: str
    effective_date: str
    summary: str
    required: bool


class LegalDocumentResponse(LegalSummary):
    """One document with its full Markdown body."""

    body: str


class LegalStatus(BaseModel):
    """What this account has accepted, and what it still owes."""

    current_versions: dict[str, str]
    accepted_versions: dict[str, str]
    outstanding: list[str] = Field(
        ..., description="Required documents not accepted at their current version."
    )
    up_to_date: bool
    history: list[AcceptanceRecord] = Field(default_factory=list)


class AcceptanceRecord(BaseModel):
    """One recorded acceptance."""

    document: str
    version: str
    accepted_at: datetime


class AcceptRequest(BaseModel):
    """``POST /api/v1/legal/accept``."""

    documents: list[str] | None = Field(
        default=None, description="Which documents to accept; omit for every required one."
    )


def _summary(document: legal.LegalDocument) -> LegalSummary:
    return LegalSummary(
        key=document.key,
        title=document.title,
        version=document.version,
        effective_date=document.effective_date,
        summary=document.summary,
        required=document.required,
    )


@router.get("/documents", response_model=list[LegalSummary])
def list_documents() -> list[LegalSummary]:
    """Every legal document and its current version. Unauthenticated."""
    return [_summary(document) for document in legal.all_documents()]


@router.get("/documents/{key}", response_model=LegalDocumentResponse, responses={404: {"description": "unknown document"}})
def get_document(key: str) -> LegalDocumentResponse:
    """One document's full Markdown. Unauthenticated, so signup can render it."""
    document = legal.load_document(key)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no legal document named {key!r}")
    return LegalDocumentResponse(**_summary(document).model_dump(), body=document.body)


@router.get("/status", response_model=LegalStatus)
def acceptance_status(context: CurrentContext) -> LegalStatus:
    """What this account has accepted, and what a version bump has left outstanding."""
    session, user_id = context.session, context.user_id
    outstanding = legal.outstanding_documents(session, user_id)
    return LegalStatus(
        current_versions=legal.current_versions(),
        accepted_versions=legal.accepted_versions(session, user_id),
        outstanding=outstanding,
        up_to_date=not outstanding,
        history=[
            AcceptanceRecord(document=row.document, version=row.version, accepted_at=row.accepted_at)
            for row in legal.list_acceptances(session, user_id)
        ],
    )


@router.post("/accept", response_model=LegalStatus)
def accept(body: AcceptRequest, request: Request, context: CurrentContext) -> LegalStatus:
    """Record acceptance of the current version of each named (or every required) document."""
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else None)
    recorded = legal.accept_all(
        context.session,
        context.user_id,
        documents=body.documents,
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
    )
    from src.db import repo

    if not legal.outstanding_documents(context.session, context.user_id):
        repo.set_onboarding(context.session, context.user, legal=True)
    logger.info("user %s accepted: %s", context.user.email, ", ".join(recorded) or "nothing")
    return acceptance_status(context)


LegalStatus.model_rebuild()
