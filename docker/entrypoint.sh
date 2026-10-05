#!/usr/bin/env bash
# Brings up the two pieces of infrastructure a human normally sets up by hand
# on a laptop - a display for Chromium and a pair of virtual audio cables -
# then execs the real command (normally `python -m src.main join`).
#
# See src/gateway/bot_base.py's "Container audio routing" section for the
# full explanation of why this exists and exactly how the two PulseAudio
# devices below map onto the two physical/virtual cables device_setup.py
# helps a human wire up outside a container.
set -euo pipefail

log() { echo "[entrypoint] $*" >&2; }

# --------------------------------------------------------------------- #
# Virtual display
# --------------------------------------------------------------------- #
# Headless Chromium has no audio devices at all (see meet_bot.py's own
# docstring) - Xvfb makes Chromium believe it has a real screen so it
# behaves exactly as it does on a developer's machine with a visible window.
DISPLAY_NUM="${DELEG8_DISPLAY_NUM:-99}"
export DISPLAY=":${DISPLAY_NUM}"
Xvfb "$DISPLAY" -screen 0 "${DELEG8_SCREEN_RES:-1280x800x24}" -nolisten tcp -ac &
XVFB_PID=$!
trap 'kill "$XVFB_PID" 2>/dev/null || true' EXIT

log "waiting for Xvfb on $DISPLAY..."
for _ in $(seq 1 50); do
    if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
        log "Xvfb ready"
        break
    fi
    sleep 0.2
done

# --------------------------------------------------------------------- #
# Virtual audio
# --------------------------------------------------------------------- #
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/runtime-deleg8}"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

pulseaudio -D --exit-idle-time=-1 --disallow-exit --log-target=stderr
log "waiting for PulseAudio..."
for _ in $(seq 1 50); do
    if pactl info >/dev/null 2>&1; then
        log "PulseAudio ready"
        break
    fi
    sleep 0.2
done

# Two null-sinks stand in for the two cables (BlackHole 2ch / 16ch on macOS):
#   deleg8_from_meeting - Chromium's call audio (remote participants) lands
#                         here; this process's STT reads it via the sink's
#                         .monitor source.
#   deleg8_to_meeting   - this process's TTS (the delegate's own speech)
#                         lands here; Chromium reads it as its "microphone".
pactl load-module module-null-sink sink_name=deleg8_from_meeting \
    sink_properties=device.description=DELEG8_From_Meeting
pactl load-module module-null-sink sink_name=deleg8_to_meeting \
    sink_properties=device.description=DELEG8_To_Meeting

# A sink's .monitor source is flagged internally as a monitor
# (source.monitor-of-sink is set), and browsers - Chromium included -
# deliberately exclude monitor sources from the microphone picker (verified
# live 2026-09-23: PulseAudio had the devices, `pactl` saw them fine, but
# Chromium's pre-join screen still reported "Mic not found" pointed straight
# at deleg8_to_meeting.monitor). module-virtual-source clones that monitor's
# audio into a brand new source with no such flag, so it appears to Chromium
# as an ordinary, selectable microphone.
pactl load-module module-virtual-source master=deleg8_to_meeting.monitor \
    source_name=deleg8_to_meeting_mic source_properties=device.description=DELEG8_Mic

# This process: speak into deleg8_to_meeting, listen on what Chromium played
# into deleg8_from_meeting. PortAudio's own PulseAudio backend (used by both
# PyAudio and sounddevice) honours these two variables as "the default
# device" - no device index to discover or configure, and it has no
# monitor-hiding UI logic to work around, unlike Chromium's own picker above.
export PULSE_SINK=deleg8_to_meeting
export PULSE_SOURCE=deleg8_from_meeting.monitor

# Chromium is a *separate* process the bot launches (via Playwright); it
# needs the other half of the routing, cross-wired. src/gateway/meet_bot.py,
# zoom_bot.py and teams_bot.py all read these two and pass them to Chromium
# specifically via src.gateway.bot_base.chromium_pulse_env - see there for
# why this can't just be "the same PULSE_SINK/PULSE_SOURCE" for both processes.
export BOT_CHROMIUM_PULSE_SINK=deleg8_from_meeting
export BOT_CHROMIUM_PULSE_SOURCE=deleg8_to_meeting_mic

log "virtual audio ready (this process: sink=$PULSE_SINK source=$PULSE_SOURCE; chromium: sink=$BOT_CHROMIUM_PULSE_SINK source=$BOT_CHROMIUM_PULSE_SOURCE)"

if [ "${BOT_HEADLESS:-false}" = "true" ]; then
    log "WARNING: BOT_HEADLESS=true - headless Chromium has no audio devices even under Xvfb. Leave it false/unset in this image."
fi

log "starting: $*"
exec "$@"
