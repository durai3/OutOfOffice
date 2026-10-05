"""
Audio device setup for the Google Meet bot.

The bot hears the meeting and speaks into it through two virtual audio
cables (on macOS: BlackHole 2ch + BlackHole 16ch; on Windows: two VB-Cables;
on Linux: two PulseAudio/PipeWire null sinks):

    Meet participants -> Chromium speaker (= system default OUTPUT) -> cable A -> DELEG8 INPUT  -> Deepgram
    Cartesia TTS      -> DELEG8 OUTPUT -> cable B -> Chromium microphone (= system default INPUT) -> Meet

So the two DELEG8 indexes point at the far ends of the cables:

    DELEG8_INPUT_DEVICE_INDEX  = cable A     and the system default output is also cable A
    DELEG8_OUTPUT_DEVICE_INDEX = cable B     and the system default input  is also cable B

This script lists every PortAudio device (sounddevice and PyAudio share the
same PortAudio enumeration, so the indexes are interchangeable and the script
cross-checks them), suggests the virtual cables by name, lets you choose,
tests them, and writes the indexes into ``.env``.

Usage::

    python -m src.audio.device_setup                       # list devices, mark suggestions
    python -m src.audio.device_setup --select --write      # interactive choice, saved to .env
    python -m src.audio.device_setup --input 3 --output 5 --write
    python -m src.audio.device_setup --test-input 3        # 2 s level meter on device 3
    python -m src.audio.device_setup --test-output 5       # 1 s tone on device 5
    python -m src.audio.device_setup --set-system-defaults # macOS: point Chromium at the cables
"""

from __future__ import annotations

import argparse
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import sounddevice as sd

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
ENV_PATH: Path = PROJECT_ROOT / ".env"

VIRTUAL_CABLE_HINTS: tuple[str, ...] = (
    "blackhole", "vb-cable", "cable input", "cable output", "vb-audio", "voicemeeter",
    "virtual", "loopback", "soundflower", "null sink", "monitor of",
)
INPUT_KEY: str = "DELEG8_INPUT_DEVICE_INDEX"
OUTPUT_KEY: str = "DELEG8_OUTPUT_DEVICE_INDEX"

ROUTING_DIAGRAM: str = """\
Wiring (two virtual cables):
  Meet participants -> Chromium speaker (= system default OUTPUT) -> cable A -> DELEG8 INPUT  -> Deepgram
  Cartesia TTS      -> DELEG8 OUTPUT -> cable B -> Chromium microphone (= system default INPUT) -> Meet
  => DELEG8_INPUT_DEVICE_INDEX  = cable A, and make cable A the system default output
  => DELEG8_OUTPUT_DEVICE_INDEX = cable B, and make cable B the system default input
  Using ONE cable for both would feed the bot's own voice back into its ears.
  macOS: brew install --cask blackhole-2ch blackhole-16ch && brew install switchaudio-osx
  (BlackHole is a cask, not a formula; installing it does not load the driver -
   restart Core Audio once so macOS picks it up: see --check-driver below)
"""

_CORE_AUDIO_RESTART_CMD: str = "sudo launchctl kickstart -kill system/com.apple.audio.coreaudiod"
_CORE_AUDIO_RESTART_HINT: str = f"""\
If you already ran `brew install --cask blackhole-2ch blackhole-16ch` but it's
still not listed above: the cask drops the driver files but does not load them.
Core Audio only scans for new HAL plug-ins when it starts, so restart it once
(a fraction of a second of audio glitch, nothing else is affected):
    {_CORE_AUDIO_RESTART_CMD}
then rerun this script. That command needs your password, so it has to be run
by you in a real terminal, not by an agent.\
"""


@dataclass(frozen=True)
class Device:
    """One PortAudio device as seen by sounddevice."""

    index: int
    name: str
    host_api: str
    input_channels: int
    output_channels: int
    default_sample_rate: int
    is_default_input: bool
    is_default_output: bool

    @property
    def looks_virtual(self) -> bool:
        """True when the name matches a known virtual-cable product."""
        lowered = self.name.lower()
        return any(hint in lowered for hint in VIRTUAL_CABLE_HINTS)


def list_devices() -> list[Device]:
    """Enumerate every device with its capabilities."""
    default_in, default_out = sd.default.device
    host_apis = sd.query_hostapis()
    devices: list[Device] = []
    for index, info in enumerate(sd.query_devices()):
        devices.append(
            Device(
                index=index,
                name=str(info["name"]),
                host_api=str(host_apis[int(info["hostapi"])]["name"]),
                input_channels=int(info["max_input_channels"]),
                output_channels=int(info["max_output_channels"]),
                default_sample_rate=int(info["default_samplerate"]),
                is_default_input=index == default_in,
                is_default_output=index == default_out,
            )
        )
    return devices


def cross_check_with_pyaudio(devices: list[Device]) -> list[str]:
    """Confirm PyAudio (what the voice loop uses) sees the same indexes and names."""
    try:
        from src.audio.audio_io import list_audio_devices
    except ImportError as exc:  # pragma: no cover - PyAudio missing
        return [f"PyAudio not importable ({exc}); indexes could not be cross-checked"]
    problems: list[str] = []
    pyaudio_devices = {d.index: d for d in list_audio_devices()}
    for device in devices:
        other = pyaudio_devices.get(device.index)
        if other is None:
            problems.append(f"index {device.index} ({device.name}) is not visible to PyAudio")
        elif other.name.strip() != device.name.strip():
            problems.append(f"index {device.index}: sounddevice says {device.name!r}, PyAudio says {other.name!r}")
    return problems


def suggest(devices: list[Device]) -> tuple[Device | None, Device | None]:
    """Guess cable A (DELEG8 input) and cable B (DELEG8 output) from the device names."""
    virtual = [d for d in devices if d.looks_virtual]
    inputs = [d for d in virtual if d.input_channels > 0]
    outputs = [d for d in virtual if d.output_channels > 0]
    cable_a = inputs[0] if inputs else None
    cable_b = next((d for d in outputs if cable_a is None or d.index != cable_a.index), None)
    return cable_a, cable_b


def print_table(devices: list[Device], *, suggestion: tuple[Device | None, Device | None]) -> None:
    """Human-readable device table."""
    cable_a, cable_b = suggestion
    print(f"{'idx':>3}  {'in':>2} {'out':>3}  {'rate':>6}  {'flags':<14} name  [host api]")
    for d in devices:
        flags: list[str] = []
        if d.is_default_input:
            flags.append("sys-in")
        if d.is_default_output:
            flags.append("sys-out")
        if d.looks_virtual:
            flags.append("virtual")
        if cable_a is not None and d.index == cable_a.index:
            flags.append("=> A")
        if cable_b is not None and d.index == cable_b.index:
            flags.append("=> B")
        print(f"{d.index:>3}  {d.input_channels:>2} {d.output_channels:>3}  {d.default_sample_rate:>6}  "
              f"{','.join(flags):<14} {d.name}  [{d.host_api}]")


def _find(devices: list[Device], index: int) -> Device:
    for d in devices:
        if d.index == index:
            return d
    raise SystemExit(f"no audio device with index {index}")


def prompt_choice(devices: list[Device], *, kind: str, default: Device | None) -> Device:
    """Ask for an index on stdin, validating the direction."""
    want_input = kind == "input"
    while True:
        hint = f" [{default.index}]" if default is not None else ""
        raw = input(f"Index of the device DELEG8 should use as its {kind.upper()}{hint}: ").strip()
        if not raw and default is not None:
            return default
        if not raw.isdigit():
            print("  enter a number from the table")
            continue
        device = next((d for d in devices if d.index == int(raw)), None)
        if device is None:
            print("  no such index")
            continue
        channels = device.input_channels if want_input else device.output_channels
        if channels == 0:
            print(f"  device {device.index} has no {kind} channels")
            continue
        return device


def test_input(device: Device, seconds: float = 2.0, sample_rate: int = 16000) -> None:
    """Record from ``device`` and print a level meter, slice by slice."""
    print(f"Recording {seconds:.0f}s from [{device.index}] {device.name}; play something into it now...")
    frames = int(seconds * sample_rate)
    recording = sd.rec(frames, samplerate=sample_rate, channels=1, dtype="float32", device=device.index)
    sd.wait()
    samples = recording[:, 0]
    slices = np.array_split(samples, 10)
    for number, chunk in enumerate(slices, start=1):
        rms = float(np.sqrt(np.mean(chunk ** 2))) if chunk.size else 0.0
        dbfs = 20 * math.log10(rms) if rms > 0 else -120.0
        bar = "#" * max(0, min(40, int((dbfs + 60) / 60 * 40)))
        print(f"  {number:>2}/10 {dbfs:7.1f} dBFS |{bar:<40}|")
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    verdict = "signal present" if peak > 0.01 else "silence: nothing is reaching this device"
    print(f"  peak {peak:.3f} -> {verdict}")


def test_output(device: Device, seconds: float = 1.0, sample_rate: int = 24000, frequency: float = 440.0) -> None:
    """Play a sine tone on ``device``."""
    print(f"Playing a {frequency:.0f} Hz tone for {seconds:.0f}s on [{device.index}] {device.name}...")
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    tone = (0.3 * np.sin(2 * math.pi * frequency * t)).astype("float32")
    channels = max(1, min(2, device.output_channels))
    data = np.column_stack([tone] * channels)
    sd.play(data, samplerate=sample_rate, device=device.index)
    sd.wait()
    print("  done (whatever listens on the other end of that cable should have heard it)")


def write_env(updates: dict[str, str], path: Path = ENV_PATH) -> None:
    """Set keys in ``.env`` in place, preserving order and comments; append missing keys."""
    lines = path.read_text().splitlines() if path.exists() else []
    remaining = dict(updates)
    for number, line in enumerate(lines):
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            lines[number] = f"{key}={remaining.pop(key)}"
    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Virtual audio cable indexes chosen by python -m src.audio.device_setup")
        lines.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(lines) + "\n")
    print(f"wrote {', '.join(f'{k}={v}' for k, v in updates.items())} to {path}")


def set_system_defaults(cable_a: Device, cable_b: Device) -> int:
    """macOS: make cable A the default output and cable B the default input (what Chromium uses).

    Fails before touching anything if either device cannot serve that role
    (e.g. a real microphone has no output side): ``SwitchAudioSource``'s own
    error for that case is a generic "no such device", which is not
    actionable, so the reason is diagnosed here instead.
    """
    if cable_a.output_channels == 0:
        print(f"[ERROR] {cable_a.name!r} (chosen as cable A / {INPUT_KEY}) has no output channels, so it cannot "
              "become the system default output. A real microphone can only ever be an input; only a loopback "
              "device (BlackHole, VB-Cable, ...) can be both.")
        return 1
    if cable_b.input_channels == 0:
        print(f"[ERROR] {cable_b.name!r} (chosen as cable B / {OUTPUT_KEY}) has no input channels, so it cannot "
              "become the system default input. A real speaker can only ever be an output; only a loopback "
              "device (BlackHole, VB-Cable, ...) can be both.")
        return 1
    tool = shutil.which("SwitchAudioSource")
    if tool is None:
        print("SwitchAudioSource is not installed. macOS: brew install switchaudio-osx\n"
              "Otherwise choose the defaults in System Settings > Sound (output = cable A, input = cable B),\n"
              "or set BOT_MEET_MIC_NAME / BOT_MEET_SPEAKER_NAME so the bot picks them inside Meet.")
        return 1
    for kind, device in (("output", cable_a), ("input", cable_b)):
        result = subprocess.run([tool, "-t", kind, "-s", device.name], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            print(f"could not set system {kind} to {device.name!r}: {result.stderr.strip() or result.stdout.strip()}")
            return 1
        print(f"system default {kind} -> [{device.index}] {device.name}")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--select", action="store_true", help="choose the two devices interactively")
    parser.add_argument("--input", type=int, default=None, help=f"index for {INPUT_KEY} (cable A)")
    parser.add_argument("--output", type=int, default=None, help=f"index for {OUTPUT_KEY} (cable B)")
    parser.add_argument("--write", action="store_true", help="save the chosen indexes to .env")
    parser.add_argument("--test-input", type=int, default=None, metavar="IDX", help="record 2 s and show levels")
    parser.add_argument("--test-output", type=int, default=None, metavar="IDX", help="play a 1 s tone")
    parser.add_argument("--set-system-defaults", action="store_true",
                        help="macOS: set system output = cable A and input = cable B (needs switchaudio-osx)")
    parser.add_argument("--force", action="store_true",
                        help="write/apply even if the chosen devices don't look like virtual cables "
                             "(real hardware cannot route Meet audio; use only if you know what you're doing)")
    parser.add_argument("--env", type=Path, default=ENV_PATH, help="path of the .env to update")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """Entry point."""
    args = parse_args(argv)
    devices = list_devices()
    if not devices:
        print("No audio devices found. On macOS install BlackHole; on Linux create PulseAudio null sinks.")
        return 1
    suggestion = suggest(devices)
    print(ROUTING_DIAGRAM)
    print_table(devices, suggestion=suggestion)
    problems = cross_check_with_pyaudio(devices)
    for problem in problems:
        print(f"[WARN] {problem}")
    if not problems:
        print("(PyAudio sees the same indexes; the numbers above are what the voice loop will use)")

    cable_a, cable_b = suggestion
    if args.test_input is not None:
        test_input(_find(devices, args.test_input))
    if args.test_output is not None:
        test_output(_find(devices, args.test_output))

    chosen_in = _find(devices, args.input) if args.input is not None else None
    chosen_out = _find(devices, args.output) if args.output is not None else None
    if args.select:
        print()
        chosen_in = prompt_choice(devices, kind="input", default=chosen_in or cable_a)
        chosen_out = prompt_choice(devices, kind="output", default=chosen_out or cable_b)
    if chosen_in is None and chosen_out is None and not args.set_system_defaults:
        if cable_a is None and cable_b is None:
            print("\nNo virtual cable detected. Install one (see the wiring note above) and rerun.")
            print(_CORE_AUDIO_RESTART_HINT)
        else:
            print(f"\nSuggested: {INPUT_KEY}={cable_a.index if cable_a else '?'} (A), "
                  f"{OUTPUT_KEY}={cable_b.index if cable_b else '?'} (B). "
                  "Rerun with --select --write to confirm, or pass --input/--output.")
        return 0

    chosen_in = chosen_in or cable_a
    chosen_out = chosen_out or cable_b
    if chosen_in is None or chosen_out is None:
        print("Both an input (cable A) and an output (cable B) are needed.")
        return 1
    if chosen_in.index == chosen_out.index:
        print(f"[WARN] input and output are the same device ({chosen_in.name}); the bot will hear itself. "
              "Use two cables.")
    if chosen_in.input_channels == 0:
        print(f"device {chosen_in.index} cannot record")
        return 1
    if chosen_out.output_channels == 0:
        print(f"device {chosen_out.index} cannot play")
        return 1
    if not chosen_in.looks_virtual and not chosen_out.looks_virtual and (args.write or args.set_system_defaults):
        print(f"\n[WARN] neither {chosen_in.name!r} nor {chosen_out.name!r} looks like a virtual cable "
              "(BlackHole, VB-Cable, ...); they are real hardware. Routing Meet's audio through the bot needs "
              "a loopback device on both ends - real mic/speakers cannot do it, so the Meet bot will not hear "
              "the meeting or be heard in it with this configuration.")
        if not args.force:
            print("Install a virtual cable (see the wiring note above) and rerun, or pass --force to proceed "
                  "anyway (e.g. for a plain microphone/speaker test unrelated to Meet).")
            return 1
        print("--force given; proceeding anyway.")
    print(f"\n{INPUT_KEY}={chosen_in.index}   ({chosen_in.name})\n{OUTPUT_KEY}={chosen_out.index}   ({chosen_out.name})")
    if args.write:
        write_env({INPUT_KEY: str(chosen_in.index), OUTPUT_KEY: str(chosen_out.index)}, args.env)
    else:
        print("(add --write to save these to .env)")
    if args.set_system_defaults:
        return set_system_defaults(chosen_in, chosen_out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
