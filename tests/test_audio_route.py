"""
The delegate's hearing: device resolution, cable verification, silence detection.

This covers the failure that produced a whole live meeting of "Nothing heard
yet" while every component reported itself healthy - Chromium in the call,
Deepgram connected, the brain idle because no utterance ever arrived. Two
separate silent faults were behind it, and both are asserted on here:

1. **A cable that enumerates but carries no audio.** BlackHole 16ch was
   present, opened without error, accepted everything written to it, and
   returned silence from its input side forever. Nothing checked, so the bot
   listened to a dead wire for the length of the call.
2. **Device indexes that drift.** PortAudio numbers devices by enumeration
   order, so an iPhone's Continuity microphone appearing or disappearing
   renumbers everything after it. ``DELEG8_INPUT_DEVICE_INDEX=1`` meant
   BlackHole 16ch when it was written and BlackHole 2ch afterwards - and
   ``=2`` came to mean a microphone, which cannot be an output at all.

Nothing here touches real hardware: the device layouts and the probe results
are supplied directly, which is the only way to reproduce a cable that is
present and deaf.

Usage::

    python tests/test_audio_route.py

Exit codes: 0 = every check passed, 1 = at least one failed.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.audio.cable_check import (  # noqa: E402
    Cable,
    CableProbe,
    find_by_index,
    find_by_name,
    resolve_route,
)
from src.audio.config import VoiceSettings  # noqa: E402
from src.gateway.meet_bot import _PEOPLE_COUNT_RE  # noqa: E402

GREEN, RED, BOLD, RESET = "\033[32m", "\033[31m", "\033[1m", "\033[0m"

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one assertion."""
    if condition:
        print(f"  {GREEN}PASS{RESET} {name}")
    else:
        print(f"  {RED}FAIL{RESET} {name}{(' - ' + detail) if detail else ''}")
        _failures.append(name)


# The exact device layout of the machine the bug was found on, before and after
# the iPhone microphone disappeared and renumbered everything.
WITH_IPHONE: list[Cable] = [
    Cable(0, "Durai iphone  Microphone", 1, 0),
    Cable(1, "BlackHole 16ch", 16, 16),
    Cable(2, "BlackHole 2ch", 2, 2),
    Cable(3, "MacBook Pro Microphone", 1, 0),
    Cable(4, "MacBook Pro Speakers", 0, 2),
]
WITHOUT_IPHONE: list[Cable] = [
    Cable(0, "BlackHole 16ch", 16, 16),
    Cable(1, "BlackHole 2ch", 2, 2),
    Cable(2, "MacBook Pro Microphone", 1, 0),
    Cable(3, "MacBook Pro Speakers", 0, 2),
    Cable(4, "Microsoft Teams Audio", 1, 1),
]


def probes_for(cables: list[Cable], *, dead: set[str] = frozenset()) -> list[CableProbe]:
    """Probe results for every loopback device, with ``dead`` ones silent."""
    return [
        CableProbe(c, peak=30 if c.name in dead else 11000)
        for c in cables
        if c.is_loopback
    ]


def settings(**kwargs: object) -> VoiceSettings:
    """Voice settings with only the routing fields that matter here."""
    return VoiceSettings(deepgram_api_key="k", cartesia_api_key="k", **kwargs)  # type: ignore[arg-type]


def test_name_resolution() -> None:
    print(f"\n{BOLD}1. devices resolve by name, which does not drift{RESET}")
    check("an exact name is found", find_by_name(WITH_IPHONE, "BlackHole 2ch") == WITH_IPHONE[2])
    check("a substring matches too", find_by_name(WITH_IPHONE, "16ch") == WITH_IPHONE[1])
    check("an absent name is None, not a wrong guess", find_by_name(WITH_IPHONE, "VB-Cable") is None)
    check("an empty name resolves to nothing", find_by_name(WITH_IPHONE, "  ") is None)

    found = find_by_name(WITHOUT_IPHONE, "BlackHole 2ch")
    check(
        "the same name survives renumbering (index 2 -> 1)",
        found is not None and found.name == "BlackHole 2ch" and found.index == 1,
        f"got {found}",
    )


def test_index_drift() -> None:
    print(f"\n{BOLD}2. an index that has drifted is rejected, not used{RESET}")
    # Index 2 was BlackHole 2ch; after the renumbering it is a microphone, which
    # has no output channels and so cannot be the delegate's mouth.
    check(
        "index 2 as an OUTPUT is refused once it points at a microphone",
        find_by_index(WITHOUT_IPHONE, 2, need_input=False, need_output=True) is None,
    )
    check(
        "index 2 as an INPUT is still fine (a microphone can record)",
        find_by_index(WITHOUT_IPHONE, 2, need_input=True, need_output=False) == WITHOUT_IPHONE[2],
    )
    check("an index nobody has is None", find_by_index(WITHOUT_IPHONE, 99, need_input=True, need_output=False) is None)

    route = resolve_route(
        settings(input_device_index=1, output_device_index=2),
        cables=WITHOUT_IPHONE,
        probes=probes_for(WITHOUT_IPHONE),
        defaults=(1, 0),
    )
    check(
        "resolving the drifted config says so in the notes",
        any("drift" in note for note in route.notes),
        f"notes: {route.notes}",
    )
    check(
        "and it does not open the microphone as an output",
        route.mouth is not None and route.mouth.output_channels > 0,
        f"mouth: {route.mouth}",
    )


def test_dead_cable_is_replaced() -> None:
    print(f"\n{BOLD}3. a cable that carries no audio is detected and replaced{RESET}")
    dead = {"BlackHole 16ch"}
    route = resolve_route(
        settings(input_device_name="BlackHole 16ch", output_device_name="BlackHole 2ch"),
        cables=WITHOUT_IPHONE,
        probes=probes_for(WITHOUT_IPHONE, dead=dead),
        defaults=(1, 0),
    )
    check(
        "the delegate is not left listening to the dead cable",
        route.ears is not None and route.ears.name != "BlackHole 16ch",
        f"ears: {route.ears}",
    )
    check("the swap is recorded", route.repaired)
    check("the route is usable afterwards", route.usable, f"notes: {route.notes}")
    check(
        "the dead device is named, with the fix",
        any("carry no audio" in note and "coreaudiod" in note for note in route.notes),
        f"notes: {route.notes}",
    )
    check(
        "the two ends are different devices",
        route.ears is not None and route.mouth is not None and route.ears.index != route.mouth.index,
    )


def test_deaf_route_is_reported() -> None:
    print(f"\n{BOLD}4. with every cable dead, the delegate is reported deaf{RESET}")
    cables = [Cable(0, "BlackHole 16ch", 16, 16), Cable(1, "MacBook Pro Microphone", 1, 0)]
    route = resolve_route(
        settings(input_device_name="BlackHole 16ch", output_device_name="BlackHole 16ch"),
        cables=cables,
        probes=probes_for(cables, dead={"BlackHole 16ch"}),
        defaults=(1, 0),
    )
    check("deaf is true, so the console can say so", route.deaf)
    check("the route is not called usable", not route.usable)
    check(
        "installing a cable is suggested",
        any("no working loopback device" in note for note in route.notes),
        f"notes: {route.notes}",
    )


def test_one_device_is_never_both_ends() -> None:
    print(f"\n{BOLD}5. one device is never used as both ends{RESET}")
    # PortAudio cannot open a single Core Audio device as a separate input and
    # output stream: the output succeeds, the input reports AUHAL error -50 and
    # then delivers zero frames for the whole call. Sharing is not a degraded
    # mode to fall back to, it is total deafness.
    cables = [Cable(0, "BlackHole 2ch", 2, 2), Cable(1, "MacBook Pro Speakers", 0, 2)]
    route = resolve_route(
        settings(input_device_name="BlackHole 2ch", output_device_name="BlackHole 2ch"),
        cables=cables,
        probes=probes_for(cables),
        defaults=(0, 0),
    )
    check("the ears keep the only working cable", route.ears is not None and route.ears.name == "BlackHole 2ch")
    check("the mouth is not the same device", route.mouth is None or route.mouth.index != route.ears.index)
    check("the route is not called usable", not route.usable)
    check(
        "a second cable is asked for, with the reason",
        any("second virtual cable" in note and "deaf" in note for note in route.notes),
        f"notes: {route.notes}",
    )

    # With a second loopback present, the mouth moves there instead.
    with_second = [*cables, Cable(2, "Microsoft Teams Audio", 1, 1)]
    route = resolve_route(
        settings(input_device_name="BlackHole 2ch", output_device_name="BlackHole 2ch"),
        cables=with_second,
        probes=probes_for(with_second),
        defaults=(0, 0),
    )
    check(
        "the mouth moves to the second cable",
        route.mouth is not None and route.mouth.name == "Microsoft Teams Audio",
        f"mouth: {route.mouth}",
    )
    check("and the route becomes usable", route.usable, f"notes: {route.notes}")


def test_probing_can_be_skipped() -> None:
    print(f"\n{BOLD}6. probing can be turned off without breaking resolution{RESET}")
    route = resolve_route(
        settings(input_device_name="BlackHole 2ch", output_device_name="Microsoft Teams Audio"),
        probe=False,
        cables=WITHOUT_IPHONE,
        defaults=(1, 0),
    )
    check("the configured cables are honoured", route.ears is not None and route.ears.name == "BlackHole 2ch")
    check("nothing was measured", not route.probed)
    check("no cable is called deaf on no evidence", not route.deaf)
    check("the route is taken at face value", route.usable)


def test_input_level_detects_silence() -> None:
    print(f"\n{BOLD}7. a silent input is distinguishable from a quiet room{RESET}")
    from src.audio.audio_io import SILENCE_PEAK, AudioIO

    audio = AudioIO.__new__(AudioIO)  # no PortAudio: only the measurement path is under test
    audio._muted = False
    audio.frames_captured = 0
    audio._peak_since_read = 0
    audio._last_sound_at = None
    audio._listening_since = None

    quiet = (10).to_bytes(2, "little", signed=True) * 160
    for _ in range(50):
        audio._measure(quiet)
    peak, since = audio.read_input_level()
    check("noise-floor frames do not count as sound", not audio.heard_anything)
    check(f"the peak is reported anyway ({peak})", peak < SILENCE_PEAK)
    check("there is no 'last heard' time at all", since is None)

    speech = (6000).to_bytes(2, "little", signed=True) * 160
    audio._measure(speech)
    peak, since = audio.read_input_level()
    check("real speech registers", audio.heard_anything and peak >= SILENCE_PEAK, f"peak {peak}")
    check("and the level resets between reads", audio.read_input_level()[0] == 0)

    audio._muted = True
    audio._peak_since_read = 0
    audio._measure(speech)
    check("a muted input measures nothing (the delegate's own voice)", audio.read_input_level()[0] == 0)


def test_headcount_patterns() -> None:
    print(f"\n{BOLD}8. the headcount is read, not guessed from tile count{RESET}")
    for label, expected in (
        ("People (2)", 2),
        ("Show everyone (5)", 5),
        ("3 people", 3),
        ("2 participants", 2),
    ):
        match = _PEOPLE_COUNT_RE.search(label)
        got = int(match.group(1) or match.group(2)) if match else None
        check(f"{label!r} -> {expected}", got == expected, f"got {got}")
    check("a label with no number yields nothing", _PEOPLE_COUNT_RE.search("People") is None)


def main() -> int:
    """Run every check."""
    print(f"{BOLD}DELEG8: can the delegate actually hear the meeting?{RESET}")
    test_name_resolution()
    test_index_drift()
    test_dead_cable_is_replaced()
    test_deaf_route_is_reported()
    test_one_device_is_never_both_ends()
    test_probing_can_be_skipped()
    test_input_level_detects_silence()
    test_headcount_patterns()
    print()
    if _failures:
        print(f"{RED}{len(_failures)} check(s) failed:{RESET} " + ", ".join(_failures))
        return 1
    print(f"{GREEN}{BOLD}every check passed{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
