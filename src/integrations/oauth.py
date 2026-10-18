"""
The shared Authorization Code flow every OAuth connector is built on.

One operator-registered app per provider - its client id and secret come
from environment variables, exactly like ``TELEGRAM_BOT_TOKEN``
(:class:`~src.escalation.telegram_bot.TelegramSettings`) - so an end user
never sees or handles an OAuth client secret; they only ever see the
provider's own consent screen. :class:`OAuthConnector` implements the parts
that are identical for every provider (building the authorize URL, the
token-endpoint POST, refreshing); each concrete connector supplies only its
endpoints, scope, and whatever response-shape quirks it has.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from src.integrations.base import (
    BriefingContent,
    Connector,
    ConnectorAuthError,
    ConnectorMeta,
    ConnectorStatus,
)

logger = logging.getLogger("deleg8.integrations.oauth")


def build_oauth_authorize_url(
    *,
    authorize_url: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    extra_params: dict[str, str] | None = None,
) -> str:
    """The URL to send the browser to for a provider's consent screen.

    Shared by every OAuth flow in this codebase - connectors
    (:class:`OAuthConnector`) and the "Continue with Google/Microsoft" login
    providers (:mod:`src.auth.oauth_identity`) alike.
    """
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "state": state,
        **(extra_params or {}),
    }
    return f"{authorize_url}?{urlencode(params)}"


async def post_oauth_token_request(
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    auth_style: Literal["body", "basic"],
    extra: dict[str, str],
    label: str,
) -> dict[str, Any]:
    """POST to a provider's token endpoint. Shared by every OAuth flow in this codebase."""
    data = dict(extra)
    auth = None
    if auth_style == "basic":
        auth = (client_id, client_secret)
    else:
        data["client_id"] = client_id
        data["client_secret"] = client_secret
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(token_url, data=data, auth=auth, headers={"Accept": "application/json"})
    if response.status_code >= 400:
        raise ConnectorAuthError(f"{label} rejected the token request (HTTP {response.status_code}): {response.text[:200]}")
    return response.json()


def expiry_config_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Project a token response's ``expires_in``/``refresh_token`` onto stored config fields.

    A free function (rather than only a method on :class:`OAuthConnector`) so
    :mod:`src.calendar.providers` - which is deliberately not an
    ``OAuthConnector`` subclass, for the same reason
    :mod:`src.auth.oauth_identity` is not one - can reuse it too.
    """
    config: dict[str, Any] = {}
    if "refresh_token" in payload:
        config["refresh_token"] = payload["refresh_token"]
    if "expires_in" in payload:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(payload["expires_in"]))
        config["expires_at"] = expires_at.isoformat()
    return config


async def refresh_oauth_token_if_needed(
    token: str,
    config: dict[str, Any],
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    auth_style: Literal["body", "basic"],
    label: str,
) -> tuple[str, dict[str, Any]]:
    """Refresh an access token if it is expired or about to be. No-op when there is nothing to refresh."""
    expires_at = config.get("expires_at")
    refresh_token = config.get("refresh_token")
    if not expires_at or not refresh_token:
        return token, config
    if datetime.fromisoformat(expires_at) - datetime.now(timezone.utc) > timedelta(seconds=60):
        return token, config
    payload = await post_oauth_token_request(
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
        auth_style=auth_style,
        extra={"grant_type": "refresh_token", "refresh_token": refresh_token},
        label=label,
    )
    new_token = payload.get("access_token", token)
    new_config = {**config, **expiry_config_from_payload(payload)}
    new_config.setdefault("refresh_token", refresh_token)
    return new_token, new_config


@dataclass(frozen=True)
class OAuthProviderConfig:
    """Everything that differs between one OAuth provider and another."""

    authorize_url: str
    token_url: str
    scope: str
    client_id_env: str
    client_secret_env: str
    extra_authorize_params: dict[str, str] = field(default_factory=dict)
    token_auth_style: Literal["body", "basic"] = "body"
    """Most providers take ``client_id``/``client_secret`` in the POST body; a few require HTTP Basic auth instead."""


class OAuthConnector(Connector):
    """Base for a connector that authenticates via the Authorization Code flow."""

    meta: ConnectorMeta
    provider: OAuthProviderConfig

    def client_credentials(self) -> tuple[str, str] | None:
        """``(client_id, client_secret)`` from the operator's environment, or ``None`` if not configured."""
        client_id = os.getenv(self.provider.client_id_env, "").strip()
        client_secret = os.getenv(self.provider.client_secret_env, "").strip()
        if not client_id or not client_secret:
            return None
        return client_id, client_secret

    def is_configured(self) -> bool:
        """True when this deployment's operator has registered an app for this provider."""
        return self.client_credentials() is not None

    def is_available(self) -> bool:
        """An OAuth connector is only available once the operator has configured its app."""
        return self.is_configured()

    def build_authorize_url(self, *, redirect_uri: str, state: str) -> str:
        """The URL to send the browser to for the provider's consent screen."""
        creds = self.client_credentials()
        if creds is None:
            raise ConnectorAuthError(f"{self.meta.label} is not configured on this server yet")
        client_id, _secret = creds
        return build_oauth_authorize_url(
            authorize_url=self.provider.authorize_url,
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=self.provider.scope,
            state=state,
            extra_params=self.provider.extra_authorize_params,
        )

    async def _post_token_request(self, *, extra: dict[str, str]) -> dict[str, Any]:
        """POST to the provider's token endpoint. Shared by the initial exchange and a refresh."""
        creds = self.client_credentials()
        if creds is None:
            raise ConnectorAuthError(f"{self.meta.label} is not configured on this server yet")
        client_id, client_secret = creds
        return await post_oauth_token_request(
            token_url=self.provider.token_url,
            client_id=client_id,
            client_secret=client_secret,
            auth_style=self.provider.token_auth_style,
            extra=extra,
            label=self.meta.label,
        )

    async def exchange_code(self, *, code: str, redirect_uri: str) -> tuple[str, dict[str, Any]]:
        """Exchange an authorization code for an access token. Returns ``(access_token, config)``."""
        payload = await self._post_token_request(
            extra={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        )
        token = payload.get("access_token")
        if not token:
            raise ConnectorAuthError(f"{self.meta.label} did not return an access token")
        return token, expiry_config_from_payload(payload)

    async def refresh_if_needed(self, token: str, config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Refresh the access token if it is expired or about to be. No-op when there is nothing to refresh."""
        creds = self.client_credentials()
        if creds is None:
            raise ConnectorAuthError(f"{self.meta.label} is not configured on this server yet")
        client_id, client_secret = creds
        return await refresh_oauth_token_if_needed(
            token, config,
            token_url=self.provider.token_url,
            client_id=client_id,
            client_secret=client_secret,
            auth_style=self.provider.token_auth_style,
            label=self.meta.label,
        )

    async def send_briefing(
        self, token: str | None, config: dict[str, Any], briefing: BriefingContent
    ) -> ConnectorStatus:
        """Most OAuth connectors here are read or task-creation connectors, not briefing destinations."""
        return ConnectorStatus(ok=False, detail=f"{self.meta.label} does not receive briefings")
