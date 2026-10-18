"""
Slack via an Incoming Webhook.

Deliberately not the full Slack OAuth bot flow: that needs a registered
Slack app (client id/secret, a Request URL for slash commands) that this
deployment does not have. A webhook needs none of that - a user creates one
in their own workspace in one click at the docs URL below - and posting a
briefing to it is a single unauthenticated POST, since the URL itself is the
secret.
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

logger = logging.getLogger("deleg8.integrations.slack")


class SlackConnector(Connector):
    """Posts formatted briefings to a Slack channel via its Incoming Webhook."""

    meta = ConnectorMeta(
        connector_type=ConnectorType.SLACK.value,
        label="Slack",
        auth_mode=AuthMode.TOKEN,
        fields=(
            ConnectorField(
                name="webhook_url",
                label="Incoming Webhook URL",
                secret=True,
                placeholder="https://hooks.slack.com/services/...",
            ),
        ),
        docs_url="https://api.slack.com/messaging/webhooks",
    )

    async def authorize(self, credential: str | None, config: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        url = (credential or "").strip()
        if not url.startswith("https://hooks.slack.com/"):
            raise ConnectorAuthError(
                "that doesn't look like a Slack Incoming Webhook URL "
                "(expected https://hooks.slack.com/services/...)"
            )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    url, json={"text": "✅ DELEG8 is connected. Post-meeting briefings will arrive here."}
                )
        except httpx.HTTPError as exc:
            raise ConnectorAuthError(f"could not reach Slack: {exc}") from exc
        if response.status_code != 200:
            raise ConnectorAuthError(
                f"Slack rejected this webhook (HTTP {response.status_code}); generate a new one and try again"
            )
        return url, {}

    async def send_briefing(
        self, token: str | None, config: dict[str, Any], briefing: BriefingContent
    ) -> ConnectorStatus:
        if not token:
            return ConnectorStatus(ok=False, detail="not connected")
        blocks = _format_blocks(briefing)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(token, json={"blocks": blocks, "text": briefing.summary})
        except httpx.HTTPError as exc:
            return ConnectorStatus(ok=False, detail=f"could not reach Slack: {exc}")
        if response.status_code == 200:
            return ConnectorStatus(ok=True, detail="posted")
        return ConnectorStatus(ok=False, detail=f"Slack returned HTTP {response.status_code}")


def _format_blocks(briefing: BriefingContent) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📋 {briefing.meeting_key}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Summary*\n{briefing.summary}"}},
    ]
    if briefing.decisions:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Decisions*\n" + "\n".join(f"• {d}" for d in briefing.decisions)},
        })
    if briefing.action_items:
        lines = [
            f"• {item.get('description', '')} — _{item.get('owner') or 'Unassigned'}_"
            + (f" (due {item['due']})" if item.get("due") else "")
            for item in briefing.action_items
        ]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Action Items*\n" + "\n".join(lines)}})
    if briefing.risks:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Risks*\n" + "\n".join(f"• {r}" for r in briefing.risks)},
        })
    return blocks
