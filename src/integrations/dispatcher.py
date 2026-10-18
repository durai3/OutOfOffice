"""
Fan-out of one post-meeting briefing to every connector a user has enabled.

Called from :meth:`~src.memory.post_meeting.PostMeetingReporter.conclude`
once the Telegram briefing has already been sent and archived. A connector
failing here must never affect the meeting that just concluded: every send
is isolated in its own ``try``/``except`` and only ever logged.
"""

from __future__ import annotations

import json
import logging
import uuid

from src.auth.security import AuthError, decrypt_secret
from src.db import repo
from src.db.base import run_db
from src.db.models import Integration
from src.integrations.base import BriefingContent, ConnectorStatus
from src.integrations.registry import get_connector

logger = logging.getLogger("deleg8.integrations.dispatcher")


def _decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return decrypt_secret(value)
    except AuthError:
        logger.error("could not decrypt a stored integration credential; treating it as absent")
        return None


async def dispatch_briefing(user_id: uuid.UUID, briefing: BriefingContent) -> None:
    """Send ``briefing`` to every connector this user has enabled."""
    rows = await run_db(lambda session: list(repo.list_enabled_integrations(session, user_id)))
    for row in rows:
        await _send_one(user_id, row, briefing)


async def _send_one(user_id: uuid.UUID, row: Integration, briefing: BriefingContent) -> None:
    connector = get_connector(row.connector_type)
    if connector is None:
        logger.warning("integration %s has unknown connector_type %r", row.id, row.connector_type)
        return
    token = _decrypt(row.auth_token_encrypted)
    config_raw = _decrypt(row.config_encrypted)
    config = json.loads(config_raw) if config_raw else {}
    try:
        status: ConnectorStatus = await connector.send_briefing(token, config, briefing)
    except Exception as exc:  # noqa: BLE001 - one connector's crash must not break the others
        logger.exception("connector %s failed to send a briefing for user %s", row.connector_type, user_id)
        status = ConnectorStatus(ok=False, detail=str(exc))

    def _record(session) -> None:  # noqa: ANN001
        repo.upsert_integration(
            session,
            user_id=user_id,
            connector_type=row.connector_type,
            last_synced_at=repo.utcnow() if status.ok else row.last_synced_at,
            last_error=None if status.ok else status.detail,
        )

    try:
        await run_db(_record)
    except Exception:  # noqa: BLE001 - recording the outcome must not raise either
        logger.exception("could not record the outcome of the %s briefing for user %s", row.connector_type, user_id)
    logger.info("briefing -> %s for user %s: ok=%s (%s)", row.connector_type, user_id, status.ok, status.detail)
