"""
Connectors that need a registered OAuth app this deployment does not have.

Each is still a first-class entry in the connector registry - the console
lists it as a known, plug-and-play connector type - but ``authorize()``
explains exactly what is missing (a client id/secret from the provider's
developer console) instead of simulating a working connection. Once a real
app is registered, the fix is to replace the registry entry with a connector
that implements the real OAuth exchange; nothing about the framework around
it changes.
"""

from __future__ import annotations

from typing import Any

from src.integrations.base import (
    BriefingContent,
    Connector,
    ConnectorAuthError,
    ConnectorMeta,
    ConnectorStatus,
)


class NotConfiguredConnector(Connector):
    """A listed connector whose OAuth app has not been registered yet."""

    def __init__(self, meta: ConnectorMeta) -> None:
        self.meta = meta

    async def authorize(self, credential: str | None, config: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        raise ConnectorAuthError(
            f"{self.meta.label} needs a registered OAuth application (client id and secret) "
            f"that has not been configured on this server yet."
            + (f" See {self.meta.docs_url}." if self.meta.docs_url else "")
        )

    async def send_briefing(
        self, token: str | None, config: dict[str, Any], briefing: BriefingContent
    ) -> ConnectorStatus:
        return ConnectorStatus(ok=False, detail=f"{self.meta.label} is not connected")

    async def get_status(self, token: str | None, config: dict[str, Any]) -> ConnectorStatus:
        return ConnectorStatus(ok=False, detail=self.meta.unavailable_reason or "requires OAuth app credentials")
