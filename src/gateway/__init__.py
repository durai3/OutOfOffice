"""
Meeting-platform ingress (Google Meet, Zoom, Microsoft Teams) and the meeting session runner.

* :mod:`src.gateway.bot_base`    - the shared Platform/MeetStatus/MeetingBot seam every bot implements.
* :mod:`src.gateway.meet_bot`    - Playwright-driven Chromium that joins a Google Meet call.
* :mod:`src.gateway.zoom_bot`    - same, for Zoom's web client.
* :mod:`src.gateway.teams_bot`   - same, for Microsoft Teams' guest web-join.
* :mod:`src.gateway.components`  - what a session runs on (bridge, knowledge, brain, memory, reporter).
* :mod:`src.gateway.session`     - one attended meeting, for the command line or the HTTP API - picks
  the right bot above via :func:`~src.gateway.bot_base.detect_platform`.
"""

from src.gateway.bot_base import (
    MeetConfigError,
    MeetJoinError,
    MeetLoginError,
    MeetStatus,
    Platform,
    detect_platform,
)
from src.gateway.components import SessionComponents, SessionOptions
from src.gateway.meet_bot import GoogleMeetBot, MeetSettings, meeting_code
from src.gateway.session import MeetingSession, SessionConfig, run_meeting_session
from src.gateway.teams_bot import TeamsBot, TeamsSettings
from src.gateway.zoom_bot import ZoomBot, ZoomSettings

__all__ = [
    "GoogleMeetBot",
    "MeetConfigError",
    "MeetJoinError",
    "MeetLoginError",
    "MeetSettings",
    "MeetStatus",
    "MeetingSession",
    "Platform",
    "SessionComponents",
    "SessionConfig",
    "SessionOptions",
    "TeamsBot",
    "TeamsSettings",
    "ZoomBot",
    "ZoomSettings",
    "detect_platform",
    "meeting_code",
    "run_meeting_session",
]
