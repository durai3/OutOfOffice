"""
``/api/v1/integrations`` - connect, configure, and disconnect third-party connectors.

    GET    /api/v1/integrations                      every known connector, plus this account's status
    POST   /api/v1/integrations/{type}/authorize      connect by pasting a credential (webhook URL / API key)
    GET    /api/v1/integrations/{type}/oauth/start    begin an OAuth connection: returns the provider's consent URL
    GET    /api/v1/integrations/{type}/oauth/callback the provider redirects the browser here; not session-authed
    GET    /api/v1/integrations/{type}/options        follow-up choices after connecting (project, calendar, ...)
    PATCH  /api/v1/integrations/{type}                toggle on/off, update config
    DELETE /api/v1/integrations/{type}                revoke and remove

A connector's auth token and config are encrypted at rest
(:func:`src.auth.security.encrypt_secret`) and the token is never returned by
any of these endpoints - only whether one is stored.

The OAuth callback is a plain browser redirect from the provider with no
``Authorization`` header, so unlike every other route here it cannot use
:data:`~src.auth.deps.CurrentContext`. It trusts only the signed ``state``
token minted by ``oauth/start`` (:func:`src.auth.security.create_oauth_state_token`)
to identify who initiated the connection - see that function's docstring for
why it is deliberately not the same token type as a login session.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from src.auth.deps import CurrentContext
from src.auth.security import (
    AuthError,
    create_oauth_state_token,
    decode_oauth_state_token,
    decrypt_secret,
    encrypt_secret,
)
from src.db import repo
from src.db.base import CommittingRoute, run_db
from src.db.models import Integration
from src.integrations.base import Connector, ConnectorAuthError
from src.integrations.oauth import OAuthConnector
from src.integrations.registry import all_connector_meta, get_connector

logger = logging.getLogger("deleg8.api.integrations")

router = APIRouter(prefix="/api/v1/integrations", tags=["integrations"], route_class=CommittingRoute)


def _base_url() -> str:
    """This server's own externally-reachable URL, for building an OAuth redirect_uri."""
    return os.getenv("DELEG8_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def _redirect_uri(connector_type: str) -> str:
    return f"{_base_url()}/api/v1/integrations/{connector_type}/oauth/callback"


class ConnectorFieldOut(BaseModel):
    """One credential/config field a connector's console form should show."""

    name: str
    label: str
    secret: bool
    placeholder: str = ""


class ConnectorSummary(BaseModel):
    """A connector type, merged with this account's connection to it (if any)."""

    connector_type: str
    label: str
    auth_mode: str
    available: bool
    unavailable_reason: str | None = None
    docs_url: str = ""
    fields: list[ConnectorFieldOut] = Field(default_factory=list)
    is_connected: bool
    is_enabled: bool
    config: dict[str, Any] = Field(default_factory=dict)
    last_synced_at: datetime | None = None
    last_error: str | None = None


class AuthorizeRequest(BaseModel):
    """Connect a connector: a pasted credential and/or non-secret config."""

    credential: str | None = Field(default=None, description="Webhook URL, API key, or OAuth code.")
    config: dict[str, Any] = Field(default_factory=dict)


class PatchRequest(BaseModel):
    """Toggle a connector on/off, or update its non-secret config."""

    is_enabled: bool | None = None
    config: dict[str, Any] | None = None


def _connector_or_404(connector_type: str) -> Connector:
    connector = get_connector(connector_type)
    if connector is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown connector type {connector_type!r}")
    return connector


def _decrypt_config(row: Integration | None) -> dict[str, Any]:
    if row is None or not row.config_encrypted:
        return {}
    try:
        return json.loads(decrypt_secret(row.config_encrypted))
    except (AuthError, json.JSONDecodeError):
        logger.error("could not decrypt stored config for integration %s", row.id)
        return {}


def _summary(connector: Connector, row: Integration | None) -> ConnectorSummary:
    meta = connector.meta
    return ConnectorSummary(
        connector_type=meta.connector_type,
        label=meta.label,
        auth_mode=meta.auth_mode.value,
        available=connector.is_available(),
        unavailable_reason=meta.unavailable_reason,
        docs_url=meta.docs_url,
        fields=[ConnectorFieldOut(name=f.name, label=f.label, secret=f.secret, placeholder=f.placeholder) for f in meta.fields],
        is_connected=bool(row and row.auth_token_encrypted),
        is_enabled=bool(row and row.is_enabled),
        config=_decrypt_config(row),
        last_synced_at=row.last_synced_at if row else None,
        last_error=row.last_error if row else None,
    )


@router.get("", response_model=list[ConnectorSummary])
def list_integrations(context: CurrentContext) -> list[ConnectorSummary]:
    """Every known connector type, merged with this account's connection to it."""
    rows = {row.connector_type: row for row in repo.list_integrations(context.session, context.user_id)}
    return [_summary(get_connector(meta.connector_type), rows.get(meta.connector_type)) for meta in all_connector_meta()]


@router.post("/{connector_type}/authorize", response_model=ConnectorSummary)
async def authorize(connector_type: str, body: AuthorizeRequest, context: CurrentContext) -> ConnectorSummary:
    """Connect a connector: validate the credential, then store it encrypted."""
    connector = _connector_or_404(connector_type)
    existing = repo.get_integration(context.session, context.user_id, connector_type)
    merged_config = {**_decrypt_config(existing), **body.config}
    try:
        token, config = await connector.authorize(body.credential, merged_config)
    except ConnectorAuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    row = repo.upsert_integration(
        context.session,
        user_id=context.user_id,
        connector_type=connector_type,
        auth_token_encrypted=encrypt_secret(token) if token else None,
        config_encrypted=encrypt_secret(json.dumps(config)) if config else None,
        is_enabled=True,
        last_error=None,
    )
    logger.info("user %s connected %s", context.user.email, connector_type)
    return _summary(connector, row)


class OAuthStartResponse(BaseModel):
    """Where to send the browser to begin an OAuth connection."""

    redirect_url: str


@router.get("/{connector_type}/oauth/start", response_model=OAuthStartResponse)
def oauth_start(
    connector_type: str,
    context: CurrentContext,
    origin: str = Query(..., description="window.location.origin of the console tab that opened the popup."),
) -> OAuthStartResponse:
    """Begin an OAuth connection: mint a state token and return the provider's consent URL."""
    connector = _connector_or_404(connector_type)
    if not isinstance(connector, OAuthConnector):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{connector.meta.label} does not use OAuth")
    if not connector.is_available():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, connector.meta.unavailable_reason or f"{connector.meta.label} is not configured"
        )
    state = create_oauth_state_token(context.user_id, connector_type, origin)
    url = connector.build_authorize_url(redirect_uri=_redirect_uri(connector_type), state=state)
    return OAuthStartResponse(redirect_url=url)


def _oauth_result_page(*, ok: bool, connector_type: str, origin: str, message: str = "") -> HTMLResponse:
    """A minimal page that hands the result back to the window that opened the popup, then closes."""
    safe_origin = origin if origin.startswith(("http://", "https://")) else "*"
    payload = json.dumps({"source": "deleg8-oauth", "connector_type": connector_type, "ok": ok, "message": message})
    status_line = "Connected. You can close this window." if ok else f"Could not connect: {message}"
    body = f"""<!doctype html><html><body style="font-family:sans-serif;padding:2rem;color:#1a1a1a">
<p>{status_line}</p>
<script>
  if (window.opener) {{ window.opener.postMessage({payload}, {json.dumps(safe_origin)}); }}
  window.close();
</script>
</body></html>"""
    return HTMLResponse(body, status_code=200 if ok else 400)


@router.get("/{connector_type}/oauth/callback", response_class=HTMLResponse, include_in_schema=False)
async def oauth_callback(
    connector_type: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> HTMLResponse:
    """The provider redirects the browser here after consent. Trusts only the signed ``state``."""
    connector = get_connector(connector_type)
    if connector is None or not isinstance(connector, OAuthConnector):
        return _oauth_result_page(ok=False, connector_type=connector_type, origin="*", message="unknown connector")
    if state is None:
        return _oauth_result_page(ok=False, connector_type=connector_type, origin="*", message="missing state")
    try:
        claims = decode_oauth_state_token(state)
    except AuthError as exc:
        return _oauth_result_page(ok=False, connector_type=connector_type, origin="*", message=str(exc))
    origin = claims.get("origin", "*")
    if claims.get("connector_type") != connector_type:
        return _oauth_result_page(ok=False, connector_type=connector_type, origin=origin, message="state mismatch")
    if error or not code:
        return _oauth_result_page(
            ok=False, connector_type=connector_type, origin=origin, message=error_description or error or "denied"
        )
    try:
        token, config = await connector.exchange_code(code=code, redirect_uri=_redirect_uri(connector_type))
    except ConnectorAuthError as exc:
        return _oauth_result_page(ok=False, connector_type=connector_type, origin=origin, message=str(exc))
    user_id = uuid.UUID(claims["sub"])

    def _store(session: Any) -> None:
        repo.upsert_integration(
            session,
            user_id=user_id,
            connector_type=connector_type,
            auth_token_encrypted=encrypt_secret(token),
            config_encrypted=encrypt_secret(json.dumps(config)) if config else None,
            is_enabled=True,
            last_error=None,
        )

    await run_db(_store)
    logger.info("user %s connected %s via OAuth", user_id, connector_type)
    return _oauth_result_page(ok=True, connector_type=connector_type, origin=origin)


@router.get("/{connector_type}/options", response_model=list[dict[str, str]])
async def connector_options(connector_type: str, context: CurrentContext) -> list[dict[str, str]]:
    """Follow-up choices after connecting (which project/board/calendar), if this connector has any."""
    connector = _connector_or_404(connector_type)
    row = repo.get_integration(context.session, context.user_id, connector_type)
    if row is None or not row.auth_token_encrypted:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{connector.meta.label} is not connected yet")
    token = decrypt_secret(row.auth_token_encrypted)
    options = await connector.list_options(token, _decrypt_config(row))
    return options or []


@router.patch("/{connector_type}", response_model=ConnectorSummary)
def patch_integration(connector_type: str, body: PatchRequest, context: CurrentContext) -> ConnectorSummary:
    """Toggle a connector on/off, or update its non-secret config, without re-authorizing."""
    connector = _connector_or_404(connector_type)
    row = repo.get_integration(context.session, context.user_id, connector_type)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{connector_type} is not connected yet")
    values: dict[str, Any] = {}
    if body.is_enabled is not None:
        if body.is_enabled and not row.auth_token_encrypted:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"connect {connector.meta.label} before enabling it")
        values["is_enabled"] = body.is_enabled
    if body.config is not None:
        merged = {**_decrypt_config(row), **body.config}
        values["config_encrypted"] = encrypt_secret(json.dumps(merged))
    row = repo.upsert_integration(context.session, user_id=context.user_id, connector_type=connector_type, **values)
    return _summary(connector, row)


@router.delete("/{connector_type}", status_code=status.HTTP_204_NO_CONTENT)
def delete_integration(connector_type: str, context: CurrentContext) -> None:
    """Revoke and remove one connector's connection."""
    _connector_or_404(connector_type)
    repo.delete_integration(context.session, context.user_id, connector_type)
    logger.info("user %s disconnected %s", context.user.email, connector_type)
