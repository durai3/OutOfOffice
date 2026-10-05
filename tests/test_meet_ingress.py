"""
Standalone verification for Sprint 5: headless Google Meet ingress.

Two modes:

    python tests/test_meet_ingress.py                 # dry run (default)
    python tests/test_meet_ingress.py --join          # really join, sit in the call, leave

Dry run, no credentials needed:
    1. Launches Playwright's Chromium with the media flags (--use-fake-ui-for-media-stream,
       --disable-blink-features=AutomationControlled, audio unmuted) and a persistent profile.
    2. Opens the Meet landing page and checks the automation is not flagged
       (navigator.webdriver must be false).
    3. Opens a deliberately invalid meeting link and checks the bot reports it as
       MeetJoinError instead of hanging.
    4. If MEET_JOIN_URL (or --url) is set: signs in when BOT_GOOGLE_EMAIL is set,
       opens the real pre-join screen, reports the join button, name field and
       mic/camera toggles it found, and saves a screenshot. Nothing is clicked.

Join mode (needs MEET_JOIN_URL; credentials optional):
    Signs in, joins (waits in the lobby until admitted or BOT_LOBBY_TIMEOUT_SECONDS),
    runs the health loop for --stay seconds, then leaves. Audio is NOT started here;
    use ``python -m src.main join`` for the full delegate.

Chromium runs with a window unless --headless is passed (headless is fine for
the dry run; it has no audio devices, so never use it for a real call).

Exit codes: 0 = all checks passed, 1 = a check failed, 2 = configuration problem.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from src.gateway.meet_bot import (  # noqa: E402
    MEET_LANDING_URL,
    MEET_SETUP_INSTRUCTIONS,
    GoogleMeetBot,
    MeetConfigError,
    MeetJoinError,
    MeetLoginError,
    MeetSettings,
    MeetStatus,
)

INVALID_URL: str = "https://meet.google.com/zzz-zzzz-zzz"


class Checker:
    """Collects PASS/FAIL checks and prints them as they happen."""

    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        """Record one check."""
        self.results.append((name, ok, detail))
        print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
        return ok

    @property
    def failures(self) -> int:
        """Number of failed checks."""
        return sum(1 for _, ok, _ in self.results if not ok)


def step(number: int, title: str) -> None:
    """Print a numbered step banner."""
    print(f"\n[{number}] {title}")


def load_settings(args: argparse.Namespace) -> MeetSettings | None:
    """Settings from the environment with command-line overrides; None on a config problem."""
    try:
        settings = MeetSettings.from_env(require_url=args.join)
    except MeetConfigError as exc:
        print(f"\nConfiguration problem: {exc}")
        return None
    overrides: dict[str, object] = {}
    if args.url:
        overrides["join_url"] = args.url
    if args.headless:
        overrides["headless"] = True
    if args.lobby_timeout is not None:
        overrides["lobby_timeout_seconds"] = args.lobby_timeout
    return dataclasses.replace(settings, **overrides) if overrides else settings


async def dry_run(settings: MeetSettings) -> int:
    """Launch, probe the landing page, probe an invalid link, inspect the real pre-join screen."""
    checks = Checker()
    print("DELEG8 Sprint 5 verification: Google Meet ingress (dry run)")
    bot = GoogleMeetBot(settings)

    step(1, f"Launching Chromium (headless={settings.headless}, profile {settings.profile_dir})")
    try:
        await bot.start()
    except MeetConfigError as exc:
        print(f"\n{exc}")
        return 2
    try:
        checks.check("browser started", bot.status is MeetStatus.IDLE, bot.status.value)

        step(2, f"Opening {MEET_LANDING_URL}")
        signed_in = await bot.is_signed_in()
        page = bot.page
        webdriver = await page.evaluate("() => navigator.webdriver")
        title = await page.title()
        print(f"    title: {title!r}; signed in: {signed_in}; navigator.webdriver: {webdriver}")
        on_meet = "meet.google.com" in page.url or "workspace.google.com/products/meet" in page.url
        checks.check("landing page loaded (Meet app, or the Workspace page shown to signed-out visitors)", on_meet, page.url)
        checks.check("automation flag hidden (navigator.webdriver is false)", webdriver in (False, None), repr(webdriver))

        step(3, f"Opening an invalid link {INVALID_URL} (must fail fast, not hang)")
        started = time.monotonic()
        try:
            await bot.join_meeting(INVALID_URL)
            checks.check("invalid link rejected", False, "join_meeting returned without error")
        except MeetJoinError as exc:
            elapsed = time.monotonic() - started
            checks.check("invalid link rejected with MeetJoinError", True, f"{exc} [{elapsed:.1f}s]")
            checks.check("rejection was quick (fast-fail on Meet's error text)", elapsed < 20.0, f"{elapsed:.1f}s")
            checks.check("screenshot saved for diagnostics",
                         bot.last_reason is not None and "screenshot" in bot.last_reason, bot.last_reason or "")

        if settings.join_url:
            step(4, f"Inspecting the real pre-join screen {settings.join_url}")
            if settings.has_credentials:
                try:
                    signed_in = await bot.login()
                    checks.check("signed in as the bot account", signed_in, settings.email)
                except MeetLoginError as exc:
                    checks.check("signed in as the bot account", False, str(exc))
            report = await bot.inspect_prejoin(settings.join_url)
            print(f"    join button   : {report.join_button_label!r}")
            print(f"    name input    : {report.name_input_present}")
            print(f"    mic toggle    : {report.mic_toggle_present}")
            print(f"    camera toggle : {report.camera_toggle_present}")
            print(f"    blocking text : {report.blocking_text!r}")
            print(f"    screenshot    : {report.screenshot}")
            for note in report.notes:
                print(f"    note          : {note}")
            checks.check("pre-join screen shows Join now / Ask to join", report.join_button_label is not None,
                         report.blocking_text or "no join button")
            checks.check("mic and camera toggles found", report.mic_toggle_present and report.camera_toggle_present)
        else:
            step(4, "No MEET_JOIN_URL set; skipping the real pre-join inspection (set it or pass --url)")
    finally:
        await bot.stop()

    total = len(checks.results)
    print(f"\n{total - checks.failures}/{total} checks passed.")
    return 1 if checks.failures else 0


async def join_run(settings: MeetSettings, stay_seconds: float) -> int:
    """Sign in, join, hold the call for a while under the health loop, leave."""
    checks = Checker()
    print("DELEG8 Sprint 5 verification: Google Meet ingress (join)")
    bot = GoogleMeetBot(settings)
    step(1, f"Launching Chromium (headless={settings.headless})")
    try:
        await bot.start()
    except MeetConfigError as exc:
        print(f"\n{exc}")
        return 2
    try:
        if settings.has_credentials:
            step(2, f"Signing in as {settings.email}")
            try:
                checks.check("signed in", await bot.login())
            except MeetLoginError as exc:
                print(f"\n{exc}")
                return 2
        else:
            step(2, f"No credentials; joining as guest {settings.display_name!r}")

        step(3, f"Joining {settings.join_url} (lobby timeout {settings.lobby_timeout_seconds:.0f}s)")
        try:
            await bot.join_meeting()
        except MeetJoinError as exc:
            checks.check("joined the call", False, str(exc))
            return 1
        checks.check("joined the call", bot.status is MeetStatus.IN_CALL, bot.status.value)

        step(4, f"Holding the call for {stay_seconds:.0f}s under the health loop (remove the bot to test detection)")
        watcher = asyncio.create_task(bot.stay_connected())
        done, _ = await asyncio.wait({watcher}, timeout=stay_seconds)
        if watcher in done:
            print(f"    left early: {watcher.result()} (status {bot.status.value})")
            checks.check("health loop detected leaving the call", bot.status in (MeetStatus.REMOVED, MeetStatus.ENDED, MeetStatus.ERROR),
                         bot.status.value)
        else:
            watcher.cancel()
            checks.check("still in the call after the hold", bot.status is MeetStatus.IN_CALL, bot.status.value)

        step(5, "Leaving")
        await bot.leave()
        checks.check("left the call", bot.status in (MeetStatus.LEFT, MeetStatus.REMOVED, MeetStatus.ENDED), bot.status.value)
    finally:
        await bot.stop()
    total = len(checks.results)
    print(f"\n{total - checks.failures}/{total} checks passed.")
    return 1 if checks.failures else 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--join", action="store_true", help="really join the meeting instead of a dry run")
    parser.add_argument("--url", default=None, help="Meet link (default: MEET_JOIN_URL)")
    parser.add_argument("--headless", action="store_true", help="run Chromium without a window (dry runs only)")
    parser.add_argument("--stay", type=float, default=60.0, help="seconds to stay in the call in --join mode")
    parser.add_argument("--lobby-timeout", type=float, default=None, help="override BOT_LOBBY_TIMEOUT_SECONDS")
    parser.add_argument("--setup-help", action="store_true", help="print the setup instructions and exit")
    parser.add_argument("--log-level", default="WARNING")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """Entry point."""
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.setup_help:
        print(MEET_SETUP_INSTRUCTIONS)
        return 0
    settings = load_settings(args)
    if settings is None:
        return 2
    if args.join:
        return asyncio.run(join_run(settings, args.stay))
    return asyncio.run(dry_run(settings))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
