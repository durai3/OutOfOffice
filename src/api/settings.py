"""
``/api/v1/settings`` - keys, notification channel, consent preference, plan.

    GET    /api/v1/settings                      everything this page shows
    PUT    /api/v1/settings/preferences          consent mode, escalation timeout
    GET    /api/v1/settings/credentials          which providers have a key, and whose
    PUT    /api/v1/settings/credentials/{p}      store one of your own keys (verified first)
    DELETE /api/v1/settings/credentials/{p}      remove it and fall back to the platform key
    POST   /api/v1/settings/telegram/code        mint a link code
    POST   /api/v1/settings/telegram/bot         use your own bot instead of the shared one
    DELETE /api/v1/settings/telegram             unlink

A stored key is verified with one real call before it is saved. A wrong key
that is accepted here fails later, in the middle of a live meeting, which is
the worst possible place to discover it.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from src.auth.deps import ActiveContext, CurrentContext
from src.auth.security import encrypt_secret, secret_hint
from src.compliance.consent import describe_modes, mode_from_value
from src.db import repo
from src.db.base import CommittingRoute
from src.db.models import ConsentMode
from src.escalation.hub import issue_link_code
from src.tenancy import credentials as creds
from src.usage.meter import PLANS, plan_for_context, quota_status

logger = logging.getLogger("deleg8.api.settings")

router = APIRouter(prefix="/api/v1/settings", tags=["settings"], route_class=CommittingRoute)


class CredentialSummary(BaseModel):
    """One provider: whether there is a key, and whose it is."""

    provider: str
    label: str
    help_url: str
    configured: bool
    own_key: bool = Field(..., description="True when this is the user's own key, not the platform's.")
    hint: str = Field(default="", description="Last four characters of a stored key.")
    verified_at: datetime | None = None
    verify_error: str | None = None


class ChannelSummary(BaseModel):
    """Where this account's escalations and briefings go."""

    kind: str = "telegram"
    linked: bool
    own_bot: bool = Field(default=False, description="True when the user runs their own bot.")
    label: str | None = None
    bot_username: str | None = None
    link_code: str | None = None
    link_code_expires_at: datetime | None = None
    timeout_seconds: float | None = None
    describe: str = ""


class Preferences(BaseModel):
    """Per-account behaviour the console can change."""

    consent_mode: str = ConsentMode.NOTIFY_ONLY.value
    escalation_timeout_seconds: float = 30.0
    plan: str = "beta"


class PreferencesUpdate(BaseModel):
    """``PUT /api/v1/settings/preferences``."""

    consent_mode: str | None = Field(
        default=None, description="notify_only, all_party, or single_party."
    )
    escalation_timeout_seconds: float | None = Field(default=None, ge=5.0, le=300.0)

    @field_validator("consent_mode")
    @classmethod
    def _known_mode(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in {m.value for m in ConsentMode}:
            raise ValueError(f"unknown consent mode {value!r}")
        return value


class SettingsResponse(BaseModel):
    """Everything the Settings page renders."""

    preferences: Preferences
    consent_modes: list[dict[str, str]]
    credentials: list[CredentialSummary]
    channel: ChannelSummary
    plan_name: str
    plan_describe: str
    using_own_keys: bool = Field(
        ..., description="True when nothing this account runs is on the platform's keys."
    )
    usage_fractions: dict[str, float]


class CredentialRequest(BaseModel):
    """``PUT /api/v1/settings/credentials/{provider}``."""

    key: str = Field(..., min_length=8, max_length=400)
    verify: bool = Field(default=True, description="Make one real call to check the key first.")


class OwnBotRequest(BaseModel):
    """``POST /api/v1/settings/telegram/bot``."""

    bot_token: str = Field(..., min_length=20, max_length=200, description="Token from @BotFather.")
    chat_id: str = Field(..., min_length=1, max_length=40, description="Your numeric Telegram chat id.")

    @field_validator("chat_id")
    @classmethod
    def _numeric(cls, value: str) -> str:
        if not value.strip().lstrip("-").isdigit():
            raise ValueError("chat_id must be a number; ask @userinfobot for yours")
        return value.strip()


def _credentials(context: CurrentContext) -> list[CredentialSummary]:
    stored = {row.provider: row for row in repo.list_credentials(context.session, context.user_id)}
    resolved = creds.resolve(context.session, context.user_id)
    summaries: list[CredentialSummary] = []
    for provider in creds.PLATFORM_ENV:
        row = stored.get(provider)
        key = resolved.get(provider)
        summaries.append(
            CredentialSummary(
                provider=provider,
                label=creds.PROVIDER_LABELS[provider],
                help_url=creds.PROVIDER_HELP[provider],
                configured=key.present,
                own_key=key.own_key,
                hint=row.hint if row is not None else "",
                verified_at=row.verified_at if row is not None else None,
                verify_error=row.verify_error if row is not None else None,
            )
        )
    return summaries


def _channel(context: CurrentContext, request: Request) -> ChannelSummary:
    channel = repo.get_channel(context.session, context.user_id, "telegram")
    hub = getattr(request.app.state, "hub", None)
    username = getattr(hub, "bot_username", None) if hub is not None else None
    workspace = context.workspace
    describe = workspace.bridge.describe() if workspace is not None else "not loaded"
    if channel is None:
        return ChannelSummary(linked=False, bot_username=username, describe=describe)
    return ChannelSummary(
        linked=bool(channel.verified and channel.address),
        own_bot=bool(channel.bot_token_encrypted),
        label=channel.label,
        bot_username=username,
        link_code=channel.link_code,
        link_code_expires_at=channel.link_code_expires_at,
        timeout_seconds=channel.timeout_seconds,
        describe=describe,
    )


def _preferences(context: CurrentContext) -> Preferences:
    settings = dict(context.user.settings or {})
    return Preferences(
        consent_mode=mode_from_value(settings.get("consent_mode")).value,
        escalation_timeout_seconds=float(settings.get("escalation_timeout_seconds") or 30.0),
        plan=str(settings.get("plan") or "beta"),
    )


def _response(context: CurrentContext, request: Request) -> SettingsResponse:
    resolved = creds.resolve(context.session, context.user_id)
    own_keys = not (resolved.uses_platform_llm or resolved.uses_platform_voice)
    org_settings = context.org.settings if context.org is not None else None
    plan = PLANS["unlimited"] if own_keys else plan_for_context(context.user.settings, org_settings)
    status_now = quota_status(context.session, context.user_id, own_keys=own_keys, org_settings=org_settings)
    return SettingsResponse(
        preferences=_preferences(context),
        consent_modes=describe_modes(),
        credentials=_credentials(context),
        channel=_channel(context, request),
        plan_name=plan.name,
        plan_describe=plan.describe(),
        using_own_keys=own_keys,
        usage_fractions=status_now.fractions,
    )


@router.get("", response_model=SettingsResponse)
def read_settings(request: Request, context: CurrentContext) -> SettingsResponse:
    """Everything the Settings page renders."""
    return _response(context, request)


@router.put("/preferences", response_model=SettingsResponse)
async def update_preferences(
    body: PreferencesUpdate, request: Request, context: ActiveContext
) -> SettingsResponse:
    """Change the default consent mode or the escalation timeout."""
    values: dict[str, object] = {}
    if body.consent_mode is not None:
        values["consent_mode"] = body.consent_mode
    if body.escalation_timeout_seconds is not None:
        values["escalation_timeout_seconds"] = body.escalation_timeout_seconds
        repo.upsert_channel(
            context.session,
            user_id=context.user_id,
            kind="telegram",
            timeout_seconds=body.escalation_timeout_seconds,
        )
    if values:
        repo.update_settings(context.session, context.user, **values)
        context.session.flush()
    if context.workspace is not None:
        context.workspace.invalidate_profile(context.session)
        await context.workspace.refresh_channel(context.session)
    return _response(context, request)


@router.get("/credentials", response_model=list[CredentialSummary])
def list_keys(context: CurrentContext) -> list[CredentialSummary]:
    """Which providers have a key, and whether it is yours or the platform's."""
    return _credentials(context)


@router.put(
    "/credentials/{provider}",
    response_model=list[CredentialSummary],
    responses={400: {"description": "the provider rejected the key"}},
)
async def store_key(provider: str, body: CredentialRequest, context: ActiveContext) -> list[CredentialSummary]:
    """Store one of your own API keys, encrypted, after checking it works."""
    if provider not in creds.PLATFORM_ENV:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"unknown provider {provider!r}; expected one of {', '.join(creds.PLATFORM_ENV)}",
        )
    detail = "not verified"
    if body.verify:
        ok, detail = await creds.verify_key(provider, body.key)
        if not ok:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{provider}: {detail}")
    row = repo.upsert_credential(
        context.session,
        user_id=context.user_id,
        provider=provider,
        secret_encrypted=encrypt_secret(body.key.strip()),
        hint=secret_hint(body.key),
    )
    if body.verify:
        from src.db.models import utcnow

        row.verified_at = utcnow()
        row.verify_error = None
    context.session.flush()
    if context.workspace is not None:
        await context.workspace.refresh_credentials(context.session)
    logger.info("user %s stored their own %s key (%s)", context.user.email, provider, detail)
    return _credentials(context)


@router.delete("/credentials/{provider}", response_model=list[CredentialSummary])
async def delete_key(provider: str, context: ActiveContext) -> list[CredentialSummary]:
    """Remove your key for a provider; calls fall back to the platform key and its quota."""
    if not repo.delete_credential(context.session, context.user_id, provider):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no stored {provider} key")
    context.session.flush()
    if context.workspace is not None:
        await context.workspace.refresh_credentials(context.session)
    return _credentials(context)


@router.post("/telegram/code", response_model=ChannelSummary)
def telegram_code(request: Request, context: CurrentContext) -> ChannelSummary:
    """Mint a one-time code to link a Telegram chat to this account."""
    issue_link_code(context.session, context.user_id)
    context.session.flush()
    return _channel(context, request)


@router.post("/telegram/bot", response_model=ChannelSummary)
async def use_own_bot(body: OwnBotRequest, request: Request, context: ActiveContext) -> ChannelSummary:
    """Use your own Telegram bot instead of the shared one.

    Worth doing if you would rather DELEG8's operator had no visibility of the
    bot your approvals pass through; the trade is that you run the bot.
    """
    repo.upsert_channel(
        context.session,
        user_id=context.user_id,
        kind="telegram",
        address=body.chat_id,
        bot_token_encrypted=encrypt_secret(body.bot_token.strip()),
        verified=True,
        link_code=None,
        link_code_expires_at=None,
        label="your own bot",
    )
    repo.set_onboarding(context.session, context.user, escalation=True)
    context.session.flush()
    if context.workspace is not None:
        await context.workspace.refresh_channel(context.session)
    hub = getattr(request.app.state, "hub", None)
    if hub is not None:
        hub.reload_routes()
    return _channel(context, request)


@router.delete("/telegram", response_model=ChannelSummary)
async def unlink_telegram(request: Request, context: ActiveContext) -> ChannelSummary:
    """Disconnect the notification channel. Escalations fall back to the holding reply."""
    repo.upsert_channel(
        context.session,
        user_id=context.user_id,
        kind="telegram",
        address=None,
        bot_token_encrypted=None,
        verified=False,
        link_code=None,
        link_code_expires_at=None,
        label=None,
    )
    repo.set_onboarding(context.session, context.user, escalation=False)
    context.session.flush()
    if context.workspace is not None:
        await context.workspace.refresh_channel(context.session)
    hub = getattr(request.app.state, "hub", None)
    if hub is not None:
        hub.reload_routes()
    return _channel(context, request)
