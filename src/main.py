"""
DELEG8 master entry point.

Two ways in, one process each:

* ``python -m src.main join`` - one delegate attends one meeting and exits.
  This is what the worker container runs. On a laptop it reads its
  configuration from ``.env``; dispatched into a container it fetches its
  tenant's policy and dossier from the control plane with a one-meeting token
  (:mod:`src.dispatch.bundle`) and reports every event back the same way.
* ``python -m src.main serve`` (or ``uvicorn src.main:app``) - the control
  plane: accounts, policies, knowledge bases, dispatch, the live feed, usage,
  consent and the legal documents.

What changed when this stopped being a single-user program
----------------------------------------------------------
Everything in the old lifespan was a process-wide singleton: one profile
store, one vector index, one Telegram poller pointed at one chat id, one
memory registry, one delegate. Every one of those is now per tenant and lives
in a :class:`~src.tenancy.workspace.Workspace`, opened on the first
authenticated request and evicted when idle.

Three things remain process-wide, because they genuinely are:

* the database engine (:mod:`src.db`);
* the Telegram hub (:mod:`src.escalation.hub`) - Telegram permits exactly one
  poller per bot token, so it must be a singleton; it routes each update to a
  tenant by chat id before anything is done with it;
* the embedding model, shared by every tenant's otherwise separate index.

The API surface is in :mod:`src.api.routes` (console), :mod:`src.auth.routes`
(accounts), :mod:`src.api.onboarding`, :mod:`src.api.settings`,
:mod:`src.api.usage`, :mod:`src.api.legal`, :mod:`src.api.worker` (the
callback the containers talk to) and :mod:`src.api.admin`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.api.admin import router as admin_router
from src.api.analytics import router as analytics_router
from src.api.calendar import router as calendar_router
from src.api.commitments_tracker import router as commitments_tracker_router
from src.api.integrations import router as integrations_router
from src.api.legal import router as legal_router
from src.api.onboarding import router as onboarding_router
from src.api.organizations import router as organizations_router
from src.api.playground import router as playground_router
from src.api.routes import router as console_router
from src.api.search import router as search_router
from src.api.settings import router as settings_router
from src.api.usage import router as usage_router
from src.api.worker import router as worker_router
from src.auth.routes import router as auth_router
from src.auth.security import AuthError, require_production_secrets
from src.brain.profile_store import ProfileNotFoundError
from src.calendar.scheduler import CalendarScheduler
from src.db.base import create_all, describe_backend, init_engine, ping
from src.dispatch.base import configured_mode
from src.dispatch.container import ContainerDispatcher
from src.escalation.hub import TelegramHub
from src.escalation.telegram_bot import EscalationConfigError
from src.health import COMPONENT_LLM, Degradation
from src.health import health as health_registry
from src.health import report_degraded, report_healthy
from src.llm import build_llm, preflight
from src.tenancy.workspace import WorkspaceManager
from src.usage.meter import QuotaExceeded

load_dotenv()

APP_VERSION: str = "1.0.0"
HOST: str = os.getenv("DELEG8_HOST", "127.0.0.1")
PORT: int = int(os.getenv("DELEG8_PORT", "8000"))
LOG_LEVEL: str = os.getenv("DELEG8_LOG_LEVEL", "INFO").upper()
CORS_ORIGINS: list[str] = [
    origin.strip()
    for origin in os.getenv("DELEG8_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
    if origin.strip()
]

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("deleg8.api")

COMPONENT_DATABASE: str = "database"


class HealthResponse(BaseModel):
    """Liveness payload for the whole deployment.

    Deliberately says nothing about any one tenant: this endpoint is
    unauthenticated so a load balancer can call it, and "which principals
    exist" is not a fact an unauthenticated caller should be able to read.
    Per-tenant health is on ``GET /api/v1/dashboard``.
    """

    status: str = Field(..., description="'ok' when every component is healthy.")
    version: str = Field(..., description="Application version.")
    database: str = Field(..., description="Backend and whether it answers.")
    escalation: str = Field(..., description="The shared Telegram hub.")
    dispatch: str = Field(..., description="How meetings are run, and whether that runtime is usable.")
    workspaces: str = Field(..., description="Open tenant workspaces and running meetings.")
    llm: str = Field(default="none configured", description="The platform's default provider and models.")
    healthy: bool = Field(default=True, description="False when any component is in a fallback mode.")
    banner: str | None = Field(default=None, description="One line summarising every degradation, or null.")
    degradations: list[Degradation] = Field(default_factory=list)
    generated_at: datetime


class ErrorResponse(BaseModel):
    """Uniform error body."""

    detail: str = Field(..., description="What went wrong.")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the process-wide components before serving and stop them on shutdown."""
    require_production_secrets()

    init_engine()
    create_all()
    if ping():
        report_healthy(COMPONENT_DATABASE)
    else:
        report_degraded(
            COMPONENT_DATABASE,
            "the database is unreachable; nothing can be saved",
            f"check DATABASE_URL ({describe_backend()})",
        )

    hub = TelegramHub()
    try:
        await hub.start()
    except EscalationConfigError:
        logger.exception("the platform Telegram bot could not start; tenants must bring their own")
        report_degraded(
            "telegram",
            "the shared Telegram bot is not running; escalations fall back to the holding reply",
            "check TELEGRAM_BOT_TOKEN",
        )

    workspaces = WorkspaceManager(hub)
    workspaces.start_sweeper()

    calendar_scheduler = CalendarScheduler(workspaces, hub)
    calendar_scheduler.start()

    dispatcher = ContainerDispatcher()
    dispatch_ok, dispatch_detail = await dispatcher.available()

    app.state.hub = hub
    app.state.workspaces = workspaces
    app.state.calendar_scheduler = calendar_scheduler
    app.state.container_dispatcher = dispatcher
    app.state.dispatch_detail = dispatch_detail
    app.state.started_at = datetime.now(timezone.utc)

    # The platform's own model credentials are checked once at startup rather
    # than discovered mid-meeting. A tenant on their own key is unaffected by a
    # failure here; one on the platform key would otherwise get a delegate with
    # no brain, which is the exact failure this check exists to prevent.
    result = await preflight(build_llm())
    logger.info("%s", result.line())
    if result.ok:
        report_healthy(COMPONENT_LLM)
    else:
        report_degraded(
            COMPONENT_LLM,
            "the platform language model is not answering; tenants without their own key have no brain",
            result.detail,
        )

    logger.info(
        "DELEG8 %s ready | %s | %s | dispatch: %s (%s) | CORS: %s",
        APP_VERSION, describe_backend(), hub.describe(),
        configured_mode().value, dispatch_detail, ", ".join(CORS_ORIGINS),
    )
    try:
        yield
    finally:
        await calendar_scheduler.stop()
        await workspaces.shutdown()
        await hub.stop()
        logger.info("DELEG8 shut down cleanly")


app = FastAPI(
    title="DELEG8",
    version=APP_VERSION,
    description=(
        "A multi-tenant AI meeting delegate. Each account has its own Representation "
        "Policy, its own isolated knowledge index, its own notification channel, its "
        "own usage allowance and its own meetings - each of which runs in a container "
        "of its own."
    ),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ProfileNotFoundError)
async def _profile_not_found(_: Request, exc: ProfileNotFoundError) -> JSONResponse:
    """Map an unknown principal to HTTP 404."""
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})


@app.exception_handler(AuthError)
async def _auth_error(_: Request, exc: AuthError) -> JSONResponse:
    """Map a credential or token failure to HTTP 401."""
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": str(exc)},
        headers={"WWW-Authenticate": "Bearer"},
    )


@app.exception_handler(QuotaExceeded)
async def _quota_exceeded(_: Request, exc: QuotaExceeded) -> JSONResponse:
    """Map a spent allowance to HTTP 402, with the numbers that caused it."""
    return JSONResponse(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        content={
            "detail": str(exc),
            "plan": exc.status.plan.name,
            "exceeded": exc.status.exceeded,
            "usage": exc.status.fractions,
        },
    )


@app.get("/api/v1/health", response_model=HealthResponse, tags=["ops"])
async def health(request: Request) -> HealthResponse:
    """Deployment liveness: database, escalation hub, dispatch runtime, workspaces."""
    state = request.app.state
    workspaces: WorkspaceManager = state.workspaces
    database_ok = ping()
    if database_ok:
        report_healthy(COMPONENT_DATABASE)
    else:
        report_degraded(COMPONENT_DATABASE, "the database is unreachable; nothing can be saved", None)
    llm = build_llm()
    return HealthResponse(
        status="ok" if health_registry.healthy else "degraded",
        version=APP_VERSION,
        database=f"{describe_backend()} - {'reachable' if database_ok else 'UNREACHABLE'}",
        escalation=state.hub.describe(),
        dispatch=f"{configured_mode().value}: {state.dispatch_detail}",
        workspaces=workspaces.describe(),
        llm=llm.describe() if llm is not None else "none configured",
        healthy=health_registry.healthy,
        banner=health_registry.banner(),
        degradations=health_registry.current(),
        generated_at=datetime.now(timezone.utc),
    )


app.include_router(auth_router)
app.include_router(legal_router)
app.include_router(onboarding_router)
app.include_router(settings_router)
app.include_router(usage_router)
app.include_router(worker_router)
app.include_router(admin_router)
app.include_router(integrations_router)
app.include_router(calendar_router)
app.include_router(search_router)
app.include_router(commitments_tracker_router)
app.include_router(analytics_router)
app.include_router(organizations_router)
app.include_router(playground_router)
app.include_router(console_router)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str]) -> argparse.Namespace:
    """``join`` (default) attends one meeting; ``serve`` runs the control plane."""
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv = ["join", *argv]
    parser = argparse.ArgumentParser(prog="python -m src.main", description="DELEG8 - AI meeting delegate")
    commands = parser.add_subparsers(dest="command", required=True)

    join = commands.add_parser("join", help="join one meeting as the delegate (default)")
    join.add_argument("--url", default=None, help="meeting link (default: MEET_JOIN_URL)")
    join.add_argument("--meeting-id", default=None, help="memory / briefing id (default: <platform>-<code>)")
    join.add_argument("--speaker", default="Meeting", help="label for whoever is heard in the call")
    join.add_argument("--half-duplex", action="store_true", help="mute the bot's mic while it speaks (no barge-in)")
    join.add_argument("--no-briefing", action="store_true", help="skip the executive briefing when the session ends")
    join.add_argument("--max-minutes", type=float, default=None, help="leave automatically after this many minutes")
    join.add_argument("--dry-run", action="store_true", help="open the pre-join screen, report what is there, exit")
    join.add_argument("--log-level", default=None, help="component log level (default: DELEG8_LOG_LEVEL)")

    serve = commands.add_parser("serve", help="run the control plane with uvicorn")
    serve.add_argument("--host", default=HOST)
    serve.add_argument("--port", type=int, default=PORT)
    serve.add_argument("--reload", action="store_true", help="auto-reload on code changes (development; restarts drop in-process meetings)")

    admin = commands.add_parser("createadmin", help="create or promote an administrator account")
    admin.add_argument("--email", required=True)
    admin.add_argument("--password", default=None, help="required when creating a new account")
    admin.add_argument("--name", default=None, help="display name for a new account")
    return parser.parse_args(argv)


def _create_admin(email: str, password: str | None, name: str | None) -> int:
    """``createadmin``: bootstrap the first administrator without a web signup."""
    from src.auth.service import provision_account
    from src.db import repo
    from src.db.base import create_all, init_engine, session_scope

    init_engine()
    create_all()
    with session_scope() as session:
        user = repo.get_user_by_email(session, email)
        if user is not None:
            user.role = "admin"
            session.add(user)
            print(f"promoted {email} to administrator", flush=True)
            return 0
        if not password:
            print("that account does not exist; pass --password to create it", flush=True)
            return 2
        created = provision_account(
            session, email=email, password=password, display_name=name or email.split("@")[0]
        )
        created.role = "admin"
        session.add(created)
        print(f"created administrator {email} (principal {repo.primary_principal(session, created.id).slug})", flush=True)
        return 0


def main(argv: list[str]) -> int:
    """Entry point for ``python -m src.main``."""
    args = parse_args(argv)
    if args.command == "serve":
        uvicorn.run("src.main:app", host=args.host, port=args.port, reload=args.reload, log_level=LOG_LEVEL.lower())
        return 0
    if args.command == "createadmin":
        return _create_admin(args.email, args.password, args.name)

    from src.gateway.session import SessionOptions, run_meeting_session

    if args.log_level:
        logging.getLogger().setLevel(args.log_level.upper())
    options = SessionOptions(
        join_url=args.url,
        meeting_id=args.meeting_id,
        speaker_label=args.speaker,
        half_duplex=args.half_duplex,
        briefing=not args.no_briefing,
        dry_run=args.dry_run,
        max_call_seconds=args.max_minutes * 60 if args.max_minutes else None,
    )
    return asyncio.run(run_meeting_session(options))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
