"""
``/api/v1/onboarding`` - the four things a new account must do before it is useful.

    GET  /api/v1/onboarding              every step, its state, and what to do next
    GET  /api/v1/onboarding/templates    the starter Representation Policies
    POST /api/v1/onboarding/policy       install a template (or confirm the current policy)
    POST /api/v1/onboarding/telegram     mint a one-time link code for the bot
    POST /api/v1/onboarding/skip         skip an optional step
    POST /api/v1/onboarding/complete     mark onboarding finished

The four steps, in the order they matter:

1. **legal** - accepted at signup; re-appears when a document version changes.
2. **policy** - a starter policy is installed at signup, so this step is about
   the user *seeing* what their delegate will and will not say. An
   unreviewed policy is the single most likely way a delegate says something
   its principal would not have.
3. **escalation** - link a Telegram chat. Skippable: without it YELLOW turns
   fall back to the holding reply instead of reaching a human, which is safe
   but much less useful, and the step says so.
4. **documents** - upload something. A delegate with no dossier can route a
   question correctly and still have nothing to say.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from src.auth.deps import ActiveContext, CurrentContext
from src.auth.models import AccountResponse
from src.auth.service import ONBOARDING_STEPS, account_response
from src.brain.policy_templates import TEMPLATES, build_profile, describe_templates
from src.compliance import legal
from src.db import repo
from src.db.base import CommittingRoute
from src.escalation.hub import issue_link_code

logger = logging.getLogger("deleg8.api.onboarding")

router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"], route_class=CommittingRoute)

_TITLES: dict[str, tuple[str, str]] = {
    "legal": (
        "Accept the terms",
        "The Terms of Service and Privacy Policy, including who is responsible for the meetings you send a delegate to.",
    ),
    "policy": (
        "Review what your delegate may say",
        "Your Representation Policy: which topics it answers on its own, which need your approval, and which it must never touch.",
    ),
    "escalation": (
        "Connect your phone",
        "Link a Telegram chat so approvals and briefings reach you. Without it, anything needing approval gets a holding reply instead.",
    ),
    "documents": (
        "Upload something about you",
        "A CV, a project brief, anything. Your delegate compiles it into a dossier and answers from that.",
    ),
}
_OPTIONAL: frozenset[str] = frozenset({"escalation"})


class OnboardingStep(BaseModel):
    """One step of the first-run flow."""

    key: str
    title: str
    description: str
    complete: bool
    optional: bool
    detail: str | None = Field(default=None, description="What is currently true for this account.")


class TelegramLink(BaseModel):
    """The one-time code and where to send it."""

    code: str | None = None
    expires_at: datetime | None = None
    bot_username: str | None = None
    linked: bool = False
    instructions: str


class OnboardingState(BaseModel):
    """Everything the onboarding wizard renders."""

    complete: bool
    next_step: str | None = Field(..., description="The first incomplete step, or null when done.")
    steps: list[OnboardingStep]
    account: AccountResponse
    telegram: TelegramLink


class PolicyChoice(BaseModel):
    """``POST /api/v1/onboarding/policy``."""

    template: str | None = Field(
        default=None, description="Install this starter policy; omit to keep the current one."
    )


class SkipRequest(BaseModel):
    """``POST /api/v1/onboarding/skip``."""

    step: str = Field(..., description="Which optional step to skip.")


def _telegram_link(context: CurrentContext, request: Request) -> TelegramLink:
    channel = repo.get_channel(context.session, context.user_id, "telegram")
    hub = getattr(request.app.state, "hub", None)
    username = getattr(hub, "bot_username", None) if hub is not None else None
    if channel is not None and channel.verified and channel.address:
        return TelegramLink(
            linked=True,
            bot_username=username,
            instructions="Your phone is connected. Send /status to the bot to check on your delegate.",
        )
    if hub is None or not getattr(hub, "available", False):
        return TelegramLink(
            linked=False,
            instructions=(
                "This server has no shared Telegram bot configured. You can still connect your own "
                "bot from Settings, or skip this step - approvals will fall back to a holding reply."
            ),
        )
    handle = f"@{username}" if username else "the DELEG8 bot"
    code = channel.link_code if channel is not None else None
    return TelegramLink(
        code=code,
        expires_at=channel.link_code_expires_at if channel is not None else None,
        bot_username=username,
        linked=False,
        instructions=(
            f"Open Telegram, start a chat with {handle}, and send:  /link {code or '<code>'}"
            if code
            else f"Request a code below, then send it to {handle} as  /link <code>"
        ),
    )


def _state(context: CurrentContext, request: Request) -> OnboardingState:
    session, user = context.session, context.user
    stored = dict(user.onboarding or {})
    outstanding = legal.outstanding_documents(session, context.user_id)
    channel = repo.get_channel(session, context.user_id, "telegram")
    documents = repo.list_documents(session, context.user_id)
    rule_count = len(context.principal.policy_rules) if context.principal is not None else 0

    actual: dict[str, bool] = {
        "legal": not outstanding,
        "policy": bool(stored.get("policy")) and rule_count > 0,
        "escalation": bool(channel is not None and channel.verified) or bool(stored.get("escalation_skipped")),
        "documents": bool(documents) or bool(stored.get("documents_skipped")),
    }
    details: dict[str, str | None] = {
        "legal": None if not outstanding else f"outstanding: {', '.join(outstanding)}",
        "policy": f"{rule_count} rule(s) in your Representation Policy",
        "escalation": (
            "connected"
            if channel is not None and channel.verified
            else ("skipped" if stored.get("escalation_skipped") else "not connected")
        ),
        "documents": f"{len(documents)} document(s) uploaded" if documents else "nothing uploaded yet",
    }
    steps = [
        OnboardingStep(
            key=key,
            title=_TITLES[key][0],
            description=_TITLES[key][1],
            complete=actual[key],
            optional=key in _OPTIONAL,
            detail=details.get(key),
        )
        for key in ONBOARDING_STEPS
    ]
    # Persist what is now demonstrably true, so the flow does not re-ask.
    repo.set_onboarding(session, user, **{k: v for k, v in actual.items()})
    next_step = next((s.key for s in steps if not s.complete), None)
    return OnboardingState(
        complete=next_step is None,
        next_step=next_step,
        steps=steps,
        account=account_response(session, user),
        telegram=_telegram_link(context, request),
    )


@router.get("", response_model=OnboardingState)
def state(request: Request, context: CurrentContext) -> OnboardingState:
    """Every step and what is left to do."""
    return _state(context, request)


@router.get("/templates")
def templates() -> list[dict]:
    """The starter Representation Policies offered at signup and here."""
    return describe_templates()


@router.post("/policy", response_model=OnboardingState)
def choose_policy(body: PolicyChoice, request: Request, context: ActiveContext) -> OnboardingState:
    """Install a starter policy, or confirm the one already installed."""
    if context.principal is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "this account has no principal to give a policy to")
    if body.template:
        if body.template not in TEMPLATES:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"unknown template {body.template!r}; choose one of {', '.join(TEMPLATES)}",
            )
        profile = build_profile(
            slug=context.principal.slug,
            display_name=context.principal.display_name,
            aliases=list(context.principal.aliases or []),
            template=body.template,
        )
        repo.replace_policy(
            context.session,
            context.principal,
            display_name=profile.display_name,
            aliases=profile.aliases,
            rules=[
                {
                    "topic": r.topic,
                    "tier": r.tier.value,
                    "description": r.description,
                    "triggers": list(r.triggers),
                }
                for r in profile.policy
            ],
            knowledge=[
                {
                    "topic": e.topic,
                    "subject": e.subject,
                    "keywords": list(e.keywords),
                    "answer": e.answer,
                }
                for e in profile.knowledge_base
            ],
        )
        if context.workspace is not None:
            context.workspace.invalidate_profile(context.session)
        logger.info("user %s installed the %s policy template", context.user.email, body.template)
    repo.set_onboarding(context.session, context.user, policy=True)
    return _state(context, request)


@router.post("/telegram", response_model=TelegramLink)
def telegram_code(request: Request, context: CurrentContext) -> TelegramLink:
    """Mint a fresh one-time code for linking a Telegram chat."""
    channel = repo.get_channel(context.session, context.user_id, "telegram")
    if channel is not None and channel.verified and channel.address:
        return _telegram_link(context, request)
    issue_link_code(context.session, context.user_id)
    context.session.flush()
    return _telegram_link(context, request)


@router.post("/skip", response_model=OnboardingState)
def skip(body: SkipRequest, request: Request, context: CurrentContext) -> OnboardingState:
    """Skip an optional step, recording that it was a choice rather than an omission."""
    if body.step not in _OPTIONAL and body.step != "documents":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"the {body.step!r} step cannot be skipped",
        )
    repo.set_onboarding(context.session, context.user, **{f"{body.step}_skipped": True})
    logger.info("user %s skipped the %s step", context.user.email, body.step)
    return _state(context, request)


@router.post("/complete", response_model=OnboardingState)
def complete(request: Request, context: CurrentContext) -> OnboardingState:
    """Mark onboarding finished. Refuses while a required step is outstanding."""
    current = _state(context, request)
    required_outstanding = [s.key for s in current.steps if not s.complete and not s.optional]
    if required_outstanding:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"finish these first: {', '.join(required_outstanding)}",
        )
    repo.set_onboarding(context.session, context.user, **{s.key: True for s in current.steps})
    return _state(context, request)
