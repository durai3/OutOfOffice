"""
Microsoft Teams via a Workflows webhook.

Microsoft retired the old Office 365 Connector webhooks
(``webhook.office.com/...``) in 2026; their replacement is a webhook trigger
inside the Workflows app ("Post to a channel when a webhook request is
received"), which a user sets up entirely inside Teams itself - no Azure
app registration, no client secret. Like Slack's Incoming Webhook
(:mod:`src.integrations.slack`), this needs nothing from this deployment's
operator.

The default Workflows template accepts a plain ``{"text": "..."}`` body. A
user who has customised their flow's trigger schema may need a different
shape - this sends the simplest, most broadly compatible payload rather than
an Adaptive Card, since the trigger schema is theirs to configure, not ours.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.db.models import ConnectorType
from src.integrations.base import (
    AuthMode,
    BriefingContent,
    Connector,
    ConnectorAuthError,
    ConnectorField,
    ConnectorMeta,
    ConnectorStatus,
)

logger = logging.getLogger("deleg8.integrations.teams")


class TeamsConnector(Connector):
    """Posts formatted briefings to a Teams channel via a Workflows webhook."""

    meta = ConnectorMeta(
        connector_type=ConnectorType.MS_TEAMS.value,
        label="Microsoft Teams",
        auth_mode=AuthMode.TOKEN,
        fields=(
            ConnectorField(
                name="webhook_url",
                label="Workflows Webhook URL",
                secret=True,
                placeholder="https://prod-00.westus.logic.azure.com/workflows/...",
            ),
        ),
        docs_url="https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook",
    )

    async def authorize(self, credential: str | None, config: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        url = (credential or "").strip()
        if not url.startswith("https://"):
            raise ConnectorAuthError(
                "that doesn't look like a webhook URL; copy it from the Workflow's "
                "\"When a Teams webhook request is received\" trigger"
            )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    url, json={"text": "DELEG8 is connected. Post-meeting briefings will arrive here."}
                )
        except httpx.HTTPError as exc:
            raise ConnectorAuthError(f"could not reach Teams: {exc}") from exc
        if response.status_code >= 300:
            raise ConnectorAuthError(
                f"Teams rejected this webhook (HTTP {response.status_code}); check the workflow is turned on"
            )
        return url, {}

    async def send_briefing(
        self, token: str | None, config: dict[str, Any], briefing: BriefingContent
    ) -> ConnectorStatus:
        if not token:
            return ConnectorStatus(ok=False, detail="not connected")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(token, json={"text": _format_text(briefing)})
        except httpx.HTTPError as exc:
            return ConnectorStatus(ok=False, detail=f"could not reach Teams: {exc}")
        if response.status_code < 300:
            return ConnectorStatus(ok=True, detail="posted")
        return ConnectorStatus(ok=False, detail=f"Teams returned HTTP {response.status_code}")


def _format_text(briefing: BriefingContent) -> str:
    lines = [f"**{briefing.meeting_key}**", "", briefing.summary]
    if briefing.decisions:
        lines += ["", "**Decisions**"] + [f"- {d}" for d in briefing.decisions]
    if briefing.action_items:
        lines += ["", "**Action Items**"] + [
            f"- {item.get('description', '')} ({item.get('owner') or 'Unassigned'}"
            f"{', due ' + item['due'] if item.get('due') else ''})"
            for item in briefing.action_items
        ]
    if briefing.risks:
        lines += ["", "**Risks**"] + [f"- {r}" for r in briefing.risks]
    return "\n".join(lines)
