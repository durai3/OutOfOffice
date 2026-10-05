"""
Zoom ingress: a Playwright-driven Chromium that joins a call as the delegate, via
Zoom's web client (no desktop app, no SDK credentials).

Same shape as :mod:`src.gateway.meet_bot` on purpose - same lifecycle, same
defensive patterns (screenshot on failure, multiple selector fallbacks, never
hang past a timeout), same :class:`~src.gateway.bot_base.MeetStatus` values -
so :class:`~src.gateway.session.MeetingSession` runs against a Zoom link with
zero changes, and anyone who has read ``meet_bot.py`` already knows this file.

    bot = ZoomBot(ZoomSettings.from_env())
    await bot.start()
    await bot.login()                     # skipped when the profile is already signed in
    await bot.join_meeting(url)           # pre-join screen -> Join -> waiting room -> in call
    reason = await bot.stay_connected()   # health loop; rejoins after a dropped connection
    await bot.leave(); await bot.stop()

.. warning::
    Every selector below is a best-effort read of Zoom's web client, not
    verified against a live Zoom account or meeting - this environment has
    neither. ``meet_bot.py`` started exactly this way and only became
    reliable after several rounds of real-account testing (see the project's
    own sprint history); expect the same here. Before relying on this in a
    real meeting: get a Zoom test account and a real meeting link, then run

        python -m src.gateway.zoom_bot --inspect <join-url>

    (mirrors ``GoogleMeetBot.inspect_prejoin``) to see exactly what this bot
    finds on the pre-join screen without clicking anything, and fix whichever
    selector below does not match what you see. ``BOT_SCREENSHOT_DIR`` gets a
    PNG on every failure specifically to make that fix fast.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.gateway.bot_base import (
    BOT_CHROMIUM_PULSE_SINK_ENV,
    BOT_CHROMIUM_PULSE_SOURCE_ENV,
    MeetConfigError,
    MeetJoinError,
    MeetLoginError,
    MeetStatus,
    PrejoinReport,
    chromium_pulse_env,
    env_bool,
    env_number,
    env_str,
    resolve_path,
)

logger = logging.getLogger("deleg8.gateway.zoom")

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

ZOOM_URL_RE = re.compile(r"^https://[\w.-]*\.?zoom\.(?:us|com)/", re.IGNORECASE)
ZOOM_ID_RE = re.compile(r"/(?:j|wc(?:/join)?)/(?P<id>\d{9,11})", re.IGNORECASE)

ZOOM_SETUP_INSTRUCTIONS: str = """\
How to configure the DELEG8 Zoom bot
-------------------------------------
1. Browser
   python -m playwright install chromium

2. Bot identity
   Create a throwaway Zoom account for the bot and put it in .env:
       BOT_ZOOM_EMAIL=deleg8.bot@example.com
       BOT_ZOOM_PASSWORD=...
   Leave both empty to join as a guest instead - fine for meetings that do not
   require a Zoom account, but a signed-in account is far more reliable
   against waiting rooms and "authorized users only" meetings.

3. The meeting
       ZOOM_JOIN_URL=https://zoom.us/j/1234567890?pwd=...
   (a plain zoom.us/j/... link is fine - the bot converts it to the web-client
   form itself; you do not need to paste the /wc/ link by hand.)

4. Audio routing - same as the Google Meet bot: two virtual cables locally
   (python -m src.audio.device_setup --select --write), or the containerised
   worker's PulseAudio devices when running in Docker. Chromium must run with
   a window (BOT_HEADLESS=false): headless Chromium has no audio devices.

NOTE: the selectors this bot uses were written from documentation and public
examples, not verified against a live Zoom meeting (see the module docstring).
Run  python -m src.gateway.zoom_bot --inspect <url>  against a real meeting
before trusting this with a call that matters.
"""

# --------------------------------------------------------------------------- #
# Selectors and texts (Zoom web client, best effort - see module docstring)
# --------------------------------------------------------------------------- #

NAME_INPUT: str = (
    'input#inputname, input[name="displayName"], input[aria-label="Your Name"], '
    'input[placeholder="Your Name"]'
)
JOIN_BUTTON: str = 'button#joinBtn, button:has-text("Join"), [aria-label="Join"]'
JOIN_AUDIO_BUTTON: str = (
    'button:has-text("Join Audio by Computer"), button:has-text("Join with Computer Audio"), '
    '#voip, [aria-label*="Join Audio"]'
)
MIC_OFF_BUTTON: str = '[aria-label*="mute my microphone"], button#mute'
MIC_ON_BUTTON: str = '[aria-label*="unmute my microphone"], button#unmute'
CAMERA_OFF_BUTTON: str = '[aria-label*="stop my video"]'
CAMERA_ON_BUTTON: str = '[aria-label*="start my video"]'
LEAVE_BUTTON: str = 'button:has-text("Leave"), [aria-label="Leave"]'
LEAVE_CONFIRM_BUTTON: str = 'button:has-text("Leave Meeting"), button:has-text("Leave meeting")'
PARTICIPANTS_BUTTON: str = 'button:has-text("Participants"), [aria-label*="participant"]'
PARTICIPANT_TILE_SELECTOR: str = '[class*="participants-item"], [class*="video-avatar__avatar"], [class*="participant-item"]'
DISMISS_BUTTONS: str = (
    'button:has-text("Got it"), button:has-text("Dismiss"), button:has-text("Close"), '
    'button:has-text("OK"), [aria-label="Close"]'
)

WAITING_ROOM_TEXTS: tuple[str, ...] = (
    "Please wait, the meeting host will let you in soon",
    "waiting room",
    "waiting for the host",
)
DENIED_TEXTS: tuple[str, ...] = (
    "The host has another meeting in progress",
    "This meeting has been ended by host",
    "denied your request",
)
INVALID_TEXTS: tuple[str, ...] = (
    "Invalid meeting ID",
    "This meeting could not be found",
    "This meeting has not started",
    "Meeting ID is not valid",
)
REMOVED_TEXTS: tuple[str, ...] = ("removed you from the meeting", "You have been removed")
ENDED_TEXTS: tuple[str, ...] = ("This meeting has been ended", "Meeting ended", "The host has ended this meeting")
CONNECTION_LOST_TEXTS: tuple[str, ...] = ("Your Internet connection is unstable", "Reconnecting")
LOGIN_BLOCKED_TEXTS: tuple[str, ...] = ("Invalid email or password", "Sign In failed")
EMAIL_INPUT: str = 'input#email, input[type="email"]'
PASSWORD_INPUT: str = 'input#password, input[type="password"]'
SIGNIN_BUTTON: str = 'button#js_btn_login, button:has-text("Sign In")'
ZOOM_SIGNIN_URL: str = "https://zoom.us/signin"

_INSTANT_REJECTION_SECONDS: float = 8.0


def zoom_web_client_url(url: str) -> str:
    """Convert a ``/j/<id>`` link to its ``/wc/join/<id>`` web-client form, keeping the query string.

    Raises:
        MeetConfigError: ``url`` is not a Zoom link.
    """
    stripped = url.strip()
    if not ZOOM_URL_RE.match(stripped):
        raise MeetConfigError(f"ZOOM_JOIN_URL must be a zoom.us/zoom.com link (got {stripped!r}).")
    if "/wc/" in stripped:
        return stripped
    parsed = urlparse(stripped)
    match = re.search(r"/j/(?P<id>\d+)", parsed.path)
    if not match:
        # Already some other Zoom URL shape (a personal room name, etc.) - pass through
        # unchanged rather than guess; the web client generally redirects itself.
        return stripped
    meeting_id = match.group("id")
    query = parsed.query
    return f"{parsed.scheme}://{parsed.netloc}/wc/join/{meeting_id}" + (f"?{query}" if query else "")


def zoom_meeting_id(url: str) -> str:
    """Best-effort numeric meeting id from a Zoom link, for a readable ``meeting_id`` label."""
    match = ZOOM_ID_RE.search(url)
    return match.group("id") if match else ""


@dataclass(frozen=True)
class ZoomSettings:
    """Everything the Zoom bot needs, normally read from the environment.

    Field-for-field the same shape as :class:`~src.gateway.meet_bot.MeetSettings`
    on purpose - see :class:`~src.gateway.bot_base.BotSettingsLike`.
    """

    join_url: str = ""
    email: str = ""
    password: str = ""
    display_name: str = "DELEG8"
    headless: bool = False
    profile_dir: Path = PROJECT_ROOT / "data" / "chrome_profile_zoom"
    screenshot_dir: Path = PROJECT_ROOT / "data" / "screenshots"
    lobby_timeout_seconds: float = 300.0
    prejoin_timeout_seconds: float = 45.0
    login_timeout_seconds: float = 45.0
    manual_login_seconds: float = 0.0
    health_interval_seconds: float = 5.0
    rejoin_attempts: int = 2
    window_width: int = 1280
    window_height: int = 800
    chromium_pulse_sink: str = ""
    chromium_pulse_source: str = ""

    @staticmethod
    def is_configured() -> bool:
        """True when a Zoom meeting link is present."""
        return bool(os.getenv("ZOOM_JOIN_URL", "").strip())

    @property
    def has_credentials(self) -> bool:
        """True when the bot should sign in rather than join as a guest."""
        return bool(self.email and self.password)

    @classmethod
    def from_env(cls, *, require_url: bool = True) -> ZoomSettings:
        """Build settings from ``BOT_*`` / ``ZOOM_JOIN_URL`` variables.

        Raises:
            MeetConfigError: with setup instructions when a value is missing or malformed.
        """
        join_url = env_str("ZOOM_JOIN_URL")
        if require_url and not join_url:
            raise MeetConfigError(f"ZOOM_JOIN_URL is not set.\n\n{ZOOM_SETUP_INSTRUCTIONS}")
        if join_url:
            zoom_web_client_url(join_url)  # validates it looks like a Zoom link
        email = env_str("BOT_ZOOM_EMAIL")
        password = env_str("BOT_ZOOM_PASSWORD")
        if bool(email) != bool(password):
            raise MeetConfigError("Set both BOT_ZOOM_EMAIL and BOT_ZOOM_PASSWORD, or neither (guest join).")
        return cls(
            join_url=join_url,
            email=email,
            password=password,
            display_name=env_str("BOT_DISPLAY_NAME", "DELEG8"),
            headless=env_bool("BOT_HEADLESS", False),
            profile_dir=resolve_path(env_str("BOT_ZOOM_PROFILE_DIR", "data/chrome_profile_zoom")),
            screenshot_dir=resolve_path(env_str("BOT_SCREENSHOT_DIR", "data/screenshots")),
            lobby_timeout_seconds=env_number("BOT_LOBBY_TIMEOUT_SECONDS", 300.0, minimum=5.0),
            prejoin_timeout_seconds=env_number("BOT_PREJOIN_TIMEOUT_SECONDS", 45.0, minimum=5.0),
            login_timeout_seconds=env_number("BOT_LOGIN_TIMEOUT_SECONDS", 45.0, minimum=5.0),
            manual_login_seconds=env_number("BOT_MANUAL_LOGIN_SECONDS", 0.0, minimum=0.0),
            health_interval_seconds=env_number("BOT_HEALTH_INTERVAL_SECONDS", 5.0, minimum=1.0),
            rejoin_attempts=int(env_number("BOT_REJOIN_ATTEMPTS", 2, minimum=0)),
            chromium_pulse_sink=env_str(BOT_CHROMIUM_PULSE_SINK_ENV),
            chromium_pulse_source=env_str(BOT_CHROMIUM_PULSE_SOURCE_ENV),
        )


class ZoomBot:
    """Drives one Chromium window into (and out of) a Zoom call via the web client."""

    def __init__(
        self,
        settings: ZoomSettings,
        on_status: Callable[[MeetStatus, str | None], None] | None = None,
    ) -> None:
        """Bind the settings; nothing launches until :meth:`start`."""
        self._settings = settings
        self._on_status = on_status
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self.status: MeetStatus = MeetStatus.IDLE
        self.joined_at: float | None = None
        self.last_reason: str | None = None
        self.participant_count: int | None = None
        self._status_changed: asyncio.Event = asyncio.Event()

    @property
    def settings(self) -> ZoomSettings:
        """The settings this bot runs with."""
        return self._settings

    @property
    def page(self) -> Page:
        """The Zoom tab. Raises if the browser is not running."""
        if self._page is None or self._page.is_closed():
            raise RuntimeError("ZoomBot.start() must be awaited first (or the browser was closed)")
        return self._page

    def _set_status(self, status: MeetStatus, reason: str | None = None) -> None:
        if status is not self.status:
            logger.info("zoom status %s -> %s%s", self.status.value, status.value, f" ({reason})" if reason else "")
        self.status = status
        if reason:
            self.last_reason = reason
        self._status_changed.set()
        self._status_changed = asyncio.Event()
        if self._on_status is not None:
            try:
                self._on_status(status, reason)
            except Exception:
                logger.exception("zoom status observer failed")

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Launch Chromium with a persistent profile and the media flags. Idempotent."""
        if self._context is not None:
            return
        settings = self._settings
        settings.profile_dir.mkdir(parents=True, exist_ok=True)
        settings.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._set_status(MeetStatus.STARTING)
        self._playwright = await async_playwright().start()
        args = [
            "--use-fake-ui-for-media-stream",
            "--disable-blink-features=AutomationControlled",
            "--autoplay-policy=no-user-gesture-required",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
            "--lang=en-US",
            f"--window-size={settings.window_width},{settings.window_height}",
        ]
        launch_kwargs: dict[str, object] = {
            "user_data_dir": str(settings.profile_dir),
            "headless": settings.headless,
            "args": args,
            "ignore_default_args": ["--mute-audio", "--enable-automation"],
            "viewport": {"width": settings.window_width, "height": settings.window_height},
            "locale": "en-US",
            "permissions": ["microphone", "camera", "notifications"],
        }
        pulse_env = chromium_pulse_env(settings.chromium_pulse_sink, settings.chromium_pulse_source)
        if pulse_env:
            launch_kwargs["env"] = {**os.environ, **pulse_env}
            # See the identical comment in meet_bot.py: verified live that
            # Chromium's sandbox otherwise blocks it from reaching PulseAudio's
            # virtual devices inside a container, even though they exist and
            # are visible to `pactl`. Container mode only.
            args.append("--no-sandbox")
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        except PlaywrightError as exc:
            await self._shutdown_playwright()
            self._set_status(MeetStatus.ERROR, str(exc))
            if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
                raise MeetConfigError(
                    "Playwright's Chromium is not installed. Run:  python -m playwright install chromium"
                ) from exc
            raise MeetConfigError(f"Could not launch Chromium: {exc}") from exc
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._page.set_default_timeout(15_000)
        await self._context.grant_permissions(["microphone", "camera"], origin="https://zoom.us")
        logger.info("chromium started for zoom (headless=%s, profile=%s)", settings.headless, settings.profile_dir)
        self._set_status(MeetStatus.IDLE)

    async def stop(self) -> None:
        """Close the browser. Idempotent."""
        await self._shutdown_playwright()
        if self.status not in (MeetStatus.ERROR, MeetStatus.REMOVED, MeetStatus.ENDED):
            self._set_status(MeetStatus.LEFT if self.joined_at else MeetStatus.IDLE)

    async def _shutdown_playwright(self) -> None:
        context, playwright = self._context, self._playwright
        self._context, self._page, self._playwright = None, None, None
        try:
            if context is not None:
                await context.close()
        except PlaywrightError as exc:  # pragma: no cover
            logger.debug("context close: %s", exc)
        finally:
            if playwright is not None:
                await playwright.stop()

    async def wait_for_status_change(self) -> MeetStatus:
        """Block until :attr:`status` changes and return the new value."""
        await self._status_changed.wait()
        return self.status

    # ------------------------------------------------------------------ #
    # Sign-in
    # ------------------------------------------------------------------ #

    async def is_signed_in(self) -> bool:
        """Best-effort check: open the Zoom profile page and look for a signed-out redirect."""
        page = self.page
        await page.goto("https://zoom.us/profile", wait_until="domcontentloaded")
        await self._settle(2.0)
        return "signin" not in page.url and "login" not in page.url

    async def login(self) -> bool:
        """Sign the bot's Zoom account in, unless the persistent profile already is.

        Raises:
            MeetLoginError: sign-in did not succeed and no human finished it within
                ``manual_login_seconds``.
        """
        settings = self._settings
        if not settings.has_credentials:
            logger.info("no bot credentials; joining Zoom as guest %r", settings.display_name)
            return False
        self._set_status(MeetStatus.LOGGING_IN)
        if await self.is_signed_in():
            self._set_status(MeetStatus.IDLE)
            return True

        page = self.page
        await page.goto(ZOOM_SIGNIN_URL, wait_until="domcontentloaded")
        await self._settle(1.0)
        try:
            await page.locator(EMAIL_INPUT).first.fill(settings.email, timeout=15_000)
            await page.locator(PASSWORD_INPUT).first.fill(settings.password, timeout=15_000)
            await page.locator(SIGNIN_BUTTON).first.click(timeout=8_000)
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            await self._fail(MeetStatus.ERROR, f"sign-in form: {exc}", "login")
            raise MeetLoginError(self._login_hint(str(exc))) from exc

        deadline = time.monotonic() + settings.login_timeout_seconds
        manual_deadline = deadline + settings.manual_login_seconds
        warned = False
        while True:
            if await self.is_signed_in():
                self._set_status(MeetStatus.IDLE)
                logger.info("signed in as %s", settings.email)
                return True
            problem = await self._visible_text(LOGIN_BLOCKED_TEXTS)
            now = time.monotonic()
            stalled = problem is not None or now >= deadline
            if stalled and settings.manual_login_seconds <= 0:
                await self._fail(MeetStatus.ERROR, f"sign-in: {problem or 'timed out'}", "login")
                raise MeetLoginError(self._login_hint(problem or "timed out signing in"))
            if stalled and not warned:
                warned = True
                remaining = manual_deadline - now
                print(
                    f"[ZOOM] sign-in needs a human ({problem or 'taking a while'}). "
                    f"Complete it in the Chromium window within {remaining:.0f}s.",
                    flush=True,
                )
            if now >= manual_deadline:
                await self._fail(MeetStatus.ERROR, f"sign-in: {problem or 'timed out'} (manual window elapsed)", "login")
                raise MeetLoginError(self._login_hint(problem or "timed out signing in"))
            await asyncio.sleep(1.0)

    @staticmethod
    def _login_hint(problem: str) -> str:
        return (
            f"Zoom sign-in failed: {problem}. Options: set BOT_MANUAL_LOGIN_SECONDS=300 and complete "
            "the sign-in in the window once (the profile keeps the session), or leave BOT_ZOOM_EMAIL "
            "empty to join as a guest."
        )

    # ------------------------------------------------------------------ #
    # Joining
    # ------------------------------------------------------------------ #

    async def inspect_prejoin(self, url: str) -> PrejoinReport:
        """Open ``url`` up to the pre-join screen and report what is there. Never clicks Join."""
        web_url = zoom_web_client_url(url)
        page = self.page
        signed_in = await self.is_signed_in()
        await page.goto(web_url, wait_until="domcontentloaded")
        await self._settle(3.0)
        await self._dismiss_popups()
        blocking = await self._visible_text(INVALID_TEXTS + DENIED_TEXTS)
        join = page.locator(JOIN_BUTTON).first
        label: str | None = None
        if await join.count() > 0 and await join.is_visible():
            label = (await join.get_attribute("aria-label")) or (await join.inner_text()).strip()
        report = PrejoinReport(
            url=web_url,
            signed_in=signed_in,
            join_button_label=label,
            name_input_present=await self._visible(NAME_INPUT),
            mic_toggle_present=False,  # Zoom's audio choice happens after joining ("Join Audio by Computer")
            camera_toggle_present=False,
            blocking_text=blocking,
            screenshot=await self._screenshot("prejoin"),
        )
        if not report.name_input_present and label is None and blocking is None:
            report.notes.append("no name input and no join button: this meeting may require a signed-in Zoom account")
        return report

    async def join_meeting(self, url: str | None = None) -> None:
        """Navigate to the Zoom link, get through the pre-join screen, and wait to be admitted.

        Raises:
            MeetJoinError: invalid link, denied by the host, or waiting-room timeout.
        """
        raw_url = (url or self._settings.join_url).strip()
        web_url = zoom_web_client_url(raw_url)
        settings = self._settings
        page = self.page
        self._set_status(MeetStatus.JOINING, "opening zoom link")
        await page.goto(web_url, wait_until="domcontentloaded")
        await self._settle(2.0)
        await self._dismiss_popups()

        deadline = time.monotonic() + settings.prejoin_timeout_seconds
        while True:
            if await self._visible(NAME_INPUT) or await self._visible(JOIN_BUTTON):
                break
            blocking = await self._visible_text(INVALID_TEXTS + DENIED_TEXTS)
            if blocking:
                await self._fail(MeetStatus.ERROR, f"cannot join: {blocking}", "join-blocked")
                raise MeetJoinError(f"Zoom will not let the bot in ({blocking}). Check ZOOM_JOIN_URL.")
            if time.monotonic() >= deadline:
                await self._fail(MeetStatus.ERROR, "pre-join screen missing", "join-prejoin")
                raise MeetJoinError(
                    f"The pre-join screen did not appear within {settings.prejoin_timeout_seconds:.0f}s. "
                    "A screenshot was saved - Zoom's web client DOM may not match this bot's selectors."
                )
            await self._dismiss_popups()
            await asyncio.sleep(1.0)

        name_input = page.locator(NAME_INPUT).first
        if await name_input.count() > 0 and await name_input.is_visible():
            await name_input.fill(settings.display_name)

        clicked_at = time.monotonic()
        join_button = page.locator(JOIN_BUTTON).first
        if await join_button.count() > 0:
            await join_button.click(timeout=8_000)
        self._set_status(MeetStatus.JOINING, "clicked Join")

        deadline = time.monotonic() + settings.lobby_timeout_seconds
        audio_joined = False
        while time.monotonic() < deadline:
            if not audio_joined:
                audio_joined = await self._join_audio()
            if await self._in_call():
                self.joined_at = time.monotonic()
                self._set_status(MeetStatus.IN_CALL, "in the call")
                await self._settle(1.0)
                await self._dismiss_popups()
                if not audio_joined:
                    audio_joined = await self._join_audio()
                print("[ZOOM] in the call.", flush=True)
                return
            denied = await self._visible_text(DENIED_TEXTS)
            if denied:
                elapsed = time.monotonic() - clicked_at
                reason = self._denied_message(denied, elapsed)
                await self._fail(MeetStatus.ERROR, f"not admitted: {denied} [{elapsed:.1f}s]", "join-denied")
                raise MeetJoinError(reason)
            if self.status is MeetStatus.JOINING and await self._visible_text(WAITING_ROOM_TEXTS):
                self._set_status(MeetStatus.LOBBY, "waiting room")
            await asyncio.sleep(1.0)
        await self._fail(MeetStatus.ERROR, "waiting-room timeout", "join-timeout")
        raise MeetJoinError(f"Nobody admitted the bot within {settings.lobby_timeout_seconds:.0f}s (BOT_LOBBY_TIMEOUT_SECONDS).")

    @classmethod
    def _denied_message(cls, denied: str, elapsed: float) -> str:
        if elapsed < _INSTANT_REJECTION_SECONDS:
            return (
                f"Rejected {elapsed:.1f}s after clicking - too fast to be the host deciding ({denied!r}). "
                "This usually means the meeting requires an authenticated/allowed account; set "
                "BOT_ZOOM_EMAIL/BOT_ZOOM_PASSWORD so the bot signs in, or ask the host to allow guests."
            )
        return f"The host did not admit the bot within {elapsed:.0f}s ({denied})."

    async def _join_audio(self) -> bool:
        """Click "Join Audio by Computer" if it is showing. Best effort, never raises."""
        try:
            button = self.page.locator(JOIN_AUDIO_BUTTON).first
            if await button.count() > 0 and await button.is_visible():
                await button.click(timeout=5_000)
                await asyncio.sleep(0.5)
                logger.info("joined Zoom audio by computer")
                return True
        except PlaywrightError as exc:
            logger.debug("join-audio click failed: %s", exc)
        return False

    async def leave(self) -> None:
        """Click "Leave" (and confirm) if the bot is in a call."""
        if self._page is None or self._page.is_closed():
            return
        try:
            leave = self._page.locator(LEAVE_BUTTON).first
            if await leave.count() > 0 and await leave.is_visible():
                await leave.click(timeout=5_000)
                await asyncio.sleep(0.5)
                confirm = self._page.locator(LEAVE_CONFIRM_BUTTON).first
                if await confirm.count() > 0 and await confirm.is_visible():
                    await confirm.click(timeout=5_000)
                await asyncio.sleep(1.0)
                logger.info("left the Zoom call")
        except PlaywrightError as exc:
            logger.warning("could not click Leave: %s", exc)
        if self.status in (MeetStatus.IN_CALL, MeetStatus.LOBBY, MeetStatus.JOINING, MeetStatus.RECONNECTING):
            self._set_status(MeetStatus.LEFT, "left")

    # ------------------------------------------------------------------ #
    # Health
    # ------------------------------------------------------------------ #

    async def get_participant_count(self) -> int | None:
        """Best-effort headcount of everyone in the call, this bot included. See meet_bot's version."""
        try:
            tiles = await self._count(PARTICIPANT_TILE_SELECTOR)
            if tiles > 0:
                return tiles
            badge = self.page.locator(PARTICIPANTS_BUTTON).first
            if await badge.count() > 0:
                label = (await badge.get_attribute("aria-label")) or await badge.inner_text()
                match = re.search(r"\((\d+)\)|(\d+)\s*$", label or "")
                if match:
                    return int(match.group(1) or match.group(2))
        except PlaywrightError as exc:
            logger.debug("participant count unavailable: %s", exc)
        return None

    async def check_health(self) -> str | None:
        """One health probe: ``None`` while in the call, otherwise why it is not."""
        if self._page is None or self._page.is_closed():
            self._set_status(MeetStatus.ERROR, "browser page closed")
            return "browser page closed"
        try:
            if await self._in_call():
                self.participant_count = await self.get_participant_count()
                if self.status is not MeetStatus.IN_CALL:
                    self.joined_at = self.joined_at or time.monotonic()
                    self._set_status(MeetStatus.IN_CALL, "reconnected")
                return None
            removed = await self._visible_text(REMOVED_TEXTS)
            if removed:
                self._set_status(MeetStatus.REMOVED, removed)
                return removed
            lost = await self._visible_text(CONNECTION_LOST_TEXTS)
            if lost:
                self._set_status(MeetStatus.RECONNECTING, lost)
                return lost
            ended = await self._visible_text(ENDED_TEXTS)
            if ended:
                self._set_status(MeetStatus.ENDED, ended)
                return ended
        except PlaywrightError as exc:
            self._set_status(MeetStatus.ERROR, str(exc))
            return f"browser error: {exc}"
        self._set_status(MeetStatus.ENDED, "the Leave control disappeared")
        return "the Leave control disappeared"

    async def monitor(self) -> str:
        """Poll :meth:`check_health` every ``health_interval_seconds``; return the reason once lost."""
        while True:
            await asyncio.sleep(self._settings.health_interval_seconds)
            reason = await self.check_health()
            if reason is not None:
                logger.warning("no longer in the Zoom call: %s", reason)
                return reason

    async def stay_connected(self) -> str:
        """Monitor the call and rejoin after a dropped connection; return why the bot is finally out."""
        attempts = 0
        while True:
            reason = await self.monitor()
            recoverable = self.status is MeetStatus.RECONNECTING
            if not recoverable or attempts >= self._settings.rejoin_attempts:
                await self._screenshot("disconnected")
                return reason
            attempts += 1
            print(f"[ZOOM] connection lost ({reason}); rejoin attempt {attempts}/{self._settings.rejoin_attempts}", flush=True)
            await asyncio.sleep(3.0)
            try:
                await self.join_meeting()
            except (MeetJoinError, MeetConfigError, PlaywrightError) as exc:
                return f"rejoin failed: {exc}"

    # ------------------------------------------------------------------ #
    # In-page helpers (identical pattern to meet_bot.py)
    # ------------------------------------------------------------------ #

    async def _in_call(self) -> bool:
        return await self._visible(LEAVE_BUTTON)

    async def _visible(self, selector: str) -> bool:
        try:
            locator = self.page.locator(selector).first
            return await locator.count() > 0 and await locator.is_visible()
        except PlaywrightError:
            return False

    async def _count(self, selector: str) -> int:
        try:
            return await self.page.locator(selector).count()
        except PlaywrightError:
            return 0

    async def _visible_text(self, texts: Sequence[str]) -> str | None:
        for text in texts:
            try:
                locator = self.page.get_by_text(text, exact=False).first
                if await locator.count() > 0 and await locator.is_visible():
                    return text
            except PlaywrightError:
                continue
        return None

    async def _settle(self, seconds: float) -> None:
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except PlaywrightTimeoutError:
            pass
        await asyncio.sleep(seconds)

    async def _dismiss_popups(self) -> None:
        for _ in range(3):
            try:
                button = self.page.locator(DISMISS_BUTTONS).first
                if await button.count() == 0 or not await button.is_visible():
                    return
                await button.click(timeout=3_000)
                await asyncio.sleep(0.5)
            except PlaywrightError:
                return

    async def _screenshot(self, label: str) -> Path | None:
        if self._page is None or self._page.is_closed():
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = self._settings.screenshot_dir / f"{stamp}-zoom-{label}.png"
        try:
            await self._page.screenshot(path=str(path), full_page=False)
            return path
        except PlaywrightError as exc:
            logger.debug("screenshot failed: %s", exc)
            return None

    async def _fail(self, status: MeetStatus, reason: str, label: str) -> None:
        shot = await self._screenshot(label)
        self._set_status(status, reason + (f"; screenshot {shot}" if shot else ""))


# --------------------------------------------------------------------------- #
# Standalone inspection - a thin wrapper around inspect_prejoin() for testing
# this one platform without touching MEET_JOIN_URL / TEAMS_JOIN_URL. The
# platform-generic path is  python -m src.main join --dry-run --url <link>
# (src.gateway.session._dry_run), which calls the same inspect_prejoin().
# --------------------------------------------------------------------------- #


async def _inspect(url: str) -> int:
    settings = ZoomSettings.from_env(require_url=False)
    bot = ZoomBot(settings)
    await bot.start()
    try:
        if settings.has_credentials:
            try:
                await bot.login()
            except MeetLoginError as exc:
                print(f"[ZOOM] sign-in failed: {exc}")
        report = await bot.inspect_prejoin(url)
        print(f"[ZOOM] pre-join inspection of {url}")
        print(f"       web client url  : {report.url}")
        print(f"       signed in       : {report.signed_in}")
        print(f"       join button     : {report.join_button_label!r}")
        print(f"       name input      : {report.name_input_present}")
        print(f"       blocking text   : {report.blocking_text!r}")
        print(f"       screenshot      : {report.screenshot}")
        for note in report.notes:
            print(f"       note            : {note}")
        print(f"       ready to join   : {report.ready}")
        return 0 if report.ready else 1
    finally:
        await bot.stop()


def _main(argv: list[str]) -> int:
    import argparse

    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description="Inspect a Zoom meeting's pre-join screen without joining.")
    parser.add_argument("--inspect", metavar="URL", required=True, help="Zoom meeting link to inspect")
    args = parser.parse_args(argv)
    return asyncio.run(_inspect(args.inspect))


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))


__all__ = [
    "ZOOM_SETUP_INSTRUCTIONS",
    "ZoomBot",
    "ZoomSettings",
    "zoom_meeting_id",
    "zoom_web_client_url",
]
