"""
Where one tenant's files live, and how their vector collection is named.

Single-tenant DELEG8 had exactly one of each: ``data/documents``,
``data/chroma_db`` with the collection ``deleg8_documents``. Both are now
per-user::

    data/tenants/<user-id>/documents/     uploaded files
    data/tenants/<user-id>/screenshots/   Meet screenshots from that user's runs
    data/chroma_db/                       one directory, one collection per user

The collection name is derived from the account's UUID rather than from
anything the user controls, so no display name, email or principal slug can
be crafted to collide with another tenant's collection. Two accounts cannot
share an index even if they somehow share a name.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

_SAFE = re.compile(r"[^a-z0-9_-]+")


def tenants_root() -> Path:
    """``DELEG8_TENANT_DIR`` (default ``data/tenants``), resolved against the project root."""
    raw = os.getenv("DELEG8_TENANT_DIR", "").strip() or "data/tenants"
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else (PROJECT_ROOT / candidate).resolve()


def collection_name(user_id: uuid.UUID | str) -> str:
    """The Chroma collection for one tenant.

    Chroma requires 3-512 characters, alphanumeric plus ``_-``, starting and
    ending alphanumerically - a bare UUID with dashes satisfies that, but the
    prefix makes the collections self-describing when listed.
    """
    token = _SAFE.sub("", str(user_id).lower())
    return f"deleg8_u_{token}"


@dataclass(frozen=True)
class TenantPaths:
    """Every filesystem location belonging to one account."""

    user_id: uuid.UUID
    root: Path

    @classmethod
    def for_user(cls, user_id: uuid.UUID | str) -> TenantPaths:
        """Paths for ``user_id``, without creating anything."""
        resolved = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
        return cls(user_id=resolved, root=tenants_root() / str(resolved))

    @property
    def documents(self) -> Path:
        """Where this tenant's uploaded source files are kept."""
        return self.root / "documents"

    @property
    def screenshots(self) -> Path:
        """Where this tenant's meeting screenshots are written."""
        return self.root / "screenshots"

    @property
    def chrome_profile(self) -> Path:
        """A browser profile directory for this tenant's container runs."""
        return self.root / "chrome_profile"

    @property
    def chroma(self) -> Path:
        """This tenant's vector index.

        A directory per tenant rather than one directory with many collections,
        because a worker container is given *only* this tenant's ``/data`` and
        must be able to query the index from inside it. Sharing one directory
        would mean mounting every tenant's vectors into every meeting.
        """
        return self.root / "chroma"

    @property
    def collection(self) -> str:
        """This tenant's Chroma collection name."""
        return collection_name(self.user_id)

    def ensure(self) -> TenantPaths:
        """Create the directories. Idempotent."""
        for path in (self.documents, self.screenshots, self.chroma):
            path.mkdir(parents=True, exist_ok=True)
        return self

    def purge(self) -> int:
        """Delete every file this tenant owns. Returns how many bytes were removed."""
        if not self.root.exists():
            return 0
        total = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        shutil.rmtree(self.root, ignore_errors=True)
        return total

    def usage_bytes(self) -> int:
        """Total size of this tenant's stored documents."""
        if not self.documents.exists():
            return 0
        return sum(p.stat().st_size for p in self.documents.rglob("*") if p.is_file())
