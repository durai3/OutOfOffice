"""
Representation Policy store.

Sprint 1 shipped an in-memory store seeded from a hard-coded dictionary.
Sprint 6 (the web console) needs the policy to be *editable* and to survive a
restart, so the process-wide default is now :class:`JsonFileProfileStore`:
one JSON document per principal under ``DELEG8_PROFILES_DIR`` (default
``data/profiles/``), seeded from the built-in principal the first time the
directory is empty. :class:`InMemoryProfileStore` remains for tests.

The engine depends only on the :class:`ProfileStore` protocol and re-reads
the profile on every evaluation, so a saved change takes effect on the very
next transcript snippet.

The GREEN ``documented_facts`` rule (Sprint 4) is what authorises the
delegate to answer from the ingested document index: without it, questions
that match no other trigger never reach the retriever.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from src.brain.models import KnowledgeEntry, PolicyRule, PolicyTier, UserProfile

logger = logging.getLogger("deleg8.brain.profiles")

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]


class ProfileNotFoundError(LookupError):
    """Raised when no Representation Policy exists for the requested principal."""

    def __init__(self, user_id: str) -> None:
        super().__init__(f"No Representation Policy found for user_id={user_id!r}")
        self.user_id = user_id


@runtime_checkable
class ProfileStore(Protocol):
    """Lookup and persistence of principals and their Representation Policies."""

    def get_profile(self, user_id: str) -> UserProfile:
        """Return the profile for ``user_id`` or raise :class:`ProfileNotFoundError`."""
        ...

    def list_user_ids(self) -> list[str]:
        """Return every known ``user_id``."""
        ...

    def save_profile(self, profile: UserProfile) -> None:
        """Create or replace the profile for ``profile.user_id``."""
        ...


DEFAULT_USER_ID: str = "durai"

_SEED_PROFILES: dict[str, UserProfile] = {
    DEFAULT_USER_ID: UserProfile(
        user_id=DEFAULT_USER_ID,
        display_name="Durai Kumaran",
        aliases=["Durai", "DK"],
        policy=[
            PolicyRule(
                topic="project_status",
                tier=PolicyTier.GREEN,
                description="Give project status and progress updates.",
                triggers=[
                    "status",
                    "progress",
                    "update on",
                    "how far along",
                    "where are we",
                    "where do we stand",
                    "on track",
                ],
            ),
            PolicyRule(
                topic="documented_facts",
                tier=PolicyTier.GREEN,
                description="State facts recorded in the ingested project documents.",
                triggers=[
                    "project alpha",
                    "architecture",
                    "tech stack",
                    "the docs",
                    "documentation",
                    "runbook",
                    "design doc",
                ],
            ),
            PolicyRule(
                topic="deadline_commitment",
                tier=PolicyTier.YELLOW,
                description="Commit to delivery dates and deadlines.",
                triggers=[
                    "deadline",
                    "deliver by",
                    "deliver this by",
                    "deliver it by",
                    "delivery date",
                    "ship by",
                    "ship this by",
                    "commit to",
                    "timeline",
                    "when will",
                    "when can",
                    "how soon",
                    "by monday",
                    "by tuesday",
                    "by wednesday",
                    "by thursday",
                    "by friday",
                    "by tomorrow",
                    "by end of",
                    "by eod",
                    "by eow",
                    "by next week",
                ],
            ),
            PolicyRule(
                topic="pricing_negotiation",
                tier=PolicyTier.RED,
                description="Negotiate pricing, discounts, or commercial terms.",
                triggers=[
                    "price",
                    "pricing",
                    "discount",
                    "cost",
                    "quote",
                    "budget",
                    "cheaper",
                    "lower the",
                    "percent off",
                    "% off",
                    "rate card",
                    "licensing fee",
                    "commercial terms",
                ],
            ),
            PolicyRule(
                topic="contract_legal",
                tier=PolicyTier.RED,
                description="Contract, legal, or liability matters.",
                triggers=[
                    "contract",
                    "legal",
                    "liability",
                    "indemnity",
                    "indemnification",
                    "terms of service",
                    "sla",
                    "nda",
                    "master services agreement",
                ],
            ),
            PolicyRule(
                topic="compensation",
                tier=PolicyTier.RED,
                description="Salary, compensation, equity, or what the principal expects to be paid.",
                triggers=[
                    "salary",
                    "compensation",
                    "how much do you earn",
                    "how much do you make",
                    "expected pay",
                    "pay expectation",
                    "salary expectation",
                    "equity",
                    "stock options",
                    "bonus",
                    "day rate",
                    "hourly rate",
                ],
            ),
            PolicyRule(
                topic="personal_sensitive",
                tier=PolicyTier.RED,
                description="Health, family, religion, politics, immigration status, or other private matters.",
                triggers=[
                    "medical",
                    "health condition",
                    "disability",
                    "religion",
                    "religious",
                    "politics",
                    "political",
                    "married",
                    "marital",
                    "children",
                    "visa status",
                    "immigration",
                    "citizenship status",
                    "date of birth",
                    "home address",
                ],
            ),
            PolicyRule(
                topic="offer_acceptance",
                tier=PolicyTier.YELLOW,
                description="Accepting an offer, agreeing a start date, or committing to join.",
                triggers=[
                    "accept the offer",
                    "accept our offer",
                    "take the job",
                    "join us",
                    "start date",
                    "when can you start",
                    "notice period",
                    "sign the offer",
                ],
            ),
        ],
        knowledge_base=[
            KnowledgeEntry(
                topic="project_status",
                subject="api_integration",
                keywords=["api integration", "api", "integration"],
                answer=(
                    "The API integration is on track. Authentication and webhook handlers "
                    "are complete, rate limiting is in code review, and integration testing "
                    "is scheduled for this sprint."
                ),
            ),
            KnowledgeEntry(
                topic="project_status",
                subject="mobile_app",
                keywords=["mobile app", "mobile", "ios", "android"],
                answer=(
                    "The mobile app is in closed beta with 40 internal testers. Crash-free "
                    "sessions are at 99.2%, and the remaining onboarding issues are being "
                    "fixed before public rollout."
                ),
            ),
        ],
    ),
}


def seed_profiles() -> list[UserProfile]:
    """Fresh copies of the built-in principals."""
    return [profile.model_copy(deep=True) for profile in _SEED_PROFILES.values()]


def profiles_dir_from_env() -> Path:
    """``DELEG8_PROFILES_DIR`` (default ``data/profiles``), resolved against the project root."""
    raw = os.getenv("DELEG8_PROFILES_DIR", "").strip() or "data/profiles"
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else (PROJECT_ROOT / candidate).resolve()


class InMemoryProfileStore:
    """Dictionary-backed :class:`ProfileStore` for local development and tests."""

    def __init__(self, profiles: Iterable[UserProfile] | None = None) -> None:
        """Seed the store; defaults to the built-in principal when ``profiles`` is omitted."""
        source = list(profiles) if profiles is not None else seed_profiles()
        self._profiles: dict[str, UserProfile] = {p.user_id: p for p in source}

    def get_profile(self, user_id: str) -> UserProfile:
        """Return the profile for ``user_id`` or raise :class:`ProfileNotFoundError`."""
        try:
            return self._profiles[user_id]
        except KeyError as exc:
            raise ProfileNotFoundError(user_id) from exc

    def list_user_ids(self) -> list[str]:
        """Return every known ``user_id`` in insertion order."""
        return list(self._profiles)

    def save_profile(self, profile: UserProfile) -> None:
        """Create or replace ``profile`` in memory."""
        self._profiles[profile.user_id] = profile


class JsonFileProfileStore:
    """One ``<user_id>.json`` per principal under a directory; loaded lazily, written atomically.

    The directory is resolved on first access rather than at construction so
    that ``.env`` (loaded by the entry points after imports) is honoured.
    Missing seed principals are written to disk the first time the store is
    opened, which is how a fresh checkout gets its default policy.
    """

    def __init__(self, directory: Path | None = None, seed: Iterable[UserProfile] | None = None) -> None:
        """``directory`` defaults to :func:`profiles_dir_from_env` at first use."""
        self._configured_dir = directory
        self._seed = list(seed) if seed is not None else None
        self._profiles: dict[str, UserProfile] | None = None
        self._lock = threading.RLock()

    @property
    def directory(self) -> Path:
        """Where profiles are stored."""
        return self._configured_dir or profiles_dir_from_env()

    def _path(self, user_id: str) -> Path:
        return self.directory / f"{user_id}.json"

    def _write(self, profile: UserProfile) -> None:
        path = self._path(profile.user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(profile.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def _load(self) -> dict[str, UserProfile]:
        if self._profiles is not None:
            return self._profiles
        with self._lock:
            if self._profiles is not None:
                return self._profiles
            directory = self.directory
            directory.mkdir(parents=True, exist_ok=True)
            profiles: dict[str, UserProfile] = {}
            for path in sorted(directory.glob("*.json")):
                try:
                    profile = UserProfile.model_validate_json(path.read_text(encoding="utf-8"))
                except (OSError, ValidationError) as exc:
                    logger.error("ignoring unreadable profile %s: %s", path, exc)
                    continue
                profiles[profile.user_id] = profile
            seeds = self._seed if self._seed is not None else seed_profiles()
            for profile in seeds:
                if profile.user_id not in profiles:
                    profiles[profile.user_id] = profile
                    self._write(profile)
                    logger.info("seeded Representation Policy for %s at %s", profile.user_id, self._path(profile.user_id))
            self._profiles = profiles
            logger.info("profile store ready: %d principal(s) in %s", len(profiles), directory)
            return profiles

    def reload(self) -> None:
        """Drop the cache so the next access re-reads the directory."""
        with self._lock:
            self._profiles = None

    def get_profile(self, user_id: str) -> UserProfile:
        """Return the profile for ``user_id`` or raise :class:`ProfileNotFoundError`."""
        try:
            return self._load()[user_id]
        except KeyError as exc:
            raise ProfileNotFoundError(user_id) from exc

    def list_user_ids(self) -> list[str]:
        """Return every known ``user_id``, seeds first, then alphabetical additions."""
        return list(self._load())

    def save_profile(self, profile: UserProfile) -> None:
        """Persist ``profile`` (atomic replace) and update the cache."""
        with self._lock:
            profiles = self._load()
            self._write(profile)
            profiles[profile.user_id] = profile
        logger.info("saved Representation Policy for %s (%d rules, %d facts)",
                    profile.user_id, len(profile.policy), len(profile.knowledge_base))


profile_store: JsonFileProfileStore = JsonFileProfileStore()
"""Process-wide default store used by the API layer and the meeting session."""
