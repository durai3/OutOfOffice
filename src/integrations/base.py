"""
The connector protocol: one small async surface every briefing/task
destination implements, so adding a connector never touches the code that
fires one.

Two auth modes cover everything reachable without a registered OAuth app:

* ``TOKEN`` - the user pastes a credential they generate themselves (a Slack
  Incoming Webhook URL, a Resend API key). Real and working today.
* ``OAUTH`` - a redirect-and-exchange dance against a provider's app
  registry. Every OAuth connector in this deployment is currently a
  :class:`~src.integrations.stub.NotConfiguredConnector`: there is no
  registered client id/secret to redirect to yet, and ``authorize()`` says so
  plainly instead of pretending to work.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ConnectorAuthError(RuntimeError):
    """Raised by ``authorize()`` when a credential is missing, malformed, or rejected."""


class AuthMode(str, Enum):
    """How a user connects this connector."""

    TOKEN = "token"
    """Paste a credential: a webhook URL or an API key. Works without any app registration."""
    OAUTH = "oauth"
    """Redirect to the provider and exchange a code. Needs a registered OAuth app."""


@dataclass(frozen=True)
class ConnectorField:
    """One piece of config or credential a ``TOKEN`` connector asks the console for."""

    name: str
    label: str
    secret: bool = False
    placeholder: str = ""


@dataclass(frozen=True)
class ConnectorMeta:
    """Static description of a connector, independent of any one user's connection to it."""

    connector_type: str
    label: str
    auth_mode: AuthMode
    fields: tuple[ConnectorField, ...] = ()
    available: bool = True
    """False when this deployment has no way to actually authenticate this connector yet."""
    unavailable_reason: str | None = None
    docs_url: str = ""


@dataclass
class BriefingContent:
    """Everything a connector needs to render one post-meeting briefing."""

    meeting_key: str
    summary: str
    decisions: list[str] = field(default_factory=list)
    action_items: list[dict[str, str]] = field(default_factory=list)
    """Each item: ``{"description": ..., "owner": ..., "due": ...}``."""
    risks: list[str] = field(default_factory=list)
    principal_email: str | None = None


@dataclass
class ConnectorStatus:
    """The outcome of one connector call."""

    ok: bool
    detail: str = ""


class Connector(ABC):
    """Base class for every connector. ``meta`` is a class attribute on each subclass."""

    meta: ConnectorMeta

    async def authorize(self, credential: str | None, config: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        """Validate a pasted credential/config and return what should be stored.

        Returns ``(token_to_encrypt_and_store, config_to_encrypt_and_store)``.
        Raises :class:`ConnectorAuthError` with a user-facing reason on rejection.
        """
        raise ConnectorAuthError(f"{self.meta.label} does not support this authorization flow")

    @abstractmethod
    async def send_briefing(
        self, token: str | None, config: dict[str, Any], briefing: BriefingContent
    ) -> ConnectorStatus:
        """Deliver one post-meeting briefing."""

    async def create_task(
        self, token: str | None, config: dict[str, Any], description: str, *, owner: str = "", due: str = ""
    ) -> ConnectorStatus:
        """Create a tracked task for one action item. Not every connector supports this."""
        return ConnectorStatus(ok=False, detail=f"{self.meta.label} does not support creating tasks")

    async def get_status(self, token: str | None, config: dict[str, Any]) -> ConnectorStatus:
        """Cheap liveness check, for the integrations list."""
        return ConnectorStatus(ok=bool(token), detail="connected" if token else "not connected")

    async def list_options(self, token: str | None, config: dict[str, Any]) -> list[dict[str, str]] | None:
        """A follow-up picker's choices (which project/board/calendar), or ``None`` when there isn't one."""
        return None

    def is_available(self) -> bool:
        """Whether this deployment can actually use this connector right now.

        Static (``meta.available``) for a ``TOKEN`` connector;
        :class:`~src.integrations.oauth.OAuthConnector` overrides this with a
        live environment check, since the operator may configure an app's
        credentials after this process already started.
        """
        return self.meta.available
