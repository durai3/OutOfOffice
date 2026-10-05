"""
The shape every meeting-platform bot presents to the rest of DELEG8.

Before this module, ``GoogleMeetBot`` and ``MeetSettings`` were hardcoded
throughout ``src/gateway/session.py`` and ``src/api/*`` - joining a meeting
meant joining a *Google Meet*. Zoom and Microsoft Teams support means the rest
of the system (the voice loop, the brain, memory, escalation, the web console)
must stop caring which platform a call is on. This module is the seam that
makes that possible:

* :data:`MeetStatus` and the three ``Meet*Error`` exceptions move here from
  ``meet_bot.py`` (which still re-exports them under the same names, so
  nothing that already imports ``from src.gateway.meet_bot import MeetStatus``
  needs to change) because none of their names or values are actually
  Google-specific - "IN_CALL", "LOBBY", "REMOVED" describe every platform.
* :class:`Platform` and :func:`detect_platform` let ``session.py`` look at a
  join URL once and pick the right bot class, instead of a caller having to
  say which platform they mean.
* :class:`MeetingBot` is the ``Protocol`` every bot implements -
  ``GoogleMeetBot`` (``meet_bot.py``), ``ZoomBot`` (``zoom_bot.py``) and
  ``TeamsBot`` (``teams_bot.py``) each drive a different DOM but expose the
  identical start/login/join/leave/health surface, so
  :class:`~src.gateway.session.MeetingSession` (and everything it calls) is
  written once and runs unchanged against any of the three.

Every platform's own module still owns its DOM selectors, its settings
dataclass, and its setup instructions - those genuinely differ per platform
and belong next to the code that uses them, not centralised here.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Env-parsing helpers shared by every platform's settings dataclass
# --------------------------------------------------------------------------- #
# Each platform module (meet_bot.py, zoom_bot.py, teams_bot.py) used to define
# its own private copy of these four functions, byte-for-byte identical. One
# shared copy here means a fix or a new accepted value ("1"/"true"/"yes"/"on")
# only has to be made once.


def env_str(name: str, default: str = "") -> str:
    """A stripped string env var, or ``default`` when unset/blank."""
    return os.getenv(name, "").strip() or default


def env_bool(name: str, default: bool) -> bool:
    """A boolean env var (``true``/``false``/``yes``/``no``/``1``/``0``/``on``/``off``).

    Raises:
        MeetConfigError: the value is set but not one of the accepted forms.
    """
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise MeetConfigError(f"{name} must be true or false (got {raw!r}).")


def env_number(name: str, default: float, *, minimum: float) -> float:
    """A numeric env var with a floor.

    Raises:
        MeetConfigError: the value is set but not a number, or is below ``minimum``.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise MeetConfigError(f"{name} must be a number (got {raw!r}).") from exc
    if value < minimum:
        raise MeetConfigError(f"{name} must be >= {minimum} (got {value}).")
    return value


def resolve_path(path: str) -> Path:
    """``path`` as an absolute :class:`~pathlib.Path`, relative ones resolved against the repo root."""
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else (PROJECT_ROOT / candidate).resolve()


# --------------------------------------------------------------------------- #
# Container audio routing
# --------------------------------------------------------------------------- #
# Locally, a human wires up virtual audio cables (BlackHole on macOS) and
# points DELEG8_INPUT_DEVICE_INDEX / DELEG8_OUTPUT_DEVICE_INDEX at them; see
# src/audio/device_setup.py. Inside the containerised worker (docker/), that
# wiring happens in code instead: docker/entrypoint.sh starts PulseAudio and
# creates two null-sinks in place of the two physical cables, then launches
# this process with PULSE_SINK / PULSE_SOURCE already set so PortAudio's own
# Pulse backend picks them up as "the default device" with no index to guess.
# Chromium is a *separate* process (launched by Playwright) that needs the
# *other* two virtual devices - these two settings pass that pair to it
# specifically, without touching the parent process's own PulseAudio defaults.
BOT_CHROMIUM_PULSE_SINK_ENV: str = "BOT_CHROMIUM_PULSE_SINK"
BOT_CHROMIUM_PULSE_SOURCE_ENV: str = "BOT_CHROMIUM_PULSE_SOURCE"


def chromium_pulse_env(pulse_sink: str, pulse_source: str) -> dict[str, str]:
    """Environment overrides for the Chromium subprocess's audio routing.

    Empty when neither is set (the ordinary local, non-containerised case) -
    callers should only pass this to Playwright's ``launch()``/
    ``launch_persistent_context()`` when it is non-empty, leaving Chromium to
    inherit the parent process's environment (and therefore the system's
    default audio devices) exactly as before.
    """
    overrides: dict[str, str] = {}
    if pulse_sink:
        overrides["PULSE_SINK"] = pulse_sink
    if pulse_source:
        overrides["PULSE_SOURCE"] = pulse_source
    return overrides


class MeetConfigError(RuntimeError):
    """Raised when a bot is misconfigured (bad URL, missing browser, bad numbers).

    Shared across every platform so callers (``session.py``, the API layer)
    catch one exception type regardless of which bot raised it.
    """


class MeetLoginError(RuntimeError):
    """Raised when a platform's sign-in flow does not end signed in."""


class MeetJoinError(RuntimeError):
    """Raised when a bot cannot get into the call (invalid link, denied, lobby timeout)."""


class UnsupportedMeetingUrlError(MeetConfigError):
    """Raised when a URL matches none of the supported platforms.

    A subclass of :class:`MeetConfigError` on purpose: every existing
    ``except MeetConfigError`` in ``session.py`` and the API layer already
    handles "bad meeting link" correctly, so this needs no new handling
    anywhere it is raised.
    """


class MeetStatus(str, Enum):
    """Where a bot is in the call lifecycle. Identical meaning on every platform."""

    IDLE = "IDLE"
    STARTING = "STARTING"
    LOGGING_IN = "LOGGING_IN"
    JOINING = "JOINING"
    LOBBY = "LOBBY"
    IN_CALL = "IN_CALL"
    RECONNECTING = "RECONNECTING"
    REMOVED = "REMOVED"
    ENDED = "ENDED"
    LEFT = "LEFT"
    ERROR = "ERROR"

    @property
    def in_call(self) -> bool:
        """True while the bot can hear and speak."""
        return self is MeetStatus.IN_CALL


class Platform(str, Enum):
    """A video-meeting platform DELEG8 can join as a delegate."""

    GOOGLE_MEET = "google_meet"
    ZOOM = "zoom"
    TEAMS = "teams"

    @property
    def label(self) -> str:
        """Human-readable name for logs and error messages."""
        return {Platform.GOOGLE_MEET: "Google Meet", Platform.ZOOM: "Zoom", Platform.TEAMS: "Microsoft Teams"}[self]

    @property
    def env_prefix(self) -> str:
        """Prefix used by this platform's ``BOT_<PREFIX>_*`` credential variables."""
        return {Platform.GOOGLE_MEET: "GOOGLE", Platform.ZOOM: "ZOOM", Platform.TEAMS: "TEAMS"}[self]


# --------------------------------------------------------------------------- #
# URL detection
# --------------------------------------------------------------------------- #

_PLATFORM_URL_PATTERNS: tuple[tuple[Platform, re.Pattern[str]], ...] = (
    (Platform.GOOGLE_MEET, re.compile(r"^https://meet\.google\.com/", re.IGNORECASE)),
    (Platform.ZOOM, re.compile(r"^https://[\w.-]*\.?zoom\.(?:us|com)/", re.IGNORECASE)),
    (
        Platform.TEAMS,
        re.compile(r"^https://(?:teams\.microsoft\.com|teams\.live\.com|[\w.-]+\.teams\.microsoft\.com)/", re.IGNORECASE),
    ),
)

_MEETING_ID_PATTERNS: tuple[tuple[Platform, re.Pattern[str]], ...] = (
    (Platform.GOOGLE_MEET, re.compile(r"^https://meet\.google\.com/(?P<id>[a-z]{3}-[a-z]{4}-[a-z]{3})(?:[/?#].*)?$", re.IGNORECASE)),
    (Platform.ZOOM, re.compile(r"^https://[\w.-]*\.?zoom\.(?:us|com)/(?:j|wc(?:/join)?)/(?P<id>\d{9,11})", re.IGNORECASE)),
    (Platform.TEAMS, re.compile(r"^https://[\w.-]*teams\.(?:microsoft|live)\.com/(?:l/meetup-join/|meet/)(?P<id>[^/?#]+)", re.IGNORECASE)),
)


def detect_platform(url: str) -> Platform:
    """Identify which platform a meeting URL belongs to.

    Raises:
        UnsupportedMeetingUrlError: ``url`` matches none of Google Meet, Zoom, or Teams.
    """
    stripped = url.strip()
    for platform, pattern in _PLATFORM_URL_PATTERNS:
        if pattern.match(stripped):
            return platform
    raise UnsupportedMeetingUrlError(
        f"{url!r} is not a recognised Google Meet, Zoom, or Microsoft Teams link. "
        "Expected something starting with https://meet.google.com/, https://*.zoom.us/, "
        "or https://teams.microsoft.com/ (or teams.live.com)."
    )


def meeting_identifier(url: str, *, platform: Platform | None = None) -> str:
    """A short, human-readable id for ``url`` - a Meet code, a Zoom meeting number, a
    Teams meeting token - falling back to a stable hash when the link's shape does not
    expose one plainly (long opaque Teams tokens, mainly).

    Used only for building a readable default ``meeting_id`` (``f"{platform}-{code}"``)
    for memory and the briefing archive - never for validating that the link is well
    formed, which is each platform's own settings class's job.
    """
    stripped = url.strip()
    resolved = platform or detect_platform(stripped)
    for candidate_platform, pattern in _MEETING_ID_PATTERNS:
        if candidate_platform is not resolved:
            continue
        match = pattern.match(stripped)
        if match:
            return match.group("id")
    # Opaque or unrecognised shape for this platform: a short stable hash beats
    # nothing, and is still useful to eyeball two runs of the same link as the same meeting.
    return hashlib.sha1(stripped.encode("utf-8")).hexdigest()[:10]


# --------------------------------------------------------------------------- #
# The shared bot protocol
# --------------------------------------------------------------------------- #


@runtime_checkable
class BotSettingsLike(Protocol):
    """The minimal settings surface :mod:`src.gateway.session` needs, common to every
    platform's own settings dataclass (``MeetSettings``, ``ZoomSettings``, ``TeamsSettings``)."""

    join_url: str
    display_name: str
    headless: bool
    email: str

    @property
    def has_credentials(self) -> bool: ...


@dataclass
class PrejoinReport:
    """What a bot found on the pre-join screen, without clicking Join (dry runs, diagnostics).

    Same shape for every platform - ``GoogleMeetBot.inspect_prejoin``,
    ``ZoomBot.inspect_prejoin`` and ``TeamsBot.inspect_prejoin`` all return
    this, so :func:`~src.gateway.session._dry_run` (``python -m src.main join
    --dry-run``) needs no platform-specific branch to print it.
    """

    url: str
    signed_in: bool
    join_button_label: str | None
    name_input_present: bool
    mic_toggle_present: bool
    camera_toggle_present: bool
    blocking_text: str | None
    screenshot: Path | None
    notes: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """True when a join could be attempted from this screen."""
        return self.join_button_label is not None and self.blocking_text is None


@runtime_checkable
class MeetingBot(Protocol):
    """What every platform bot must provide.

    ``GoogleMeetBot``, ``ZoomBot`` and ``TeamsBot`` each implement this
    identically - not by inheriting from a common base class (each already has
    its own constructor shape tied to its own settings dataclass), but because
    every one of them defines exactly these methods and attributes. This
    ``Protocol`` documents that contract in one place and lets type checkers
    verify a bot actually satisfies it.
    """

    status: MeetStatus
    joined_at: float | None
    last_reason: str | None
    participant_count: int | None

    @property
    def settings(self) -> BotSettingsLike: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def login(self) -> bool: ...

    async def join_meeting(self, url: str | None = None) -> None: ...

    async def inspect_prejoin(self, url: str) -> PrejoinReport: ...

    async def leave(self) -> None: ...

    async def check_health(self) -> str | None: ...

    async def stay_connected(self) -> str: ...

    async def wait_for_status_change(self) -> MeetStatus: ...


__all__ = [
    "BOT_CHROMIUM_PULSE_SINK_ENV",
    "BOT_CHROMIUM_PULSE_SOURCE_ENV",
    "PROJECT_ROOT",
    "BotSettingsLike",
    "MeetConfigError",
    "MeetJoinError",
    "MeetLoginError",
    "MeetStatus",
    "MeetingBot",
    "Platform",
    "PrejoinReport",
    "UnsupportedMeetingUrlError",
    "chromium_pulse_env",
    "detect_platform",
    "env_bool",
    "env_number",
    "env_str",
    "meeting_identifier",
    "resolve_path",
]
