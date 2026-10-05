"""
The legal documents, their versions, and who accepted which.

A product that sits in other people's meetings and transcribes them cannot
ship to strangers without a Terms of Service and a Privacy Policy, and a
checkbox that records nothing is not acceptance. So:

* the documents are files under ``legal/`` with a ``version:`` line in their
  front matter, served by the API and rendered by the console;
* signup will not complete without accepting the current version of each
  required document;
* every acceptance is a row (:class:`~src.db.models.LegalAcceptance`) with the
  version, the timestamp, the IP and the user agent;
* when a document's version changes, existing users are re-prompted on their
  next request that matters, because :func:`outstanding_documents` compares
  against the *current* version rather than "has ever accepted anything".
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from sqlalchemy.orm import Session

from src.db import repo

logger = logging.getLogger("deleg8.compliance.legal")

LEGAL_DIR: Path = Path(__file__).resolve().parents[2] / "legal"

REQUIRED_DOCUMENTS: tuple[str, ...] = ("terms", "privacy")
"""Documents every account must accept before the delegate will do anything."""

_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class LegalDocument:
    """One versioned legal document."""

    key: str
    title: str
    version: str
    effective_date: str
    body: str
    summary: str = ""

    @property
    def required(self) -> bool:
        """True when acceptance is mandatory."""
        return self.key in REQUIRED_DOCUMENTS


def _parse(key: str, text: str) -> LegalDocument:
    """Split ``key: value`` front matter from the Markdown body."""
    meta: dict[str, str] = {}
    body = text
    match = _FRONT_MATTER.match(text)
    if match:
        for line in match.group(1).splitlines():
            if ":" in line:
                name, _, value = line.partition(":")
                meta[name.strip().lower()] = value.strip()
        body = text[match.end():]
    return LegalDocument(
        key=key,
        title=meta.get("title", key.title()),
        version=meta.get("version", "1"),
        effective_date=meta.get("effective", ""),
        summary=meta.get("summary", ""),
        body=body.strip(),
    )


@lru_cache(maxsize=8)
def load_document(key: str) -> LegalDocument | None:
    """Read one document from ``legal/<key>.md``, or ``None`` when absent."""
    path = LEGAL_DIR / f"{key}.md"
    if not path.is_file():
        logger.error("legal document %s is missing at %s", key, path)
        return None
    return _parse(key, path.read_text(encoding="utf-8"))


def reload_documents() -> None:
    """Drop the cache so an edited document is picked up without a restart."""
    load_document.cache_clear()


def all_documents() -> list[LegalDocument]:
    """Every document found under ``legal/``."""
    documents: list[LegalDocument] = []
    for path in sorted(LEGAL_DIR.glob("*.md")):
        document = load_document(path.stem)
        if document is not None:
            documents.append(document)
    return documents


def current_versions() -> dict[str, str]:
    """The current version of every required document."""
    versions: dict[str, str] = {}
    for key in REQUIRED_DOCUMENTS:
        document = load_document(key)
        if document is not None:
            versions[key] = document.version
    return versions


def accepted_versions(session: Session, user_id: uuid.UUID) -> dict[str, str]:
    """The newest accepted version of each document for one user."""
    accepted: dict[str, tuple[str, datetime]] = {}
    for row in repo.list_acceptances(session, user_id):
        seen = accepted.get(row.document)
        if seen is None or row.accepted_at > seen[1]:
            accepted[row.document] = (row.version, row.accepted_at)
    return {key: value[0] for key, value in accepted.items()}


def outstanding_documents(session: Session, user_id: uuid.UUID) -> list[str]:
    """Required documents this user has not accepted at their current version."""
    accepted = accepted_versions(session, user_id)
    return [key for key, version in current_versions().items() if accepted.get(key) != version]


def accept_all(
    session: Session,
    user_id: uuid.UUID,
    *,
    documents: list[str] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> list[str]:
    """Record acceptance of the current version of each named document.

    Returns the document keys actually recorded. Unknown keys are ignored
    rather than raising, so a stale console build cannot block a signup.
    """
    recorded: list[str] = []
    for key in documents or list(REQUIRED_DOCUMENTS):
        document = load_document(key)
        if document is None:
            continue
        repo.record_acceptance(
            session,
            user_id=user_id,
            document=key,
            version=document.version,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        recorded.append(key)
    return recorded
