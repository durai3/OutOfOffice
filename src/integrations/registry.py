"""
The connector registry: one instance per connector type, looked up by the
``connector_type`` string stored on an :class:`~src.db.models.Integration`
row.

Adding a connector that can actually authenticate (it has an API key or
webhook mode, or a real OAuth app has been registered for it) means adding
one entry here; nothing else in the integrations API or the post-meeting
dispatch needs to change.
"""

from __future__ import annotations

from src.db.models import ConnectorType
from src.integrations.base import AuthMode, Connector, ConnectorMeta
from src.integrations.email_connector import EmailConnector
from src.integrations.google_calendar import GoogleCalendarConnector
from src.integrations.slack import SlackConnector
from src.integrations.stub import NotConfiguredConnector
from src.integrations.teams import TeamsConnector

_OAUTH_NOT_CONFIGURED: tuple[ConnectorMeta, ...] = (
    ConnectorMeta(
        connector_type=ConnectorType.JIRA.value,
        label="Jira",
        auth_mode=AuthMode.OAUTH,
        available=False,
        unavailable_reason="requires a registered Atlassian OAuth 2.0 app",
        docs_url="https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/",
    ),
    ConnectorMeta(
        connector_type=ConnectorType.LINEAR.value,
        label="Linear",
        auth_mode=AuthMode.OAUTH,
        available=False,
        unavailable_reason="requires a registered Linear OAuth application",
        docs_url="https://developers.linear.app/docs/oauth/authentication",
    ),
    ConnectorMeta(
        connector_type=ConnectorType.ASANA.value,
        label="Asana",
        auth_mode=AuthMode.OAUTH,
        available=False,
        unavailable_reason="requires a registered Asana OAuth application",
        docs_url="https://developers.asana.com/docs/oauth",
    ),
    ConnectorMeta(
        connector_type=ConnectorType.NOTION.value,
        label="Notion",
        auth_mode=AuthMode.OAUTH,
        available=False,
        unavailable_reason="requires a registered Notion integration",
        docs_url="https://developers.notion.com/docs/authorization",
    ),
)

_REGISTRY: dict[str, Connector] = {
    ConnectorType.SLACK.value: SlackConnector(),
    ConnectorType.EMAIL.value: EmailConnector(),
    ConnectorType.MS_TEAMS.value: TeamsConnector(),
    ConnectorType.GOOGLE_CALENDAR.value: GoogleCalendarConnector(),
    **{meta.connector_type: NotConfiguredConnector(meta) for meta in _OAUTH_NOT_CONFIGURED},
}


def get_connector(connector_type: str) -> Connector | None:
    """The connector instance for ``connector_type``, or ``None`` if unknown."""
    return _REGISTRY.get(connector_type)


def all_connector_meta() -> list[ConnectorMeta]:
    """Static description of every known connector, in a stable order."""
    order = [c.value for c in ConnectorType]
    return sorted(
        (connector.meta for connector in _REGISTRY.values()),
        key=lambda meta: order.index(meta.connector_type) if meta.connector_type in order else 99,
    )
