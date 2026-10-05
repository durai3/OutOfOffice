"""
Password hashing, token signing, and secret encryption.

Three separate secrets, deliberately:

* ``DELEG8_SECRET_KEY`` signs access tokens.
* Refresh tokens are opaque random strings; only their SHA-256 is stored, so a
  database dump does not hand an attacker live sessions.
* ``DELEG8_ENCRYPTION_KEY`` encrypts user-supplied API keys at rest
  (:mod:`src.tenancy.credentials`). It is separate from the signing key because
  rotating one must not invalidate the other.

In development both are derived from the project path when unset, with a loud
warning, so a fresh checkout runs; in production both must be set or the server
refuses to start (:func:`require_production_secrets`).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("deleg8.auth")

ALGORITHM: str = "HS256"
ACCESS_TOKEN_TTL_MINUTES: int = int(os.getenv("DELEG8_ACCESS_TOKEN_MINUTES", "60"))
REFRESH_TOKEN_TTL_DAYS: int = int(os.getenv("DELEG8_REFRESH_TOKEN_DAYS", "30"))

_DEV_SEED: str = "deleg8-development-only-do-not-use-in-production"


class AuthError(RuntimeError):
    """A credential, token, or secret was rejected."""


def _derived(name: str) -> str:
    """A stable development secret, so a fresh checkout boots without configuration."""
    logger.warning(
        "%s is not set; deriving a development-only value. Set it before exposing this server.", name
    )
    return hashlib.sha256(f"{_DEV_SEED}:{name}".encode()).hexdigest()


def secret_key() -> str:
    """The token signing secret."""
    return os.getenv("DELEG8_SECRET_KEY", "").strip() or _derived("DELEG8_SECRET_KEY")


def encryption_key() -> bytes:
    """The Fernet key used to encrypt stored API keys.

    Accepts a urlsafe-base64 Fernet key directly, or any passphrase (hashed to
    32 bytes), so operators are not forced to generate a specific format.
    """
    raw = os.getenv("DELEG8_ENCRYPTION_KEY", "").strip() or _derived("DELEG8_ENCRYPTION_KEY")
    try:
        candidate = raw.encode()
        Fernet(candidate)
        return candidate
    except Exception:  # noqa: BLE001 - not a Fernet key, so derive one from the passphrase
        return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())


def production_secret_problems() -> list[str]:
    """Which required secrets are missing or left at a development default."""
    problems: list[str] = []
    for name in ("DELEG8_SECRET_KEY", "DELEG8_ENCRYPTION_KEY"):
        value = os.getenv(name, "").strip()
        if not value:
            problems.append(f"{name} is not set")
        elif len(value) < 32:
            problems.append(f"{name} is shorter than 32 characters")
    return problems


def require_production_secrets() -> None:
    """Refuse to serve a public deployment on derived development secrets.

    Raises:
        AuthError: when ``DELEG8_ENV=production`` and a secret is missing.
    """
    if os.getenv("DELEG8_ENV", "development").strip().lower() != "production":
        return
    problems = production_secret_problems()
    if problems:
        raise AuthError(
            "Refusing to start in production with insecure secrets: "
            + "; ".join(problems)
            + ". Generate them with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #

MIN_PASSWORD_LENGTH: int = 10


def password_problems(password: str) -> list[str]:
    """Everything wrong with a proposed password, in plain language."""
    problems: list[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"must be at least {MIN_PASSWORD_LENGTH} characters")
    if password.strip() != password:
        problems.append("must not start or end with whitespace")
    if password.lower() in {"password12", "deleg8admin", "1234567890"}:
        problems.append("is too common")
    return problems


def hash_password(password: str) -> str:
    """bcrypt hash of ``password``.

    bcrypt silently truncates at 72 bytes, which would make two different long
    passwords interchangeable, so the input is pre-hashed to a fixed-length
    digest first.
    """
    digest = base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())
    return bcrypt.hashpw(digest, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Constant-time check of ``password`` against a stored hash."""
    try:
        digest = base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())
        return bcrypt.checkpw(digest, hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #

def create_access_token(
    subject: str, *, extra: dict[str, Any] | None = None, ttl_minutes: int | None = None
) -> tuple[str, datetime]:
    """Sign a short-lived access token. Returns ``(token, expires_at)``."""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=ttl_minutes or ACCESS_TOKEN_TTL_MINUTES)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "typ": "access",
        **(extra or {}),
    }
    return jwt.encode(payload, secret_key(), algorithm=ALGORITHM), expires


def decode_access_token(token: str) -> dict[str, Any]:
    """Verify and decode an access token.

    Raises:
        AuthError: expired, tampered with, or not an access token.
    """
    try:
        payload = jwt.decode(token, secret_key(), algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("session expired; sign in again") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("invalid authentication token") from exc
    if payload.get("typ") != "access":
        raise AuthError("wrong token type")
    return payload


def new_refresh_token() -> tuple[str, str]:
    """A fresh opaque refresh token and the hash to store. Returns ``(token, token_hash)``."""
    token = secrets.token_urlsafe(48)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    """SHA-256 of a bearer token, as stored in the database."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def refresh_ttl() -> timedelta:
    """How long a refresh token stays valid."""
    return timedelta(days=REFRESH_TOKEN_TTL_DAYS)


# --------------------------------------------------------------------------- #
# Secrets at rest
# --------------------------------------------------------------------------- #

def encrypt_secret(plaintext: str) -> str:
    """Encrypt a user-supplied API key for storage."""
    return Fernet(encryption_key()).encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a stored API key.

    Raises:
        AuthError: when the ciphertext does not match the current encryption key
            (the usual cause is a rotated ``DELEG8_ENCRYPTION_KEY``).
    """
    try:
        return Fernet(encryption_key()).decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise AuthError(
            "stored credential could not be decrypted; DELEG8_ENCRYPTION_KEY has changed"
        ) from exc


def secret_hint(secret: str) -> str:
    """The last four characters of a key, for display. Never the key itself."""
    cleaned = secret.strip()
    return f"...{cleaned[-4:]}" if len(cleaned) >= 4 else "..."


def new_link_code() -> str:
    """A short, unambiguous one-time code for linking a Telegram chat."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1
    return "".join(secrets.choice(alphabet) for _ in range(8))


def new_job_token() -> tuple[str, str]:
    """A bearer token for one worker container, and its stored hash."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)
