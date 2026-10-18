"""
Google Meet ingress: a Playwright-driven Chromium that joins a call as the delegate.

"Headless" here means unattended, not windowless. Chromium only exposes real
audio devices when it runs with a window, so by default the browser opens
one (``BOT_HEADLESS=false``) and every byte of audio goes through virtual
cables that :mod:`src.audio.device_setup` helps you pick:

    Meet participants -> Chromium speaker -> cable A -> DELEG8 mic     -> Deepgram
    Cartesia TTS      -> DELEG8 speaker   -> cable B -> Chromium mic   -> Meet

Lifecycle::

    bot = GoogleMeetBot(MeetSettings.from_env())
    await bot.start()                 # Chromium with a persistent profile
    await bot.login()                 # skipped when the profile is already signed in
    await bot.join_meeting(url)       # pre-join screen -> "Join now" / "Ask to join" -> lobby -> in call
    reason = await bot.stay_connected()   # health loop; rejoins after a dropped connection
    await bot.leave(); await bot.stop()

Every selector below targets Google Meet's DOM as of 2026. Google changes it
without notice; when a step cannot find its control it saves a screenshot to
``BOT_SCREENSHOT_DIR`` and raises with the text it saw, so the fix is a
one-line selector change rather than a debugging session.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from playwright.async_api import (
    BrowserContext,
    Locator,
    Page,
    Playwright,
    async_playwright,
)
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
    pin_microphone_script,
    resolve_path,
)

logger = logging.getLogger("deleg8.gateway.meet")

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

MEET_URL_RE = re.compile(r"^https://meet\.google\.com/(?P<code>[a-z]{3}-[a-z]{4}-[a-z]{3})(?:[/?#].*)?$")
MEET_LANDING_URL: str = "https://meet.google.com/?hl=en"
GOOGLE_SIGNIN_URL: str = "https://accounts.google.com/ServiceLogin?hl=en&continue=https%3A%2F%2Fmeet.google.com%2F"

MEET_SETUP_INSTRUCTIONS: str = """\
How to configure the DELEG8 Google Meet bot
-------------------------------------------
1. Browser
   python -m playwright install chromium        (one-off download of the Playwright Chromium build)

2. Bot identity
   Create a throwaway Google account for the bot and put it in .env:
       BOT_GOOGLE_EMAIL=deleg8.bot@gmail.com
       BOT_GOOGLE_PASSWORD=...
   Leave both empty to join as an anonymous guest instead - but many Workspace/
   business meetings auto-reject anonymous guests before the host ever sees a
   request ("You can't join this video call", rejected in under a second). If
   that happens, this is why: set the two variables above so the bot signs in.
   Google sometimes refuses scripted sign-ins ("This browser or app may not be secure").
   Then set BOT_MANUAL_LOGIN_SECONDS=300 and complete the sign-in yourself in the window
   the bot opens; the session is saved to BOT_CHROME_PROFILE_DIR and reused afterwards.
   Signing in is not always enough either: if the meeting is restricted to the
   host's organization, ask the host to add the bot's email as a guest on the
   invite, or to enable "Quick access" for people outside the organization.

3. The meeting
       MEET_JOIN_URL=https://meet.google.com/abc-defg-hij

4. Audio routing (two virtual cables, e.g. BlackHole 2ch + BlackHole 16ch on macOS)
   python -m src.audio.cable_check --apply
   This measures each cable rather than trusting its name - a driver that is
   installed but never loaded enumerates and opens normally while carrying no
   audio at all - and points the system defaults at the two that work.
   Chromium must run with a window (BOT_HEADLESS=false): headless Chromium has no audio devices.
"""

# --------------------------------------------------------------------------- #
# Selectors and texts (Google Meet DOM, 2026)
# --------------------------------------------------------------------------- #

JOIN_BUTTON: str = (
    '[aria-label="Join now"], [aria-label="Ask to join"], '
    'button:has-text("Join now"), button:has-text("Ask to join")'
)
NAME_INPUT: str = 'input[aria-label="Your name"], input[placeholder="Your name"]'
MIC_OFF_BUTTON: str = '[aria-label*="Turn off microphone"]'
MIC_ON_BUTTON: str = '[aria-label*="Turn on microphone"]'
CAMERA_OFF_BUTTON: str = '[aria-label*="Turn off camera"]'
CAMERA_ON_BUTTON: str = '[aria-label*="Turn on camera"]'
LEAVE_BUTTON: str = '[aria-label="Leave call"], button[aria-label*="Leave call"]'
MORE_OPTIONS_BUTTON: str = '[aria-label="More options"]'
SETTINGS_MENU_ITEM: str = '[role="menuitem"]:has-text("Settings")'
MIC_SELECT: str = '[aria-label="Microphone"][role="combobox"], [role="combobox"]:has-text("Microphone")'
SPEAKER_SELECT: str = '[aria-label="Speakers"][role="combobox"], [role="combobox"]:has-text("Speakers")'
CLOSE_DIALOG_BUTTON: str = '[aria-label="Close dialog"], [aria-label="Close"]'
DISMISS_BUTTONS: str = (
    'button:has-text("Got it"), button:has-text("Dismiss"), button:has-text("Not now"), '
    'button:has-text("No thanks"), button:has-text("Continue without microphone and camera")'
)
ACCOUNT_AVATAR: str = '[aria-label^="Google Account"], a[aria-label^="Google Account"]'
SIGN_IN_LINK: str = 'a:has-text("Sign in"), a[href*="accounts.google.com/ServiceLogin"]'
EMAIL_INPUT: str = 'input[type="email"]'
EMAIL_NEXT: str = '#identifierNext, button:has-text("Next")'
USE_ANOTHER_ACCOUNT_TEXT: str = "Use another account"
"""Shown on Google's account-chooser screen when the profile already has a remembered identity."""
PASSWORD_INPUT: str = 'input[type="password"]'
PASSWORD_NEXT: str = '#passwordNext, button:has-text("Next")'

LOBBY_TEXTS: tuple[str, ...] = (
    "Asking to join",
    "Someone will let you in",
    "You'll join the call when someone lets you in",
)
DENIED_TEXTS: tuple[str, ...] = (
    "denied your request",
    "You can't join this call",
    "You can't join this video call",
    "No one responded to your request",
    "can't join this meeting",
)
INVALID_TEXTS: tuple[str, ...] = (
    "Check your meeting code",
    "meeting code you entered doesn't work",
    "Invalid video call name",
    "You can't create a meeting yourself",
    "You can't join this video call",
    "This video call is unavailable",
    "This meeting hasn't started",
)
REMOVED_TEXTS: tuple[str, ...] = (
    "You've been removed from the meeting",
    "removed you from the meeting",
)
ENDED_TEXTS: tuple[str, ...] = (
    "The call ended",
    "The meeting has ended",
    "This call has ended",
    "You left the meeting",
    "Return to home screen",
)
CONNECTION_LOST_TEXTS: tuple[str, ...] = (
    "You lost your connection",
    "Trying to reconnect",
    "Your connection was lost",
)
LOGIN_BLOCKED_TEXTS: tuple[str, ...] = (
    "This browser or app may not be secure",
    "Couldn't sign you in",
    "Couldn't find your Google Account",
    "Wrong password",
)
PARTICIPANT_TILE_SELECTOR: str = "[data-participant-id]"
"""Elements in the call grid tagged with whose stream they show.

One person owns several of these at once (a main tile and a filmstrip
thumbnail, say), so :meth:`GoogleMeetBot.get_participant_count` counts the
distinct ``data-participant-id`` *values* rather than the elements. Best-effort
like everything else in this file: it prefers the People-panel badge and falls
back to ``None`` (unknown) rather than raising."""
PEOPLE_COUNT_BADGE: str = (
    '[aria-label*="Show everyone"], [aria-label*="People"], [aria-label*="participants"], '
    '[aria-label*="Participants"]'
)
_PEOPLE_COUNT_RE = re.compile(r"\((\d+)\)|\b(\d+)\s+(?:people|participants?)\b", re.IGNORECASE)

LOGIN_CHALLENGE_TEXTS: tuple[str, ...] = (
    "2-Step Verification",
    "Verify it's you",
    "Confirm your recovery",
    "Check your phone",
    "Enter the code",
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# MeetConfigError, MeetLoginError, MeetJoinError, and the _env_*/_resolve
# helpers now live in bot_base.py, shared across every platform's bot;
# imported above and kept importable from here under the same names
# (_env_str etc. aliased below) for every existing caller.
_env_str = env_str
_env_bool = env_bool
_env_number = env_number
_resolve = resolve_path


def meeting_code(url: str) -> str:
    """Return the ``abc-defg-hij`` code from a Meet link.

    Raises:
        MeetConfigError: when ``url`` is not a Google Meet link.
    """
    match = MEET_URL_RE.match(url.strip())
    if not match:
        raise MeetConfigError(
            f"MEET_JOIN_URL must look like https://meet.google.com/abc-defg-hij (got {url!r})."
        )
    return match.group("code")


@dataclass(frozen=True)
class MeetSettings:
    """Everything the bot needs, normally read from the environment."""

    join_url: str = ""
    email: str = ""
    password: str = ""
    display_name: str = "DELEG8"
    headless: bool = False
    """Headless Chromium has no audio devices; only use it for dry runs."""
    profile_dir: Path = PROJECT_ROOT / "data" / "chrome_profile"
    screenshot_dir: Path = PROJECT_ROOT / "data" / "screenshots"
    lobby_timeout_seconds: float = 300.0
    """How long to wait for the host to admit the bot."""
    prejoin_timeout_seconds: float = 45.0
    """How long the pre-join screen may take to render."""
    login_timeout_seconds: float = 45.0
    manual_login_seconds: float = 0.0
    """When > 0, a blocked or challenged sign-in waits this long for a human to finish it in the window."""
    health_interval_seconds: float = 5.0
    rejoin_attempts: int = 2
    """Rejoin attempts after a dropped connection (not after being removed)."""
    meet_mic_name: str = ""
    """Device name to pick in Meet's audio settings (default: system default input)."""
    meet_speaker_name: str = ""
    """Device name to pick in Meet's audio settings (default: system default output)."""
    window_width: int = 1280
    window_height: int = 800
    chromium_pulse_sink: str = ""
    chromium_pulse_source: str = ""
    """Set only inside the containerised worker (docker/entrypoint.sh); see
    :func:`src.gateway.bot_base.chromium_pulse_env`. Empty locally - Chromium
    then inherits the parent process's environment and its system audio
    devices exactly as before."""

    @staticmethod
    def is_configured() -> bool:
        """True when a meeting link is present."""
        return bool(os.getenv("MEET_JOIN_URL", "").strip())

    @property
    def has_credentials(self) -> bool:
        """True when the bot should sign in rather than join as a guest."""
        return bool(self.email and self.password)

    @classmethod
    def from_env(cls, *, require_url: bool = True) -> MeetSettings:
        """Build settings from ``BOT_*`` / ``MEET_JOIN_URL`` variables.

        Raises:
            MeetConfigError: with setup instructions when a value is missing or malformed.
        """
        join_url = _env_str("MEET_JOIN_URL")
        if require_url and not join_url:
            raise MeetConfigError(f"MEET_JOIN_URL is not set.\n\n{MEET_SETUP_INSTRUCTIONS}")
        if join_url:
            meeting_code(join_url)
        email = _env_str("BOT_GOOGLE_EMAIL")
        password = _env_str("BOT_GOOGLE_PASSWORD")
        if bool(email) != bool(password):
            raise MeetConfigError("Set both BOT_GOOGLE_EMAIL and BOT_GOOGLE_PASSWORD, or neither (guest join).")
        return cls(
            join_url=join_url,
            email=email,
            password=password,
            display_name=_env_str("BOT_DISPLAY_NAME", "DELEG8"),
            headless=_env_bool("BOT_HEADLESS", False),
            profile_dir=_resolve(_env_str("BOT_CHROME_PROFILE_DIR", "data/chrome_profile")),
            screenshot_dir=_resolve(_env_str("BOT_SCREENSHOT_DIR", "data/screenshots")),
            lobby_timeout_seconds=_env_number("BOT_LOBBY_TIMEOUT_SECONDS", 300.0, minimum=5.0),
            prejoin_timeout_seconds=_env_number("BOT_PREJOIN_TIMEOUT_SECONDS", 45.0, minimum=5.0),
            login_timeout_seconds=_env_number("BOT_LOGIN_TIMEOUT_SECONDS", 45.0, minimum=5.0),
            manual_login_seconds=_env_number("BOT_MANUAL_LOGIN_SECONDS", 0.0, minimum=0.0),
            health_interval_seconds=_env_number("BOT_HEALTH_INTERVAL_SECONDS", 5.0, minimum=1.0),
            rejoin_attempts=int(_env_number("BOT_REJOIN_ATTEMPTS", 2, minimum=0)),
            meet_mic_name=_env_str("BOT_MEET_MIC_NAME"),
            meet_speaker_name=_env_str("BOT_MEET_SPEAKER_NAME"),
            chromium_pulse_sink=_env_str(BOT_CHROMIUM_PULSE_SINK_ENV),
            chromium_pulse_source=_env_str(BOT_CHROMIUM_PULSE_SOURCE_ENV),
        )


# MeetStatus and PrejoinReport now live in bot_base.py, shared across every
# platform's bot; imported above and kept importable from here under the same names.


# --------------------------------------------------------------------------- #
# The bot
# --------------------------------------------------------------------------- #


class GoogleMeetBot:
    """Drives one Chromium window into (and out of) a Google Meet call."""

    def __init__(
        self,
        settings: MeetSettings,
        on_status: Callable[[MeetStatus, str | None], None] | None = None,
    ) -> None:
        """Bind the settings; nothing launches until :meth:`start`.

        ``on_status`` is called synchronously on every status change with the new
        status and the reason (if any); used by the web console's live feed.
        """
        self._settings = settings
        self._on_status = on_status
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self.status: MeetStatus = MeetStatus.IDLE
        self.joined_at: float | None = None
        self.last_reason: str | None = None
        self.participant_count: int | None = None
        """Total attendees in the call right now, this bot included; refreshed on
        every :meth:`check_health` tick. ``None`` until the first successful read,
        or permanently on a Meet DOM this bot's selectors do not recognise -
        callers must treat that as "unknown", never as a specific number."""
        self._status_changed: asyncio.Event = asyncio.Event()

    @property
    def settings(self) -> MeetSettings:
        """The settings this bot runs with."""
        return self._settings

    @property
    def page(self) -> Page:
        """The Meet tab. Raises if the browser is not running."""
        if self._page is None or self._page.is_closed():
            raise RuntimeError("GoogleMeetBot.start() must be awaited first (or the browser was closed)")
        return self._page

    def _set_status(self, status: MeetStatus, reason: str | None = None) -> None:
        if status is not self.status:
            logger.info("meet status %s -> %s%s", self.status.value, status.value, f" ({reason})" if reason else "")
        self.status = status
        if reason:
            self.last_reason = reason
        self._status_changed.set()
        self._status_changed = asyncio.Event()
        if self._on_status is not None:
            try:
                self._on_status(status, reason)
            except Exception:  # noqa: BLE001 - observers are best-effort
                logger.exception("meet status observer failed")

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
            "--use-fake-ui-for-media-stream",          # auto-accept mic/camera prompts
            "--disable-blink-features=AutomationControlled",  # navigator.webdriver = false
            "--autoplay-policy=no-user-gesture-required",     # let Meet play remote audio
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
            "--disable-features=Translate,MediaRouter,OptimizationHints",
            "--lang=en-US",
            f"--window-size={settings.window_width},{settings.window_height}",
        ]
        launch_kwargs: dict[str, object] = {
            "user_data_dir": str(settings.profile_dir),
            "headless": settings.headless,
            "args": args,
            # Playwright mutes audio and flags automation by default; both would break us.
            "ignore_default_args": ["--mute-audio", "--enable-automation"],
            "viewport": {"width": settings.window_width, "height": settings.window_height},
            "locale": "en-US",
            "permissions": ["microphone", "camera", "notifications"],
        }
        pulse_env = chromium_pulse_env(settings.chromium_pulse_sink, settings.chromium_pulse_source)
        if pulse_env:
            # Containerised worker only: route this Chromium's audio to the two
            # virtual devices docker/entrypoint.sh created, distinct from the
            # parent Python process's own PULSE_SINK/PULSE_SOURCE - see
            # src.gateway.bot_base.chromium_pulse_env.
            launch_kwargs["env"] = {**os.environ, **pulse_env}
            # Verified live (2026-09-23): without this, PulseAudio's virtual
            # devices exist and are visible to `pactl` inside the container,
            # but Chromium's own device picker still reports "Mic not found" -
            # its sandbox blocks the renderer/audio process from reaching the
            # PulseAudio socket. This is the standard, well-documented fix for
            # running Chromium inside a container; only applied in container
            # mode (pulse_env non-empty) so local, non-containerised runs keep
            # the sandbox.
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
        if settings.meet_mic_name:
            # Pin the microphone at the source rather than only clicking through
            # Meet's settings dialog: the dialog's DOM changes without notice,
            # and when the cable cannot be made the OS default input (some
            # virtual devices are silently refused) a missed click means the
            # room never hears the delegate at all. Harmless if the device is
            # absent - the page's own request goes through untouched.
            await self._context.add_init_script(pin_microphone_script(settings.meet_mic_name))
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._page.set_default_timeout(15_000)
        await self._context.grant_permissions(["microphone", "camera"], origin="https://meet.google.com")
        logger.info(
            "chromium started (headless=%s, profile=%s)", settings.headless, settings.profile_dir
        )
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
        except PlaywrightError as exc:  # pragma: no cover - browser already gone
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
        """Open the Meet landing page and check for the account avatar.

        Signed-out visitors are redirected to the Workspace marketing page, so
        anything other than the Meet app with an account avatar counts as signed out.
        """
        page = self.page
        await page.goto(MEET_LANDING_URL, wait_until="domcontentloaded")
        await self._settle(2.0)
        on_meet_app = page.url.startswith("https://meet.google.com")
        return on_meet_app and await self._count(ACCOUNT_AVATAR) > 0

    async def _enter_email(self, page: Page, email: str) -> None:
        """Get past Google's identifier step, whatever shape it takes.

        Usually a blank ``input[type=email]``. But once this profile has
        attempted a sign-in before, Google often shows an account-chooser
        screen instead (a tile for the remembered address, or a list, plus
        "Use another account") - no plain input exists on that screen at
        all, which is why a bare fill-and-click is not reliable across runs.
        """
        email_input = page.locator(EMAIL_INPUT).first
        if await email_input.count() == 0 or not await email_input.is_visible():
            tile = page.get_by_text(email, exact=False).first
            if await tile.count() > 0 and await tile.is_visible():
                await tile.click(timeout=8_000)
                await self._settle(1.0)
            else:
                other = page.get_by_text(USE_ANOTHER_ACCOUNT_TEXT, exact=False).first
                if await other.count() > 0 and await other.is_visible():
                    await other.click(timeout=8_000)
                    await self._settle(1.0)

        email_input = page.locator(EMAIL_INPUT).first
        if await email_input.count() == 0 or not await email_input.is_visible():
            # Clicking a remembered-account tile can skip straight past the
            # email step (and sometimes past the password step too, for a
            # still-valid session); the caller's own polling for the password
            # field / arrival at Meet covers that. Nothing left to do here.
            return
        await email_input.fill(email, timeout=15_000)
        next_button = page.locator(EMAIL_NEXT).first
        await next_button.click()
        # The click occasionally does not register (an animated button, a
        # slow re-render); Enter on the field is Google's other accepted way
        # to submit it. Harmless if the click already worked.
        await asyncio.sleep(2.0)
        password_ready = await page.locator(PASSWORD_INPUT).first.is_visible()
        blocked = await self._visible_text(LOGIN_BLOCKED_TEXTS + LOGIN_CHALLENGE_TEXTS)
        if not password_ready and not blocked:
            try:
                await email_input.press("Enter", timeout=3_000)
            except (PlaywrightError, PlaywrightTimeoutError):
                pass

    async def login(self) -> bool:
        """Sign the bot's Google account in, unless the persistent profile already is.

        Returns ``True`` when the browser ends up signed in.

        Raises:
            MeetLoginError: when Google blocks or challenges the sign-in and no
                human finishes it within ``manual_login_seconds``.
        """
        settings = self._settings
        if not settings.has_credentials:
            logger.info("no bot credentials; joining as guest %r", settings.display_name)
            return False
        self._set_status(MeetStatus.LOGGING_IN)
        if await self.is_signed_in():
            logger.info("profile already signed in")
            self._set_status(MeetStatus.IDLE)
            return True

        page = self.page
        await page.goto(GOOGLE_SIGNIN_URL, wait_until="domcontentloaded")
        await self._settle(1.0)
        try:
            await self._enter_email(page, settings.email)
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            seen = await self._visible_text(LOGIN_BLOCKED_TEXTS + LOGIN_CHALLENGE_TEXTS) or "email field never appeared"
            await self._fail(MeetStatus.ERROR, f"sign-in: {seen}", "login")
            raise MeetLoginError(self._login_hint(seen)) from exc

        async def password_shown() -> bool:
            locator = page.locator(PASSWORD_INPUT).first
            return await locator.count() > 0 and await locator.is_visible()

        # Every stage gets the same manual-login grace period: whichever part
        # of Google's flow stalls or shows a challenge, a human watching the
        # window has BOT_MANUAL_LOGIN_SECONDS to finish it by hand.
        problem = await self._wait_for_signin_step(password_shown, stage="entering the password")
        if problem:
            await self._fail(MeetStatus.ERROR, f"sign-in: {problem}", "login")
            raise MeetLoginError(self._login_hint(problem))

        await page.locator(PASSWORD_INPUT).first.fill(settings.password)
        await page.locator(PASSWORD_NEXT).first.click()

        async def reached_meet() -> bool:
            return page.url.startswith("https://meet.google.com")

        problem = await self._wait_for_signin_step(reached_meet, stage="finishing sign-in")
        if problem:
            await self._fail(MeetStatus.ERROR, f"sign-in: {problem}", "login")
            raise MeetLoginError(self._login_hint(problem))
        if not await self.is_signed_in():
            await self._fail(MeetStatus.ERROR, "sign-in: reached Meet but no signed-in account was detected", "login")
            raise MeetLoginError(self._login_hint("reached Meet but no signed-in account was detected"))

        logger.info("signed in as %s", settings.email)
        self._set_status(MeetStatus.IDLE)
        return True

    async def _wait_for_signin_step(self, ready: Callable[[], Awaitable[bool]], *, stage: str) -> str | None:
        """Poll ``ready()`` until it is true; return ``None`` on success or the reason on failure.

        Gives the ordinary ``login_timeout_seconds`` quietly, then - if
        ``manual_login_seconds`` > 0 - keeps polling for that much longer
        while a human finishes the step in the visible browser window
        (printed once). Returns a reason string once time runs out, whether
        that is an explicit Google block/challenge text or a plain timeout.
        """
        settings = self._settings
        deadline = time.monotonic() + settings.login_timeout_seconds
        manual_deadline = deadline + settings.manual_login_seconds
        warned = False
        while True:
            if await ready():
                return None
            now = time.monotonic()
            problem = await self._visible_text(LOGIN_BLOCKED_TEXTS + LOGIN_CHALLENGE_TEXTS)
            stalled = problem is not None or now >= deadline
            if stalled and settings.manual_login_seconds <= 0:
                return problem or f"timed out {stage}"
            if stalled and not warned:
                warned = True
                remaining = manual_deadline - now
                reason = problem or f"taking a while ({stage})"
                logger.warning("sign-in needs a human (%s); finish it in the browser window within %.0fs", reason, remaining)
                print(f"[MEET] Google needs a human for sign-in ({reason}). "
                      f"Complete it in the Chromium window within {remaining:.0f}s.", flush=True)
            if now >= manual_deadline:
                return problem or f"timed out {stage} (including the manual sign-in window)"
            await asyncio.sleep(1.0)

    _INSTANT_REJECTION_SECONDS: float = 8.0
    """Below this, no human host reacted; Meet auto-rejected the request itself."""

    @classmethod
    def _denied_message(cls, denied: str, elapsed: float, *, guest: bool) -> str:
        """Explain a lobby rejection, distinguishing an auto-bounce from a real human decision.

        A rejection within :attr:`_INSTANT_REJECTION_SECONDS` of clicking Join
        cannot be a host tapping Deny; it is Meet's server rejecting the
        request itself, almost always because anonymous (not signed in)
        participants are blocked for this meeting - a very common default for
        Google Workspace / business meetings.
        """
        if elapsed < cls._INSTANT_REJECTION_SECONDS and guest:
            return (
                f"Rejected {elapsed:.1f}s after clicking - too fast to be the host deciding ({denied!r}). "
                "This is Meet auto-blocking an anonymous (not signed in) participant, which many Workspace/"
                "business meetings do by default before the host ever sees a request. Set BOT_GOOGLE_EMAIL and "
                "BOT_GOOGLE_PASSWORD in .env so the bot signs in instead of joining as a guest, then retry. If "
                "it still gets this message while signed in, the account also needs to be invited to the meeting "
                "or be in the same Google Workspace domain as the host - ask the host to add the bot's email as "
                "a guest, or to turn on 'Quick access' for people outside the organization."
            )
        if elapsed < cls._INSTANT_REJECTION_SECONDS:
            return (
                f"Rejected {elapsed:.1f}s after clicking - too fast to be the host deciding ({denied!r}). "
                "The signed-in bot account is not allowed into this meeting (not invited, or not in the host's "
                "Workspace domain). Ask the host to add the bot's email as a guest on the invite, or to allow "
                "external participants."
            )
        return f"The host did not admit the bot within {elapsed:.0f}s ({denied})."

    @staticmethod
    def _login_hint(problem: str) -> str:
        return (
            f"Google sign-in failed: {problem}. Options: set BOT_MANUAL_LOGIN_SECONDS=300 and complete the "
            "sign-in in the window once (the profile keeps the session), use a throwaway account without "
            "2-Step Verification, or leave BOT_GOOGLE_EMAIL empty to join as a guest."
        )

    # ------------------------------------------------------------------ #
    # Joining
    # ------------------------------------------------------------------ #

    async def inspect_prejoin(self, url: str) -> PrejoinReport:
        """Open ``url`` up to the pre-join screen and report what is there. Never clicks Join."""
        meeting_code(url)
        page = self.page
        signed_in = await self.is_signed_in()
        await page.goto(url, wait_until="domcontentloaded")
        await self._settle(3.0)
        await self._dismiss_popups()
        blocking = await self._visible_text(INVALID_TEXTS + DENIED_TEXTS)
        join = page.locator(JOIN_BUTTON).first
        label: str | None = None
        if await join.count() > 0 and await join.is_visible():
            label = (await join.get_attribute("aria-label")) or (await join.inner_text()).strip()
        report = PrejoinReport(
            url=url,
            signed_in=signed_in,
            join_button_label=label,
            name_input_present=await self._visible(NAME_INPUT),
            mic_toggle_present=await self._visible(MIC_OFF_BUTTON) or await self._visible(MIC_ON_BUTTON),
            camera_toggle_present=await self._visible(CAMERA_OFF_BUTTON) or await self._visible(CAMERA_ON_BUTTON),
            blocking_text=blocking,
            screenshot=await self._screenshot("prejoin"),
        )
        if not signed_in and not report.name_input_present and label is None and blocking is None:
            report.notes.append("no name input and no join button: Meet probably wants a sign-in for this link")
        return report

    async def join_meeting(self, url: str | None = None) -> None:
        """Navigate to the Meet link, get through the pre-join screen, and wait to be admitted.

        Raises:
            MeetJoinError: invalid link, denied by the host, or lobby timeout.
        """
        url = (url or self._settings.join_url).strip()
        code = meeting_code(url)
        settings = self._settings
        page = self.page
        self._set_status(MeetStatus.JOINING, f"opening {code}")
        await page.goto(url, wait_until="domcontentloaded")
        await self._settle(2.0)
        await self._dismiss_popups()

        join_button = page.locator(JOIN_BUTTON).first
        prejoin_deadline = time.monotonic() + settings.prejoin_timeout_seconds
        while True:
            if await self._visible(JOIN_BUTTON):
                break
            blocking = await self._visible_text(INVALID_TEXTS + DENIED_TEXTS + LOGIN_BLOCKED_TEXTS)
            if blocking:
                await self._fail(MeetStatus.ERROR, f"cannot join {code}: {blocking}", "join-blocked")
                raise MeetJoinError(f"Meet will not let the bot in ({blocking}). Check MEET_JOIN_URL and the account.")
            if time.monotonic() >= prejoin_deadline:
                await self._fail(MeetStatus.ERROR, "pre-join screen missing: no join button", "join-prejoin")
                raise MeetJoinError(
                    f"The pre-join screen did not appear within {settings.prejoin_timeout_seconds:.0f}s. "
                    "A screenshot was saved."
                )
            await self._dismiss_popups()
            await asyncio.sleep(1.0)

        name_input = page.locator(NAME_INPUT).first
        if await name_input.count() > 0 and await name_input.is_visible():
            await name_input.fill(settings.display_name)
            logger.info("joining as guest %r", settings.display_name)
        await self._set_camera(False)
        await self._set_microphone(True)
        await self._select_audio_devices()

        label = (await join_button.get_attribute("aria-label")) or (await join_button.inner_text()).strip()
        clicked_at = time.monotonic()
        await join_button.click()
        asks = "ask" in label.lower()
        self._set_status(MeetStatus.LOBBY if asks else MeetStatus.JOINING, f"clicked {label!r}")
        print(f"[MEET] clicked {label!r}; " + ("waiting for the host to admit the bot..." if asks else "connecting..."), flush=True)

        deadline = time.monotonic() + settings.lobby_timeout_seconds
        while time.monotonic() < deadline:
            if await self._in_call():
                self.joined_at = time.monotonic()
                self._set_status(MeetStatus.IN_CALL, f"in call {code}")
                await self._settle(1.0)
                await self._dismiss_popups()
                await self._set_microphone(True)
                # Read the headcount now rather than waiting for the first
                # health tick: the delegate's very first turn can arrive before
                # that, and with the count still unknown it would be planned on
                # a guess about how many people are in the room.
                self.participant_count = await self.get_participant_count()
                heads = f", {self.participant_count} in the room" if self.participant_count else ""
                print(f"[MEET] in the call ({code}){heads}.", flush=True)
                return
            denied = await self._visible_text(DENIED_TEXTS)
            if denied:
                elapsed = time.monotonic() - clicked_at
                message = self._denied_message(denied, elapsed, guest=not settings.has_credentials)
                await self._fail(MeetStatus.ERROR, f"not admitted: {denied} [{elapsed:.1f}s after clicking]", "join-denied")
                raise MeetJoinError(message)
            if self.status is MeetStatus.JOINING and await self._visible_text(LOBBY_TEXTS):
                self._set_status(MeetStatus.LOBBY, "waiting in the lobby")
            await asyncio.sleep(1.0)
        await self._fail(MeetStatus.ERROR, "lobby timeout", "join-timeout")
        raise MeetJoinError(
            f"Nobody admitted the bot within {settings.lobby_timeout_seconds:.0f}s (BOT_LOBBY_TIMEOUT_SECONDS)."
        )

    async def leave(self) -> None:
        """Click "Leave call" if the bot is in one."""
        if self._page is None or self._page.is_closed():
            return
        try:
            leave = self._page.locator(LEAVE_BUTTON).first
            if await leave.count() > 0 and await leave.is_visible():
                await leave.click(timeout=5_000)
                await self._settle(1.0)
                logger.info("left the call")
        except PlaywrightError as exc:
            logger.warning("could not click Leave call: %s", exc)
        if self.status in (MeetStatus.IN_CALL, MeetStatus.LOBBY, MeetStatus.JOINING, MeetStatus.RECONNECTING):
            self._set_status(MeetStatus.LEFT, "left")

    # ------------------------------------------------------------------ #
    # Health
    # ------------------------------------------------------------------ #

    async def get_participant_count(self) -> int | None:
        """Best-effort headcount of everyone currently in the call, this bot included.

        Reads the People panel's numeric badge first and the call grid's tiles
        second. That order matters: Meet renders more than one element per
        person (a main tile plus a filmstrip thumbnail, a presentation surface),
        so *counting tiles* reports a one-on-one call as three or four people.
        The headcount feeds :func:`~src.brain.planner._meeting_mode_guidance`,
        where three or more turns the delegate from the room's default
        respondent into one voice among several - so an over-count does not
        just mis-report the call, it makes the delegate go quiet in exactly the
        one-on-one it should be most talkative in. Tiles are therefore
        de-duplicated by participant id rather than counted, and the badge,
        which Meet computes itself, is preferred.

        Meet's DOM changes without notice, like everything else this file
        scrapes, so any failure returns ``None`` rather than raising - callers
        already treat ``None`` as "unknown" and fall back to one-on-one
        behaviour instead of guessing a number.
        """
        try:
            badge = await self._people_badge_count()
            if badge is not None and badge > 0:
                return badge
            unique = await self._unique_participant_ids()
            if unique > 0:
                return unique
        except PlaywrightError as exc:
            logger.debug("participant count unavailable: %s", exc)
        return None

    async def _people_badge_count(self) -> int | None:
        """The headcount Meet itself shows on the People button, e.g. "People (2)"."""
        badges = self.page.locator(PEOPLE_COUNT_BADGE)
        try:
            total = await badges.count()
        except PlaywrightError:
            return None
        for position in range(min(total, 4)):
            badge = badges.nth(position)
            for source in (badge.get_attribute("aria-label"), badge.inner_text()):
                try:
                    label = await source
                except PlaywrightError:
                    continue
                match = _PEOPLE_COUNT_RE.search(label or "")
                if match:
                    return int(match.group(1) or match.group(2))
        return None

    async def _unique_participant_ids(self) -> int:
        """How many *distinct* participants the call grid is showing.

        One person can own several tiles at once, so the attribute values are
        collected into a set in the page rather than the elements being counted.
        """
        try:
            ids = await self.page.eval_on_selector_all(
                PARTICIPANT_TILE_SELECTOR,
                "nodes => new Set(nodes.map(n => n.getAttribute('data-participant-id'))"
                ".filter(Boolean)).size",
            )
        except PlaywrightError:
            return 0
        return int(ids) if isinstance(ids, (int, float)) else 0

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
        self._set_status(MeetStatus.ENDED, "leave button disappeared")
        return "the Leave call control disappeared"

    async def monitor(self) -> str:
        """Poll :meth:`check_health` every ``health_interval_seconds``; return the reason once the call is lost."""
        while True:
            await asyncio.sleep(self._settings.health_interval_seconds)
            reason = await self.check_health()
            if reason is not None:
                logger.warning("no longer in the call: %s", reason)
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
            print(f"[MEET] connection lost ({reason}); rejoin attempt {attempts}/{self._settings.rejoin_attempts}", flush=True)
            await asyncio.sleep(3.0)
            try:
                await self.join_meeting()
            except (MeetJoinError, MeetConfigError, PlaywrightError) as exc:
                return f"rejoin failed: {exc}"

    # ------------------------------------------------------------------ #
    # In-page helpers
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
        """The first of ``texts`` currently visible on the page, if any."""
        for text in texts:
            try:
                locator = self.page.get_by_text(text, exact=False).first
                if await locator.count() > 0 and await locator.is_visible():
                    return text
            except PlaywrightError:
                continue
        return None

    async def _settle(self, seconds: float) -> None:
        """Give Meet's client-side rendering a moment; ``networkidle`` never arrives on Meet."""
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except PlaywrightTimeoutError:
            pass
        await asyncio.sleep(seconds)

    async def _dismiss_popups(self) -> None:
        """Close "Got it" style overlays that would otherwise cover the controls."""
        for _ in range(3):
            try:
                button = self.page.locator(DISMISS_BUTTONS).first
                if await button.count() == 0 or not await button.is_visible():
                    return
                await button.click(timeout=3_000)
                await asyncio.sleep(0.5)
            except PlaywrightError:
                return

    async def _toggle(self, off_selector: str, on_selector: str, *, want_on: bool, what: str) -> None:
        """Bring a mic/camera toggle to the wanted state using its aria-label."""
        page = self.page
        try:
            currently_on = await self._visible(off_selector)      # "Turn off X" means X is on
            currently_off = await self._visible(on_selector)
            if want_on and currently_off:
                await page.locator(on_selector).first.click(timeout=5_000)
            elif not want_on and currently_on:
                await page.locator(off_selector).first.click(timeout=5_000)
            elif not currently_on and not currently_off:
                logger.debug("%s toggle not found; leaving Meet's default", what)
                return
            await asyncio.sleep(0.3)
            logger.info("%s %s", what, "on" if want_on else "off")
        except PlaywrightError as exc:
            logger.warning("could not set %s %s: %s", what, "on" if want_on else "off", exc)

    async def _set_microphone(self, on: bool) -> None:
        await self._toggle(MIC_OFF_BUTTON, MIC_ON_BUTTON, want_on=on, what="microphone")

    async def _set_camera(self, on: bool) -> None:
        await self._toggle(CAMERA_OFF_BUTTON, CAMERA_ON_BUTTON, want_on=on, what="camera")

    async def _select_audio_devices(self) -> None:
        """Pick the virtual cables in Meet's settings when names are configured. Best effort."""
        settings = self._settings
        if not settings.meet_mic_name and not settings.meet_speaker_name:
            return
        page = self.page
        try:
            more = page.locator(MORE_OPTIONS_BUTTON).first
            await more.click(timeout=5_000)
            await page.locator(SETTINGS_MENU_ITEM).first.click(timeout=5_000)
            await asyncio.sleep(1.0)
            if settings.meet_mic_name:
                await self._pick_option(page.locator(MIC_SELECT).first, settings.meet_mic_name, "microphone")
            if settings.meet_speaker_name:
                await self._pick_option(page.locator(SPEAKER_SELECT).first, settings.meet_speaker_name, "speakers")
            close = page.locator(CLOSE_DIALOG_BUTTON).first
            if await close.count() > 0:
                await close.click(timeout=3_000)
        except PlaywrightError as exc:
            logger.warning("could not select Meet audio devices (%s); Meet keeps the system defaults", exc)
            try:
                await page.keyboard.press("Escape")
            except PlaywrightError:
                pass

    async def _pick_option(self, combobox: Locator, device_name: str, what: str) -> None:
        await combobox.click(timeout=5_000)
        option = self.page.locator(f'[role="option"]:has-text("{device_name}")').first
        await option.click(timeout=5_000)
        logger.info("Meet %s set to %r", what, device_name)

    async def _screenshot(self, label: str) -> Path | None:
        """Save a PNG of the current page for diagnostics; returns its path."""
        if self._page is None or self._page.is_closed():
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self._settings.screenshot_dir / f"{stamp}-{label}.png"
        try:
            await self._page.screenshot(path=str(path), full_page=False)
            return path
        except PlaywrightError as exc:
            logger.debug("screenshot failed: %s", exc)
            return None

    async def _fail(self, status: MeetStatus, reason: str, label: str) -> None:
        shot = await self._screenshot(label)
        self._set_status(status, reason + (f"; screenshot {shot}" if shot else ""))
