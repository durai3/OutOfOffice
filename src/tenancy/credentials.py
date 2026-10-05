"""
Which API key a given call uses, and who pays for it.

Every user of a beta shares one deployment, and in the single-tenant design
every one of them would have been spending the operator's Gemini, Deepgram and
Cartesia quota. Two answers, and this module implements both:

* **Bring your own key.** A user stores their own key (encrypted at rest,
  :mod:`src.auth.security`) and their calls go out on it. Those calls are
  metered but not counted against the platform quota, because they did not
  cost the platform anything.
* **Use the platform key, within a quota.** Metered and counted; when the
  monthly allowance is spent, dispatch is refused with a message naming the
  limit and offering the first option.

:class:`ResolvedCredentials` is what the rest of the system asks for. It
answers "what key, whose, and does this count?" for every provider, in one
object that can be handed to a worker container as environment variables.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from src.auth.security import AuthError, decrypt_secret, encrypt_secret, secret_hint
from src.db import repo
from src.db.models import CredentialProvider

logger = logging.getLogger("deleg8.tenancy.credentials")

PLATFORM_ENV: dict[str, str] = {
    CredentialProvider.GEMINI.value: "GEMINI_API_KEY",
    CredentialProvider.ANTHROPIC.value: "ANTHROPIC_API_KEY",
    CredentialProvider.DEEPGRAM.value: "DEEPGRAM_API_KEY",
    CredentialProvider.CARTESIA.value: "CARTESIA_API_KEY",
}
"""The environment variable each provider's platform key is read from."""

PROVIDER_LABELS: dict[str, str] = {
    CredentialProvider.GEMINI.value: "Google Gemini (language model)",
    CredentialProvider.ANTHROPIC.value: "Anthropic Claude (language model)",
    CredentialProvider.DEEPGRAM.value: "Deepgram (speech to text)",
    CredentialProvider.CARTESIA.value: "Cartesia (text to speech)",
}

PROVIDER_HELP: dict[str, str] = {
    CredentialProvider.GEMINI.value: "https://aistudio.google.com/apikey",
    CredentialProvider.ANTHROPIC.value: "https://console.anthropic.com/settings/keys",
    CredentialProvider.DEEPGRAM.value: "https://console.deepgram.com",
    CredentialProvider.CARTESIA.value: "https://play.cartesia.ai",
}


@dataclass(frozen=True)
class ProviderKey:
    """One resolved key: its value, and whether the user brought it."""

    provider: str
    value: str | None
    own_key: bool
    hint: str = ""

    @property
    def present(self) -> bool:
        """True when a usable key was found."""
        return bool(self.value)

    def describe(self) -> str:
        """One line for the console and the health page."""
        if not self.present:
            return f"{self.provider}: not configured"
        return f"{self.provider}: {'your key' if self.own_key else 'platform key'} {self.hint}".rstrip()


@dataclass(frozen=True)
class ResolvedCredentials:
    """Every provider key for one tenant, with ownership attached."""

    user_id: uuid.UUID
    keys: dict[str, ProviderKey] = field(default_factory=dict)

    def get(self, provider: str) -> ProviderKey:
        """The resolved key for ``provider`` (never ``None``; may be absent)."""
        return self.keys.get(provider, ProviderKey(provider=provider, value=None, own_key=False))

    def value(self, provider: str) -> str | None:
        """The key string, or ``None``."""
        return self.get(provider).value

    def owns(self, provider: str) -> bool:
        """True when the call is on the user's own key and so outside the platform quota."""
        return self.get(provider).own_key

    @property
    def llm_provider(self) -> str | None:
        """Which language model provider will actually be used.

        The user's own key wins over the platform's, and Anthropic wins over
        Gemini when both are present - the same preference order
        :func:`src.llm.build_llm` applies, kept in one place so the console can
        state it before a meeting rather than after.
        """
        for provider in (CredentialProvider.ANTHROPIC.value, CredentialProvider.GEMINI.value):
            key = self.get(provider)
            if key.present and key.own_key:
                return provider
        for provider in (CredentialProvider.ANTHROPIC.value, CredentialProvider.GEMINI.value):
            if self.get(provider).present:
                return provider
        return None

    @property
    def uses_platform_llm(self) -> bool:
        """True when language model calls will be billed to the platform."""
        provider = self.llm_provider
        return provider is not None and not self.owns(provider)

    @property
    def uses_platform_voice(self) -> bool:
        """True when speech calls will be billed to the platform."""
        return any(
            self.get(p).present and not self.owns(p)
            for p in (CredentialProvider.DEEPGRAM.value, CredentialProvider.CARTESIA.value)
        )

    @property
    def voice_ready(self) -> bool:
        """True when both speech providers have a key; without both there is no voice loop."""
        return self.get(CredentialProvider.DEEPGRAM.value).present and self.get(
            CredentialProvider.CARTESIA.value
        ).present

    def missing(self) -> list[str]:
        """Providers with no key at all, platform or personal."""
        return [p for p in PLATFORM_ENV if not self.get(p).present]

    def environment(self) -> dict[str, str]:
        """The provider keys as environment variables, for a worker container.

        Only providers that resolved to a key appear, so a container never
        inherits a stale platform key for a provider this tenant is not using.
        """
        env: dict[str, str] = {}
        for provider, variable in PLATFORM_ENV.items():
            key = self.get(provider)
            if key.present:
                env[variable] = key.value  # type: ignore[assignment]
        return env

    def describe(self) -> str:
        """One line summarising where this tenant's calls go."""
        parts = [self.get(p).describe() for p in PLATFORM_ENV if self.get(p).present]
        return "; ".join(parts) or "no provider keys configured"


def platform_key(provider: str) -> str | None:
    """The operator's key for ``provider`` from the environment, if set."""
    variable = PLATFORM_ENV.get(provider)
    if not variable:
        return None
    return os.getenv(variable, "").strip() or None


def resolve(session: Session, user_id: uuid.UUID, *, allow_platform: bool = True) -> ResolvedCredentials:
    """Resolve every provider key for one tenant.

    ``allow_platform=False`` is how a suspended or over-quota account is served:
    their own keys still work, the operator's do not.
    """
    stored: dict[str, str] = {}
    hints: dict[str, str] = {}
    for row in repo.list_credentials(session, user_id):
        try:
            stored[row.provider] = decrypt_secret(row.secret_encrypted)
            hints[row.provider] = row.hint
        except AuthError:
            logger.error(
                "could not decrypt %s credential for user %s; treating as absent", row.provider, user_id
            )
    keys: dict[str, ProviderKey] = {}
    for provider in PLATFORM_ENV:
        own = stored.get(provider)
        if own:
            keys[provider] = ProviderKey(provider, own, True, hints.get(provider, ""))
            continue
        shared = platform_key(provider) if allow_platform else None
        keys[provider] = ProviderKey(
            provider, shared, False, secret_hint(shared) if shared else ""
        )
    return ResolvedCredentials(user_id=user_id, keys=keys)


def store_key(session: Session, user_id: uuid.UUID, *, provider: str, secret: str) -> str:
    """Encrypt and store one of a user's own keys. Returns the display hint.

    Raises:
        ValueError: unknown provider or empty key.
    """
    if provider not in PLATFORM_ENV:
        raise ValueError(f"unknown provider {provider!r}; expected one of {', '.join(PLATFORM_ENV)}")
    cleaned = secret.strip()
    if not cleaned:
        raise ValueError("the key is empty")
    hint = secret_hint(cleaned)
    repo.upsert_credential(
        session, user_id=user_id, provider=provider, secret_encrypted=encrypt_secret(cleaned), hint=hint
    )
    logger.info("stored a personal %s key for user %s", provider, user_id)
    return hint


def forget_key(session: Session, user_id: uuid.UUID, provider: str) -> bool:
    """Delete one of a user's own keys, falling back to the platform key."""
    return repo.delete_credential(session, user_id, provider)


async def verify_key(provider: str, secret: str) -> tuple[bool, str]:
    """Make one cheap real call to check a key works. Returns ``(ok, detail)``.

    A key that is stored but wrong is worse than no key: it fails in the middle
    of a live meeting instead of at the moment the user pastes it.
    """
    cleaned = secret.strip()
    if not cleaned:
        return False, "the key is empty"
    try:
        if provider in (CredentialProvider.GEMINI.value, CredentialProvider.ANTHROPIC.value):
            return await _verify_llm(provider, cleaned)
        if provider == CredentialProvider.DEEPGRAM.value:
            return await _verify_http(
                "https://api.deepgram.com/v1/projects", {"Authorization": f"Token {cleaned}"}
            )
        if provider == CredentialProvider.CARTESIA.value:
            return await _verify_http(
                "https://api.cartesia.ai/voices",
                {"X-API-Key": cleaned, "Cartesia-Version": "2024-06-10"},
            )
    except Exception as exc:  # noqa: BLE001 - any failure is "we could not verify it"
        return False, f"could not verify: {exc}"
    return False, f"no verification available for {provider}"


async def _verify_http(url: str, headers: dict[str, str]) -> tuple[bool, str]:
    import httpx

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, headers=headers)
    if response.status_code in (200, 204):
        return True, "key accepted"
    if response.status_code in (401, 403):
        return False, "the provider rejected this key"
    return False, f"provider returned HTTP {response.status_code}"


async def _verify_llm(provider: str, secret: str) -> tuple[bool, str]:
    """Verify a model key by running the same preflight the delegate runs at startup."""
    from src.llm import (
        ANTHROPIC_MODELS,
        GEMINI_MODELS,
        AnthropicLlm,
        GeminiLlm,
        preflight,
    )

    previous = os.environ.get(PLATFORM_ENV[provider])
    os.environ[PLATFORM_ENV[provider]] = secret
    try:
        client = (
            AnthropicLlm(dict(ANTHROPIC_MODELS))
            if provider == CredentialProvider.ANTHROPIC.value
            else GeminiLlm(dict(GEMINI_MODELS))
        )
        result = await preflight(client)
        return result.ok, result.detail
    finally:
        if previous is None:
            os.environ.pop(PLATFORM_ENV[provider], None)
        else:
            os.environ[PLATFORM_ENV[provider]] = previous
