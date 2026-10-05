"""
What a worker container is told about the tenant it is representing.

A worker is deliberately not given ``DATABASE_URL``. If it were, a bug or a
compromise in the code that drives Chromium would sit on a connection that can
read every tenant's policies, transcripts and credentials - which is precisely
the blast radius containerisation is meant to remove.

So the worker asks for exactly what it needs, once, with a token that
authorises exactly one meeting: the principal's Representation Policy, their
dossier, and the meeting's consent mode. That is a
:class:`MeetingBundle`. It is served by ``GET /api/v1/worker/bundle`` and
consumed here into in-memory stores that satisfy the same protocols the
database-backed ones do, so nothing downstream knows the difference.

If the fetch fails the worker does not start. A delegate that joined a call
without its principal's policy would have no notion of what is RED, which is
the one failure mode worse than not joining at all.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from src.brain.models import UserProfile
from src.brain.profile_store import ProfileNotFoundError

logger = logging.getLogger("deleg8.dispatch.bundle")


class MeetingBundle(BaseModel):
    """Everything a worker needs about its tenant, fetched once at startup."""

    meeting_key: str = Field(..., description="The id memory and the briefing use.")
    user_id: str = Field(..., description="The owning account.")
    profile: UserProfile = Field(..., description="The principal's Representation Policy.")
    dossier_markdown: str = Field(default="", description="The compiled dossier, if one exists.")
    dossier_built_by: str = Field(default="", description="Provenance line for the dossier.")
    consent_mode: str = Field(default="notify_only")
    principal_name: str = Field(default="")
    escalation_available: bool = Field(
        default=False, description="Whether the tenant has a live notification channel."
    )


class BundleProfileStore:
    """A read-only :class:`~src.brain.profile_store.ProfileStore` holding one principal."""

    def __init__(self, profile: UserProfile) -> None:
        """Hold the one profile this worker represents."""
        self._profile = profile

    def get_profile(self, user_id: str) -> UserProfile:
        """The bundled profile, or :class:`ProfileNotFoundError` for any other id."""
        if user_id != self._profile.user_id:
            raise ProfileNotFoundError(user_id)
        return self._profile

    def list_user_ids(self) -> list[str]:
        """Just the one principal."""
        return [self._profile.user_id]

    def save_profile(self, profile: UserProfile) -> None:
        """Refused: a worker never edits a policy.

        Raises:
            RuntimeError: always.
        """
        raise RuntimeError("a meeting worker cannot modify the Representation Policy")


class BundleDossierStore:
    """A read-only dossier store holding the bundled Markdown."""

    def __init__(self, user_id: str, markdown: str, built_by: str = "") -> None:
        """Hold the one dossier this worker was given."""
        self._user_id = user_id
        self._markdown = markdown
        self._built_by = built_by

    def load(self, user_id: str) -> Any:
        """The bundled dossier, or an empty one for any other id."""
        from datetime import datetime, timezone

        from src.knowledge.dossier import Dossier, DossierMeta

        if user_id != self._user_id or not self._markdown:
            return Dossier(user_id=user_id, markdown="", meta=None)
        return Dossier(
            user_id=user_id,
            markdown=self._markdown,
            meta=DossierMeta(
                user_id=user_id,
                built_at=datetime.now(timezone.utc),
                built_by=self._built_by or "control plane",
            ),
        )

    def save(self, user_id: str, markdown: str, meta: Any) -> None:
        """Refused: a worker never rebuilds a dossier.

        Raises:
            RuntimeError: always.
        """
        raise RuntimeError("a meeting worker cannot rebuild the dossier")

    def save_edit(self, user_id: str, markdown: str) -> None:
        """Refused: a worker never edits a dossier.

        Raises:
            RuntimeError: always.
        """
        raise RuntimeError("a meeting worker cannot edit the dossier")


class BundleError(RuntimeError):
    """The worker could not obtain its tenant bundle."""


async def fetch_bundle(url: str, token: str, *, timeout: float = 20.0) -> MeetingBundle:
    """Fetch this worker's bundle from the control plane.

    Raises:
        BundleError: the control plane refused, or could not be reached.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{url.rstrip('/')}/api/v1/worker/bundle",
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as exc:  # noqa: BLE001 - any transport failure is fatal for a worker
        raise BundleError(f"could not reach the control plane at {url}: {exc}") from exc
    if response.status_code == 401:
        raise BundleError("the control plane rejected this job token; the meeting may have been cancelled")
    if response.status_code >= 400:
        raise BundleError(f"the control plane returned HTTP {response.status_code}: {response.text[:300]}")
    try:
        return MeetingBundle.model_validate(response.json())
    except Exception as exc:  # noqa: BLE001 - a malformed bundle is as fatal as no bundle
        raise BundleError(f"the control plane returned an unusable bundle: {exc}") from exc
