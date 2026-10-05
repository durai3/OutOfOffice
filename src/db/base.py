"""
The database seam: one engine, one session factory, one place that knows the URL.

DELEG8 was a single-tenant program: the Representation Policy lived in
``data/profiles/durai.json``, the meeting archive in ``data/history/*.json``,
the commitments ledger in a Python dict that died with the process. None of
that survives a second user, so every one of those stores now has a table in
this schema (:mod:`src.db.models`) and a repository in :mod:`src.db.repo`.

Sync SQLAlchemy, deliberately. The hot path is
``ProfileStore.get_profile(user_id)``, called on every transcript turn from
inside the synchronous :class:`~src.brain.policy_engine.PolicyEngine`, and
turning that protocol async would ripple through the brain, the turn
processor and the orchestrator for no benefit: profile reads are served from
a per-process cache (:class:`~src.db.stores.DbProfileStore`) and the
remaining queries are single-row lookups. Route handlers that do real work -
listing an archive, paging usage - go through :func:`run_db` so the event
loop is never blocked.

``DATABASE_URL`` selects the backend. Postgres is what this is built for::

    DATABASE_URL=postgresql+psycopg://deleg8:deleg8@127.0.0.1:55432/deleg8

SQLite is accepted for a laptop with no server running, so tests and a first
``python -m src.main serve`` work out of the box; it is not a supported
deployment target and :func:`describe_backend` says so on the health page.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

from fastapi import Request, Response
from fastapi.routing import APIRoute
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

logger = logging.getLogger("deleg8.db")

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

T = TypeVar("T")

DEFAULT_SQLITE_PATH: Path = PROJECT_ROOT / "data" / "deleg8.db"


class Base(DeclarativeBase):
    """Declarative base for every DELEG8 table."""


class DatabaseNotConfigured(RuntimeError):
    """Raised when the database is used before :func:`init_engine` has run."""


def database_url() -> str:
    """``DATABASE_URL``, defaulting to a local SQLite file.

    A bare ``postgresql://`` URL is rewritten to ``postgresql+psycopg://`` so
    the driver actually installed is the one used; the same URL then works
    unchanged in a container, in ``docker compose``, and on a managed Postgres.
    """
    raw = os.getenv("DATABASE_URL", "").strip()
    if not raw:
        DEFAULT_SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+pysqlite:///{DEFAULT_SQLITE_PATH}"
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://"):]
    if raw.startswith("postgresql://"):
        raw = "postgresql+psycopg://" + raw[len("postgresql://"):]
    return raw


def is_postgres(url: str | None = None) -> bool:
    """True when the configured backend is Postgres."""
    return (url or database_url()).startswith("postgresql")


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def init_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    """Create the process-wide engine and session factory. Idempotent."""
    global _engine, _session_factory
    if _engine is not None:
        return _engine
    resolved = url or database_url()
    if resolved.startswith("sqlite"):
        # One shared connection, no thread affinity check: the ORM sessions are
        # short-lived and serialised by SQLite's own locking, and a file-backed
        # StaticPool is what lets a background meeting task and a request
        # handler see each other's writes.
        engine = create_engine(
            resolved, echo=echo, future=True,
            connect_args={"check_same_thread": False, "timeout": 30},
            poolclass=StaticPool if ":memory:" in resolved else None,
        )

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()
    else:
        engine = create_engine(
            resolved, echo=echo, future=True, pool_pre_ping=True,
            pool_size=int(os.getenv("DELEG8_DB_POOL_SIZE", "10")),
            max_overflow=int(os.getenv("DELEG8_DB_MAX_OVERFLOW", "20")),
            pool_recycle=1800,
        )
    _engine = engine
    _session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    logger.info("database engine ready: %s", describe_backend(resolved))
    return engine


def get_engine() -> Engine:
    """The process-wide engine, creating it on first use."""
    return _engine or init_engine()


def describe_backend(url: str | None = None) -> str:
    """Status line for the health page, with credentials stripped."""
    resolved = url or database_url()
    if resolved.startswith("sqlite"):
        return f"sqlite (development only): {resolved.split('///')[-1]}"
    try:
        tail = resolved.split("@", 1)[1]
    except IndexError:
        tail = resolved
    return f"postgres: {tail}"


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transactional session: commits on success, rolls back on any exception."""
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def db_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency yielding a transactional session.

    The session is also stashed on ``request.state`` so
    :class:`CommittingRoute` can commit it *before* the response is sent.
    Since FastAPI 0.106 a ``yield`` dependency's cleanup runs after the
    response has gone out, which for a write endpoint means the client is told
    "done" while the transaction is still open - and a client that immediately
    reads back (or, as it turned out here, tries to sign in to an account it
    just deleted) races the commit.
    """
    with session_scope() as session:
        request.state.db_session = session
        yield session


class CommittingRoute(APIRoute):
    """A route that commits its request's session before the response is sent.

    Applied once, on the router, rather than remembered at each write endpoint:
    "did this handler remember to commit" is exactly the kind of thing that is
    right in ten places and wrong in the eleventh.
    """

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        """Wrap the generated handler with a commit."""
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            response = await original(request)
            session = getattr(request.state, "db_session", None)
            if session is not None and session.in_transaction():
                session.commit()
            return response

        return handler


async def run_db(fn: Callable[[Session], T]) -> T:
    """Run ``fn`` against a session on a worker thread and await the result.

    Every async route that touches the database goes through this, so a slow
    query delays one request instead of the whole event loop (and with it every
    live meeting the process is running).
    """

    def _call() -> T:
        with session_scope() as session:
            return fn(session)

    return await asyncio.to_thread(_call)


def create_all() -> None:
    """Create any missing tables. Safe to call on every start."""
    from src.db import models  # noqa: F401  - registers the mappings

    engine = get_engine()
    Base.metadata.create_all(engine)
    logger.info("schema ready: %d table(s)", len(Base.metadata.tables))


def ping() -> bool:
    """True when the database answers a trivial query."""
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 - the caller turns this into a degradation
        logger.error("database unreachable: %s", exc)
        return False


def reset_engine() -> None:
    """Drop the cached engine (tests switching database URLs)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
