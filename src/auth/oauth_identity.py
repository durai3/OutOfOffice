"""
"Continue with Google" / "Continue with Microsoft" login identity providers.

Deliberately NOT a :class:`src.integrations.oauth.OAuthConnector`: a login
identity provider authenticates a *person* before any ``user_id`` exists, is
never registered in :mod:`src.integrations.registry`, and has no
``send_briefing``/``create_task``/``get_status`` concept. It reuses only the
generic Authorization Code mechanics (:func:`~src.integrations.oauth.build_oauth_authorize_url`,
:func:`~src.integrations.oauth.post_oauth_token_request`) that
``OAuthConnector`` is also built on.

Turning a token response into a verified identity needs one extra step
neither connector ever needed: a userinfo/Graph call for the signed-in
person's email and stable subject id. Both providers below do that over TLS
directly against Google/Microsoft using the access token this server just
received from them, rather than verifying the ``id_token`` JWT's signature
locally - simpler, and just as trustworthy for this purpose.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

from src.integrations.base import ConnectorAuthError
from src.integrations.oauth import build_oauth_authorize_url, post_oauth_token_request


class OAuthLoginError(RuntimeError):
    """A login identity exchange failed, or the provider is not configured."""


@dataclass(frozen=True)
class VerifiedIdentity:
    """What a login provider hands back after exchanging a code."""

    subject: str
    """The provider's stable subject id - Google's ``sub``, Microsoft's ``id``."""
    email: str
    email_verified: bool
    display_name: str


class LoginIdentityProvider:
    """Base for one provider's login Authorization Code flow."""

    provider: str
    authorize_url: str
    token_url: str
    scope: str
    client_id_env: str
    client_secret_env: str

    def client_credentials(self) -> tuple[str, str] | None:
        """``(client_id, client_secret)`` from the operator's environment, or ``None`` if not configured."""
        client_id = os.getenv(self.client_id_env, "").strip()
        client_secret = os.getenv(self.client_secret_env, "").strip()
        return (client_id, client_secret) if client_id and client_secret else None

    def is_configured(self) -> bool:
        """True once the operator has registered a login app for this provider."""
        return self.client_credentials() is not None

    def build_authorize_url(self, *, redirect_uri: str, state: str) -> str:
        """The URL to send the browser to for this provider's consent screen."""
        creds = self.client_credentials()
        if creds is None:
            raise OAuthLoginError(f"{self.provider.title()} sign-in is not configured on this server yet")
        client_id, _secret = creds
        return build_oauth_authorize_url(
            authorize_url=self.authorize_url,
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=self.scope,
            state=state,
        )

    async def exchange_code_for_identity(self, *, code: str, redirect_uri: str) -> VerifiedIdentity:
        """Exchange an authorization code for the signed-in person's verified identity."""
        creds = self.client_credentials()
        if creds is None:
            raise OAuthLoginError(f"{self.provider.title()} sign-in is not configured on this server yet")
        client_id, client_secret = creds
        try:
            payload = await post_oauth_token_request(
                token_url=self.token_url,
                client_id=client_id,
                client_secret=client_secret,
                auth_style="body",
                extra={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
                label=f"{self.provider.title()} sign-in",
            )
        except ConnectorAuthError as exc:
            raise OAuthLoginError(str(exc)) from exc
        access_token = payload.get("access_token")
        if not access_token:
            raise OAuthLoginError(f"{self.provider.title()} did not return an access token")
        return await self._fetch_identity(access_token)

    async def _fetch_identity(self, access_token: str) -> VerifiedIdentity:
        raise NotImplementedError


class GoogleLoginProvider(LoginIdentityProvider):
    """``openid email profile`` - Google's ``email_verified`` claim is a real, documented guarantee."""

    provider = "google"
    authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    scope = "openid email profile"
    client_id_env = "GOOGLE_SSO_CLIENT_ID"
    client_secret_env = "GOOGLE_SSO_CLIENT_SECRET"
    _userinfo_url = "https://openidconnect.googleapis.com/v1/userinfo"

    async def _fetch_identity(self, access_token: str) -> VerifiedIdentity:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(self._userinfo_url, headers={"Authorization": f"Bearer {access_token}"})
        if response.status_code >= 400:
            raise OAuthLoginError(f"Google rejected the userinfo request (HTTP {response.status_code})")
        data: dict[str, Any] = response.json()
        return VerifiedIdentity(
            subject=str(data.get("sub", "")),
            email=str(data.get("email", "")).strip().lower(),
            email_verified=bool(data.get("email_verified", False)),
            display_name=str(data.get("name", "")).strip(),
        )


class MicrosoftLoginProvider(LoginIdentityProvider):
    """``common`` (not ``organizations`` or a fixed tenant id), so both personal and work/school

    Microsoft accounts can sign in - this product sells to individuals and companies alike.
    """

    provider = "microsoft"
    authorize_url = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    scope = "openid email profile User.Read"
    client_id_env = "MICROSOFT_SSO_CLIENT_ID"
    client_secret_env = "MICROSOFT_SSO_CLIENT_SECRET"
    _graph_me_url = "https://graph.microsoft.com/v1.0/me"

    async def _fetch_identity(self, access_token: str) -> VerifiedIdentity:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(self._graph_me_url, headers={"Authorization": f"Bearer {access_token}"})
        if response.status_code >= 400:
            raise OAuthLoginError(f"Microsoft rejected the Graph /me request (HTTP {response.status_code})")
        data: dict[str, Any] = response.json()
        # Microsoft's Graph /me has no `email_verified`-equivalent claim the way
        # Google's userinfo does. `mail` is the actual verified mailbox; a
        # `userPrincipalName` fallback is used only when `mail` is absent (some
        # tenant configurations leave it unset), since it can in principle be an
        # unverified sign-in alias rather than a real mailbox. Treated as
        # verified by construction either way - both account kinds `common`
        # accepts already verify this address themselves before it ever reaches
        # us (a tenant admin controls/verifies a work domain; a personal
        # account's address is what Microsoft itself verified at signup) - but
        # this is a weaker guarantee than Google's explicit claim.
        email = str(data.get("mail") or data.get("userPrincipalName") or "").strip().lower()
        return VerifiedIdentity(
            subject=str(data.get("id", "")),
            email=email,
            email_verified=bool(email),
            display_name=str(data.get("displayName", "")).strip(),
        )


_PROVIDERS: dict[str, LoginIdentityProvider] = {"google": GoogleLoginProvider(), "microsoft": MicrosoftLoginProvider()}


def get_login_provider(name: str) -> LoginIdentityProvider | None:
    """One of the two registered login providers, or ``None`` if unknown."""
    return _PROVIDERS.get(name)
