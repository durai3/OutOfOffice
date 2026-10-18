"""
Virtual-cable verification: does the bot's microphone actually carry the meeting?

The delegate hears a call through a loopback device (BlackHole, VB-Cable, a
PulseAudio null sink): Chromium plays the meeting into that device's *output*
side and :class:`~src.audio.audio_io.AudioIO` records it from the same device's
*input* side. :mod:`src.audio.device_setup` wires that up, but it only ever
checked that the devices *exist*. Two failures slip through a name check, and
both are silent - Deepgram stays connected, the console prints ``[READY]``, and
not one utterance is ever transcribed:

1. **A cable that does not loop back.** A HAL plug-in can be installed and
   enumerated while its driver was never loaded (a ``brew install --cask`` of
   BlackHole drops the files, but Core Audio only scans for new plug-ins when
   it starts). The device appears, opens, accepts audio - and its input side
   returns silence forever.
2. **Indexes that move.** PortAudio numbers devices by enumeration order, so a
   Continuity-Camera iPhone mic appearing or disappearing renumbers everything
   after it. ``DELEG8_INPUT_DEVICE_INDEX=1`` meant BlackHole 16ch when it was
   written to ``.env`` and means BlackHole 2ch - or a device with no output
   channels at all - on the next run.

So this module *measures* the route instead of trusting it. :func:`probe_cable`
plays a short tone into a loopback device's output while recording its input;
only a device that hears itself back can carry a meeting. :func:`resolve_route`
resolves each direction by name first (stable) and index second (not), probes
what it picked, and substitutes a cable that passes when the configured one is
dead. :func:`apply_system_defaults` then points the system defaults - which is
what Chromium follows - at the two ends of the chosen cables.

The probe only ever touches devices with both input and output channels, so a
real microphone or a pair of speakers is never played into: the tone is audible
to nobody because a loopback device goes nowhere near a transducer.

Usage::

    python -m src.audio.cable_check             # probe every loopback device
    python -m src.audio.cable_check --apply     # also set the system defaults
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from array import array
from dataclasses import dataclass, field

import pyaudio

from src.audio.config import VoiceSettings

logger = logging.getLogger("deleg8.audio.cable")

CARRIES_AUDIO_PEAK: int = 1000
"""Captured 16-bit peak above which a loopback device is considered working.

The probe plays at roughly half scale (~16000), so a live cable comes back in
the thousands; a dead one sits at the noise floor, which measures in the tens.
Anything between is treated as dead - a cable that attenuates a tone by 30 dB
would not carry speech either.
"""

PROBE_SECONDS: float = 0.4
PROBE_RATE: int = 48000
"""Every virtual cable in practice runs at 48 kHz, and probing at the device's
own rate keeps the measurement away from any resampling question."""
PROBE_AMPLITUDE: int = 16000
PROBE_FREQUENCY: float = 440.0

VIRTUAL_CABLE_HINTS: tuple[str, ...] = (
    "blackhole", "vb-cable", "cable input", "cable output", "vb-audio", "voicemeeter",
    "virtual", "loopback", "soundflower", "null sink", "monitor of", "teams audio",
)


@dataclass(frozen=True)
class Cable:
    """One PortAudio device, as a candidate end of an audio route."""

    index: int
    name: str
    input_channels: int
    output_channels: int

    @property
    def is_loopback(self) -> bool:
        """True when the device can both play and record, so it can be probed.

        This is the hard requirement for either end of the route: Chromium has
        to play into the device the bot records from, and record from the device
        the bot plays into. Real hardware is one-directional and can never do it.
        """
        return self.input_channels > 0 and self.output_channels > 0

    @property
    def looks_virtual(self) -> bool:
        """True when the name matches a known virtual-cable product."""
        lowered = self.name.lower()
        return any(hint in lowered for hint in VIRTUAL_CABLE_HINTS)

    def label(self) -> str:
        """``[index] name`` for logs and console output."""
        return f"[{self.index}] {self.name}"


@dataclass(frozen=True)
class CableProbe:
    """What :func:`probe_cable` measured on one device."""

    cable: Cable
    peak: int
    error: str | None = None

    @property
    def carries_audio(self) -> bool:
        """True when the tone came back loudly enough to call the cable live."""
        return self.error is None and self.peak >= CARRIES_AUDIO_PEAK

    def describe(self) -> str:
        """One-line verdict."""
        if self.error is not None:
            return f"{self.cable.label()}: could not probe ({self.error})"
        verdict = "carries audio" if self.carries_audio else "SILENT - audio played into it never comes back"
        return f"{self.cable.label()}: peak {self.peak} -> {verdict}"


@dataclass
class AudioRoute:
    """The two cables the session will use, and what probing them showed.

    ``ears`` is cable A: Chromium's speaker, which the delegate records from.
    ``mouth`` is cable B: the delegate's speaker, which Chromium records from
    as its microphone. They must be different devices or the delegate hears
    its own voice as if the room had said it.
    """

    ears: Cable | None = None
    mouth: Cable | None = None
    ears_probe: CableProbe | None = None
    mouth_probe: CableProbe | None = None
    repaired: bool = False
    """True when a configured device was dead or missing and was substituted."""
    probed: bool = False
    """True when the cables were actually measured (false when probing was skipped)."""
    in_app_mic: str | None = None
    """Device the meeting app must select as its own microphone, because the OS
    refused to make it the default input. Some virtual devices accept the
    assignment, report success and are silently reverted by Core Audio, so the
    only way to get the delegate's voice into the call is the meeting's own
    audio settings (``BOT_MEET_MIC_NAME``)."""
    alternatives: list[Cable] = field(default_factory=list)
    """Other cables that were measured and carry audio, best first - what
    :func:`apply_system_defaults` falls back to when the OS refuses a device."""
    probes: list[CableProbe] = field(default_factory=list)
    """Every measurement taken, so a cable swapped in later keeps its reading."""
    notes: list[str] = field(default_factory=list)

    def probe_for(self, cable: Cable | None) -> CableProbe | None:
        """The measurement taken for ``cable``, if it was probed."""
        if cable is None:
            return None
        return next((p for p in self.probes if p.cable.index == cable.index), None)

    @property
    def usable(self) -> bool:
        """True when both ends exist, differ, and (if probed) carry audio."""
        if self.ears is None or self.mouth is None:
            return False
        # Sharing one device is not a degraded mode, it is a broken one:
        # PortAudio cannot open the same Core Audio device as two streams and
        # the input silently delivers nothing at all (AUHAL error -50).
        if self.ears.index == self.mouth.index:
            return False
        if not self.probed:
            return True
        return bool(
            self.ears_probe and self.ears_probe.carries_audio
            and self.mouth_probe and self.mouth_probe.carries_audio
        )

    @property
    def deaf(self) -> bool:
        """True when the delegate's ear cable was measured and carries nothing.

        The single most consequential failure in the whole pipeline: the call
        runs, the brain runs, and the delegate simply never hears a word.
        """
        return self.probed and self.ears_probe is not None and not self.ears_probe.carries_audio

    def describe(self) -> str:
        """Multi-line human summary for the console."""
        lines = [
            f"  ears  (Chromium speaker -> DELEG8 input):  {self.ears.label() if self.ears else 'NONE'}",
            f"  mouth (DELEG8 output -> Chromium mic):     {self.mouth.label() if self.mouth else 'NONE'}",
        ]
        for probe in (self.ears_probe, self.mouth_probe):
            if probe is not None:
                lines.append(f"  probe {probe.describe()}")
        if self.in_app_mic:
            lines.append(f"  the meeting app must select {self.in_app_mic!r} as its microphone itself")
        lines += [f"  note: {note}" for note in self.notes]
        return "\n".join(lines)

    def as_dict(self) -> dict[str, object]:
        """Serialisable form, for the live-feed event the web console reads."""
        return {
            "input_index": self.ears.index if self.ears else None,
            "input_name": self.ears.name if self.ears else None,
            "output_index": self.mouth.index if self.mouth else None,
            "output_name": self.mouth.name if self.mouth else None,
            "input_peak": self.ears_probe.peak if self.ears_probe else None,
            "output_peak": self.mouth_probe.peak if self.mouth_probe else None,
            "probed": self.probed,
            "repaired": self.repaired,
            "usable": self.usable,
            "deaf": self.deaf,
            "in_app_mic": self.in_app_mic,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# Enumeration
# --------------------------------------------------------------------------- #


def list_cables() -> list[Cable]:
    """Every PortAudio device, in the indexing the voice loop will see.

    Blocking (PortAudio initialisation); call before the event loop needs to be
    responsive, which for a session means before the bot opens a browser.
    """
    pa = pyaudio.PyAudio()
    try:
        cables: list[Cable] = []
        for index in range(pa.get_device_count()):
            try:
                info = pa.get_device_info_by_index(index)
            except OSError:  # a device that vanished mid-enumeration
                continue
            cables.append(
                Cable(
                    index=index,
                    name=str(info["name"]).strip(),
                    input_channels=int(info["maxInputChannels"]),
                    output_channels=int(info["maxOutputChannels"]),
                )
            )
        return cables
    finally:
        pa.terminate()


def find_by_name(cables: list[Cable], wanted: str) -> Cable | None:
    """Resolve a device name: exact match first, then a case-insensitive substring."""
    wanted = wanted.strip()
    if not wanted:
        return None
    for cable in cables:
        if cable.name == wanted:
            return cable
    lowered = wanted.lower()
    for cable in cables:
        if lowered in cable.name.lower():
            return cable
    return None


def find_by_index(cables: list[Cable], index: int, *, need_input: bool, need_output: bool) -> Cable | None:
    """The device at ``index``, but only if it still has the channels the role needs.

    An index that has drifted onto a device of the wrong shape (a microphone
    where an output was expected) is treated as missing rather than used, which
    is what turns a renumbered device list into a clear diagnostic instead of a
    stream that opens on the wrong hardware.
    """
    for cable in cables:
        if cable.index != index:
            continue
        if need_input and cable.input_channels == 0:
            return None
        if need_output and cable.output_channels == 0:
            return None
        return cable
    return None


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #


def _tone(channels: int, rate: int, seconds: float) -> bytes:
    """A mono/interleaved sine burst as 16-bit PCM."""
    samples = array("h")
    for n in range(int(rate * seconds)):
        value = int(PROBE_AMPLITUDE * math.sin(2 * math.pi * PROBE_FREQUENCY * n / rate))
        for _ in range(channels):
            samples.append(value)
    return samples.tobytes()


def probe_cable(cable: Cable, *, seconds: float = PROBE_SECONDS) -> CableProbe:
    """Play a tone into ``cable``'s output and measure what its input hears back.

    Blocking, roughly ``seconds`` long. Only meaningful for a loopback device;
    a non-loopback device is reported as an error rather than probed, so this
    never plays a tone at a speaker or listens to a room microphone.
    """
    if not cable.is_loopback:
        return CableProbe(cable, peak=0, error="not a loopback device (needs both input and output channels)")

    pa = pyaudio.PyAudio()
    out_channels = 2 if cable.output_channels >= 2 else 1
    payload = _tone(out_channels, PROBE_RATE, seconds + 0.3)
    failure: list[str] = []

    def play() -> None:
        try:
            stream = pa.open(
                format=pyaudio.paInt16, channels=out_channels, rate=PROBE_RATE, output=True,
                output_device_index=cable.index, frames_per_buffer=int(PROBE_RATE * 0.02),
            )
        except OSError as exc:
            failure.append(f"output would not open: {exc}")
            return
        try:
            stream.write(payload)
        except OSError as exc:
            failure.append(f"output write failed: {exc}")
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except OSError:
                pass

    player = threading.Thread(target=play, name="deleg8-cable-probe", daemon=True)
    try:
        player.start()
        time.sleep(0.25)  # let the tone reach the device before the capture opens
        frames = int(PROBE_RATE * 0.02)
        try:
            capture = pa.open(
                format=pyaudio.paInt16, channels=1, rate=PROBE_RATE, input=True,
                input_device_index=cable.index, frames_per_buffer=frames,
            )
        except OSError as exc:
            player.join(timeout=seconds + 1.0)
            return CableProbe(cable, peak=0, error=f"input would not open: {exc}")
        peak = 0
        try:
            for _ in range(max(1, int(seconds / 0.02))):
                block = array("h")
                block.frombytes(capture.read(frames, exception_on_overflow=False))
                if block:
                    peak = max(peak, abs(max(block)), abs(min(block)))
        except OSError as exc:
            failure.append(f"input read failed: {exc}")
        finally:
            try:
                capture.stop_stream()
                capture.close()
            except OSError:
                pass
        player.join(timeout=seconds + 1.0)
        return CableProbe(cable, peak=peak, error=failure[0] if failure else None)
    finally:
        pa.terminate()


def probe_all(cables: list[Cable] | None = None) -> list[CableProbe]:
    """Probe every loopback device, so a dead cable can be told from a live one."""
    candidates = [c for c in (cables if cables is not None else list_cables()) if c.is_loopback]
    return [probe_cable(cable) for cable in candidates]


# --------------------------------------------------------------------------- #
# Route resolution
# --------------------------------------------------------------------------- #


def _default_indexes() -> tuple[int | None, int | None]:
    """The system default input and output indexes, or ``None`` when unavailable."""
    pa = pyaudio.PyAudio()
    try:
        try:
            default_in: int | None = int(pa.get_default_input_device_info()["index"])
        except (OSError, KeyError):
            default_in = None
        try:
            default_out: int | None = int(pa.get_default_output_device_info()["index"])
        except (OSError, KeyError):
            default_out = None
        return default_in, default_out
    finally:
        pa.terminate()


def _pick_configured(
    cables: list[Cable], name: str | None, index: int | None, *, need_input: bool, need_output: bool, role: str,
    notes: list[str],
) -> Cable | None:
    """Resolve one end of the route from its configured name, then its index.

    Name wins over index because only the name survives PortAudio renumbering;
    when a name is set and no longer matches anything, that is said out loud
    rather than quietly falling through to a number that now points elsewhere.
    """
    if name:
        found = find_by_name(cables, name)
        if found is not None:
            return found
        notes.append(f"{role}: no device named {name!r} is present")
    if index is not None:
        found = find_by_index(cables, index, need_input=need_input, need_output=need_output)
        if found is not None:
            return found
        notes.append(
            f"{role}: index {index} no longer points at a usable device "
            "(PortAudio renumbers when a device appears or disappears, so indexes drift)"
        )
    return None


def _best_alternative(probes: list[CableProbe], exclude: Cable | None) -> Cable | None:
    """The loudest working loopback cable that is not ``exclude``."""
    working = [
        p for p in probes
        if p.carries_audio and (exclude is None or p.cable.index != exclude.index)
    ]
    if not working:
        return None
    # Prefer a recognised virtual cable over an incidental loopback device
    # (a conferencing app's own virtual device works, but is not ours to rely on).
    working.sort(key=lambda p: (p.cable.looks_virtual, p.peak), reverse=True)
    return working[0].cable


def resolve_route(
    settings: VoiceSettings,
    *,
    probe: bool = True,
    cables: list[Cable] | None = None,
    probes: list[CableProbe] | None = None,
    defaults: tuple[int | None, int | None] | None = None,
) -> AudioRoute:
    """Choose, measure and if necessary repair the two cables the delegate uses.

    Name-based configuration is honoured first, then indexes, then the system
    defaults. When ``probe`` is true each chosen cable is measured, and a cable
    that does not carry audio is replaced by one that does - the delegate ends
    up listening to a working cable instead of a device that is present but
    deaf. Never raises: a route it cannot work out is returned with
    :attr:`AudioRoute.usable` false and notes explaining why, and the caller
    decides whether to go ahead anyway.

    ``cables``, ``probes`` and ``defaults`` override what would be read from
    PortAudio, so the resolution rules can be tested against device layouts
    that are awkward to reproduce on real hardware - a cable that enumerates
    but carries nothing, or an index that has drifted onto a microphone.
    """
    route = AudioRoute()
    try:
        cables = list_cables() if cables is None else cables
    except Exception as exc:  # noqa: BLE001 - PortAudio unavailable (headless CI, no audio stack)
        route.notes.append(f"no audio devices could be enumerated: {exc}")
        return route
    if not cables:
        route.notes.append("PortAudio reports no audio devices at all")
        return route

    ears = _pick_configured(
        cables, settings.input_device_name, settings.input_device_index,
        need_input=True, need_output=False, role="input (ears)", notes=route.notes,
    )
    mouth = _pick_configured(
        cables, settings.output_device_name, settings.output_device_index,
        need_input=False, need_output=True, role="output (mouth)", notes=route.notes,
    )
    # Unconfigured ends fall back along the *wiring*, not along PortAudio's own
    # notion of a default: the device the delegate records from is the one the
    # browser plays into, which is the system default OUTPUT, and the device it
    # plays into is the browser's microphone, the system default INPUT. Taking
    # the default input for the ears - the obvious-looking choice - points the
    # delegate's microphone at its own mouth.
    default_in, default_out = _default_indexes() if defaults is None else defaults
    if ears is None and default_out is not None:
        ears = find_by_index(cables, default_out, need_input=True, need_output=False)
        if ears is not None:
            route.notes.append(
                f"input not configured; using the system default output {ears.label()}, which is what the "
                "browser plays the meeting into"
            )
    if mouth is None and default_in is not None:
        mouth = find_by_index(cables, default_in, need_input=False, need_output=True)
        if mouth is not None:
            route.notes.append(
                f"output not configured; using the system default input {mouth.label()}, which is what the "
                "browser uses as its microphone"
            )

    route.ears, route.mouth = ears, mouth
    if not probe:
        route.notes.append("cable probing disabled (DELEG8_AUDIO_PREFLIGHT=false)")
        return route

    probes = probe_all(cables) if probes is None else probes
    by_index = {p.cable.index: p for p in probes}
    route.probed = True
    route.probes = probes
    route.alternatives = [
        p.cable for p in sorted(
            (p for p in probes if p.carries_audio),
            key=lambda p: (p.cable.looks_virtual, p.peak), reverse=True,
        )
    ]
    working = [p.cable.label() for p in probes if p.carries_audio]
    dead = [p.cable.label() for p in probes if not p.carries_audio and p.error is None]
    if dead:
        route.notes.append(
            "these loopback devices are installed but carry no audio: " + ", ".join(dead)
            + " (their driver is present but not loaded; on macOS restart Core Audio once with "
            "`sudo launchctl kickstart -kill system/com.apple.audio.coreaudiod`)"
        )

    # -- the ears: the one failure that makes the whole delegate pointless --
    route.ears_probe = by_index.get(ears.index) if ears is not None else None
    if ears is None or route.ears_probe is None or not route.ears_probe.carries_audio:
        replacement = _best_alternative(probes, exclude=mouth)
        if replacement is not None and (ears is None or replacement.index != ears.index):
            route.notes.append(
                f"input {ears.label() if ears else 'NONE'} cannot hear the meeting; "
                f"switched to {replacement.label()}"
            )
            route.ears = ears = replacement
            route.ears_probe = by_index.get(replacement.index)
            route.repaired = True

    # -- the mouth: without it the room hears nothing the delegate says --
    route.mouth_probe = by_index.get(mouth.index) if mouth is not None else None
    if mouth is None or route.mouth_probe is None or not route.mouth_probe.carries_audio:
        replacement = _best_alternative(probes, exclude=route.ears)
        if replacement is not None and (mouth is None or replacement.index != mouth.index):
            route.notes.append(
                f"output {mouth.label() if mouth else 'NONE'} cannot reach the meeting; "
                f"switched to {replacement.label()}"
            )
            route.mouth = mouth = replacement
            route.mouth_probe = by_index.get(replacement.index)
            route.repaired = True

    # -- the two ends must be two different devices --
    # Not a preference, a hard constraint. PortAudio cannot hold one Core Audio
    # device open as a separate input stream and output stream: the output
    # succeeds, the input reports AUHAL error -50 and then delivers zero frames
    # forever, so the delegate goes completely deaf. (Sharing would also feed it
    # its own voice as if the room had said it.) The ears keep the device the
    # browser is already playing into; the mouth moves.
    if route.ears is not None and route.mouth is not None and route.ears.index == route.mouth.index:
        replacement = _best_alternative(probes, exclude=route.ears) or next(
            (c for c in cables if c.is_loopback and c.index != route.ears.index), None
        )
        if replacement is not None:
            carries = by_index.get(replacement.index)
            route.notes.append(
                f"both ends resolved to {route.ears.label()}, which PortAudio cannot open twice - the input "
                f"would deliver nothing at all; moved the output to {replacement.label()}"
                + ("" if carries is None or carries.carries_audio else " (which carries no audio, so the room "
                   "may not hear the delegate - but it can still hear the room)")
            )
            route.mouth = replacement
            route.mouth_probe = carries
            route.repaired = True
        else:
            route.mouth = None
            route.notes.append(
                f"{route.ears.label()} is the only loopback device, and it cannot be both ends at once: "
                "PortAudio opens the output and then the input delivers nothing, leaving the delegate "
                "completely deaf. Install a second virtual cable - macOS `brew install --cask "
                "blackhole-16ch` - so it can hear through one and speak through the other."
            )
    if not working:
        route.notes.append(
            "no working loopback device was found, so the delegate cannot hear or be heard in a browser "
            "meeting. Install one: macOS `brew install --cask blackhole-2ch`, Windows VB-Cable, "
            "Linux a PulseAudio/PipeWire null sink."
        )
    return route


# --------------------------------------------------------------------------- #
# System defaults (what Chromium follows)
# --------------------------------------------------------------------------- #


def _current_default(tool: str, kind: str) -> str | None:
    """The name macOS currently calls the default ``input`` or ``output`` device."""
    result = subprocess.run([tool, "-c", "-t", kind], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def _set_default(tool: str, kind: str, cable: Cable) -> bool:
    """Make ``cable`` the system default for ``kind``, and confirm that it stuck.

    The confirmation is not belt-and-braces: some virtual devices accept the
    assignment, report success, and are then silently reverted by Core Audio a
    moment later (a conferencing app's own device typically refuses to be a
    system default while that app is not running). Trusting the exit code there
    produces a route that looks applied and is not - the same class of silent
    failure this whole module exists to catch - so the default is read back.
    """
    if _current_default(tool, kind) == cable.name:
        return True
    result = subprocess.run([tool, "-t", kind, "-s", cable.name], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        logger.warning(
            "could not set the system default %s to %r: %s",
            kind, cable.name, (result.stderr or result.stdout).strip(),
        )
        return False
    return _current_default(tool, kind) == cable.name


def apply_system_defaults(route: AudioRoute) -> list[str]:
    """Point the system default output at the ears and the default input at the mouth.

    Chromium has no per-tab device configuration this bot can set from outside,
    so it uses whatever the OS calls default. Every assignment is read back,
    because some virtual devices accept it, report success, and are then
    silently reverted by Core Audio. A mouth cable the OS will not accept is
    *not* swapped for the ear cable - one device cannot serve both ends - so
    instead :attr:`AudioRoute.in_app_mic` asks the meeting app to select it
    itself. Returns the changes made; never raises.
    """
    changes: list[str] = []
    if route.ears is None or route.mouth is None:
        return changes
    if sys.platform != "darwin":
        return changes
    tool = shutil.which("SwitchAudioSource")
    if tool is None:
        route.notes.append(
            "SwitchAudioSource is not installed, so the system defaults were left as they are - Chromium "
            "will use them rather than the cables chosen here. macOS: brew install switchaudio-osx"
        )
        return changes

    if route.ears.output_channels > 0:
        if _set_default(tool, "output", route.ears):
            changes.append(f"system default output -> {route.ears.name} (the browser plays the meeting here)")
        else:
            route.notes.append(
                f"macOS would not make {route.ears.name!r} the default output, so the browser will not play "
                "the meeting into the delegate's ears. Set it by hand in System Settings > Sound."
            )

    if route.mouth.input_channels > 0 and _set_default(tool, "input", route.mouth):
        changes.append(f"system default input -> {route.mouth.name} (the browser's microphone)")
    else:
        # The mouth cable cannot become the default input, and the ear cable
        # must not take its place: one device cannot be both ends. The meeting
        # app can still choose a non-default input itself, which is the only
        # remaining way to get the delegate's voice into the call.
        route.in_app_mic = route.mouth.name
        route.notes.append(
            f"macOS would not make {route.mouth.name!r} the default input, so the meeting app has to select "
            "it as its microphone itself (BOT_MEET_MIC_NAME). The ear cable cannot stand in for it: one "
            "device cannot serve as both ends of the route."
        )
    return changes


def preflight(settings: VoiceSettings) -> tuple[AudioRoute, list[str]]:
    """Resolve, measure and apply the audio route for one session.

    Blocking for roughly a second per loopback device. Call before the browser
    starts: the probe tone goes into a cable the meeting would otherwise be
    carrying, and the system defaults have to be right before Chromium opens
    its audio streams.
    """
    route = resolve_route(settings, probe=settings.verify_cables)
    changes = apply_system_defaults(route) if settings.auto_route else []
    for change in changes:
        route.notes.append(change)
    return route, changes


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str]) -> int:
    """Probe every loopback device and show the route the next session would use."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="also set the system defaults to the chosen cables")
    parser.add_argument("--no-probe", action="store_true", help="resolve devices without measuring them")
    args = parser.parse_args(argv)

    cables = list_cables()
    print(f"{'idx':>3}  {'in':>3} {'out':>3}  name")
    for cable in cables:
        tags = []
        if cable.is_loopback:
            tags.append("loopback")
        if cable.looks_virtual:
            tags.append("virtual")
        print(f"{cable.index:>3}  {cable.input_channels:>3} {cable.output_channels:>3}  "
              f"{cable.name}{('  [' + ', '.join(tags) + ']') if tags else ''}")

    if not args.no_probe:
        print("\nprobing loopback devices (a tone is played into each and recorded back):")
        for probe in probe_all(cables):
            mark = "ok  " if probe.carries_audio else "DEAD"
            print(f"  {mark} {probe.describe()}")

    settings = VoiceSettings.route_from_env()
    route = resolve_route(settings, probe=not args.no_probe)
    # Applying can change the route - a device the OS refuses as a default is
    # swapped out - so describe it afterwards, never before.
    changes = apply_system_defaults(route) if args.apply else []
    print("\nroute for the next session:")
    print(route.describe())
    for change in changes:
        print(f"  applied: {change}")
    if not args.apply and route.usable:
        print("  (add --apply to point the system defaults at these cables so Chromium uses them)")
    print(f"\nusable: {route.usable}")
    return 0 if route.usable else 1


if __name__ == "__main__":  # pragma: no cover - manual tool
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    logging.basicConfig(level=os.getenv("DELEG8_LOG_LEVEL", "INFO"))
    sys.exit(main(sys.argv[1:]))
