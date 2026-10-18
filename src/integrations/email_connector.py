"""
Email briefings via Resend.

An API key is all this needs - no OAuth dance, no app registration - so it
is one of two connectors that can actually be exercised on a deployment with
no third-party developer apps configured yet (the other is Slack's Incoming
Webhook, :mod:`src.integrations.slack`).
"""

from __future__ import annotations

import html as html_escape
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

logger = logging.getLogger("deleg8.integrations.email")

RESEND_API = "https://api.resend.com"
DEFAULT_FROM = "DELEG8 <onboarding@resend.dev>"
"""Resend's own shared sending address, usable without verifying a custom domain."""


class EmailConnector(Connector):
    """Sends an HTML + plaintext briefing through the Resend API."""

    meta = ConnectorMeta(
        connector_type=ConnectorType.EMAIL.value,
        label="Email (Resend)",
        auth_mode=AuthMode.TOKEN,
        fields=(
            ConnectorField(name="api_key", label="Resend API Key", secret=True, placeholder="re_..."),
            ConnectorField(name="to_address", label="Send briefings to", placeholder="you@yourcompany.com"),
        ),
        docs_url="https://resend.com/api-keys",
    )

    async def authorize(self, credential: str | None, config: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        key = (credential or "").strip()
        if not key:
            raise ConnectorAuthError("the Resend API key is empty")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{RESEND_API}/domains", headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError as exc:
            raise ConnectorAuthError(f"could not reach Resend: {exc}") from exc
        if response.status_code in (401, 403):
            raise ConnectorAuthError("Resend rejected this API key")
        if response.status_code >= 500:
            raise ConnectorAuthError(f"Resend returned HTTP {response.status_code}; try again shortly")
        to_address = (config or {}).get("to_address", "").strip()
        if not to_address:
            raise ConnectorAuthError("enter the address briefings should be sent to")
        return key, {"to_address": to_address}

    async def send_briefing(
        self, token: str | None, config: dict[str, Any], briefing: BriefingContent
    ) -> ConnectorStatus:
        if not token:
            return ConnectorStatus(ok=False, detail="not connected")
        to_address = (config or {}).get("to_address") or briefing.principal_email
        if not to_address:
            return ConnectorStatus(ok=False, detail="no recipient address configured")
        payload = {
            "from": DEFAULT_FROM,
            "to": [to_address],
            "subject": f"Meeting briefing: {briefing.meeting_key}",
            "html": _render_html(briefing),
            "text": _render_text(briefing),
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"{RESEND_API}/emails", json=payload, headers={"Authorization": f"Bearer {token}"}
                )
        except httpx.HTTPError as exc:
            return ConnectorStatus(ok=False, detail=f"could not reach Resend: {exc}")
        if response.status_code in (200, 201):
            return ConnectorStatus(ok=True, detail="sent")
        return ConnectorStatus(ok=False, detail=f"Resend returned HTTP {response.status_code}: {response.text[:200]}")


def _render_text(briefing: BriefingContent) -> str:
    lines = [f"Meeting briefing: {briefing.meeting_key}", "", briefing.summary, ""]
    if briefing.decisions:
        lines += ["Decisions:", *[f"- {d}" for d in briefing.decisions], ""]
    if briefing.action_items:
        lines += [
            "Action items:",
            *[
                f"- {item.get('description', '')} "
                f"({item.get('owner') or 'Unassigned'}"
                f"{', due ' + item['due'] if item.get('due') else ''})"
                for item in briefing.action_items
            ],
            "",
        ]
    if briefing.risks:
        lines += ["Risks:", *[f"- {r}" for r in briefing.risks]]
    return "\n".join(lines)


def _render_html(briefing: BriefingContent) -> str:
    esc = html_escape.escape
    decisions = "".join(f"<li>{esc(d)}</li>" for d in briefing.decisions)
    actions = "".join(
        f"<li>{esc(item.get('description', ''))} &mdash; <em>{esc(item.get('owner') or 'Unassigned')}</em>"
        + (f" (due {esc(item['due'])})" if item.get("due") else "")
        + "</li>"
        for item in briefing.action_items
    )
    risks = "".join(f"<li>{esc(r)}</li>" for r in briefing.risks)
    sections = "".join([
        f"<h3>Decisions</h3><ul>{decisions}</ul>" if decisions else "",
        f"<h3>Action Items</h3><ul>{actions}</ul>" if actions else "",
        f"<h3>Risks</h3><ul>{risks}</ul>" if risks else "",
    ])
    return (
        '<div style="font-family:sans-serif;max-width:600px;color:#1a1a1a">'
        f"<h2>Meeting briefing: {esc(briefing.meeting_key)}</h2>"
        f"<p>{esc(briefing.summary)}</p>"
        f"{sections}"
        "</div>"
    )
