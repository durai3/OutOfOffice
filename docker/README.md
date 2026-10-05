# The containerised meeting worker

This is Option A from the SaaS architecture discussion: the same Playwright
bots that already work locally (`src/gateway/meet_bot.py`, `zoom_bot.py`,
`teams_bot.py`), packaged so they run on a server with no physical machine,
no installed BlackHole cable, and no human present - a Docker image that
joins exactly one meeting and exits when the call ends.

Two pieces make that possible, both handled by `entrypoint.sh` before your
code ever runs:

1. **Xvfb** gives Chromium a display. Headless Chromium has no audio devices
   at all (`meet_bot.py`'s own docstring says so); Xvfb makes it believe it
   has a real window, the same as it does on a developer's laptop.
2. **PulseAudio virtual devices** replace the two physical/virtual cables a
   human wires up by hand locally (BlackHole on macOS, via
   `src/audio/device_setup.py`). Two null-sinks are created at container
   start and wired cross-ways - Chromium's call audio becomes this process's
   microphone input, and this process's speech becomes Chromium's microphone
   input - and everything above that layer (the brain, the voice loop,
   memory, escalation) runs completely unchanged, because it was already
   just API calls with no machine-specific dependency. One extra step is
   needed on Chromium's side specifically: a plain null-sink's `.monitor`
   source is flagged internally as a monitor, and browsers deliberately hide
   monitor sources from the microphone picker - `module-virtual-source`
   clones it into an unflagged source so Chromium can actually select it (see
   "Verified live" below for how this was found).

The exact routing is documented where it's implemented:
`src/gateway/bot_base.py`'s "Container audio routing" section and
`chromium_pulse_env()`.

## Build

```bash
docker build -f docker/Dockerfile -t deleg8-worker:latest .
```

The tag matters: `DELEG8_WORKER_IMAGE` (default `deleg8-worker:latest`) is what
the dispatcher launches, and `GET /api/v1/health` reports plainly when the image
is missing rather than failing at the first dispatch.

`pyaudio` has no prebuilt wheel for `linux/arm64` (and several other
platforms), so pip compiles its C extension from source during the build -
the image installs `build-essential` for exactly this. The build also
pre-downloads the sentence-transformers embedding model: without that, every
worker fetches ~90 MB from Hugging Face on its way into a call - tens of seconds
of a meeting that has already started, and an outright failure on a host with no
egress to huggingface.co. `.env` (and anything
else in `.dockerignore`) is deliberately excluded from the build context: an
earlier version of this image baked the real `.env` file into its layers via
`COPY . .` and `src/main.py`'s `load_dotenv()` picked it up automatically
regardless of what was passed at `docker run` time - a real credential leak
this was caught by, found through actually building and testing the image,
not by reading the Dockerfile. Config always comes in through `--env-file` /
`-e` at run time now; nothing baked in.

## Run one meeting

```bash
docker run --rm \
  --env-file .env \
  -e MEET_JOIN_URL=https://meet.google.com/abc-defg-hij \
  -v deleg8-data:/data \
  deleg8-worker
```

Swap `MEET_JOIN_URL` for `ZOOM_JOIN_URL` or `TEAMS_JOIN_URL` to join those
platforms instead - the container's command (`python -m src.main join`)
detects the platform from whichever link is set, exactly like it does
outside a container. `BOT_HEADLESS` must stay `false` (the image already
sets this) - Xvfb gives Chromium a display, but *headless* Chromium still
has no audio devices regardless of Xvfb, so setting it to `true` here breaks
the delegate silently.

The `/data` volume is where the persistent Chromium profile (so a signed-in
session survives the container's next run), the document/vector index,
profiles, and history all live - mount it, or every container starts from
scratch. `BOT_CHROME_PROFILE_DIR` etc. already point there via the image's
own `ENV` (see `Dockerfile`).

**Careful with `--env-file .env`:** your local `.env` almost certainly has
`BOT_CHROME_PROFILE_DIR=data/chrome_profile` and `BOT_SCREENSHOT_DIR=data/screenshots`
as plain relative paths for running directly on your machine - passed into
the container, those override the image's own `/data/...` defaults (an
`--env-file` value wins over the image's `ENV`) and quietly redirect both
into `/app/data/...` instead of your mounted volume, so nothing actually
persists and `-v .../chrome_profile:/data/chrome_profile` silently does
nothing (found by testing, not by inspection). Either strip those two
variables out of the env file you use for `docker run`, or override them
explicitly on the command line:

```bash
docker run --rm --env-file .env \
  -e BOT_CHROME_PROFILE_DIR=/data/chrome_profile \
  -e BOT_SCREENSHOT_DIR=/data/screenshots \
  -e MEET_JOIN_URL=https://meet.google.com/abc-defg-hij \
  -v deleg8-data:/data \
  deleg8-worker
```

**Signing in for the first time inside the container:** a Chromium profile
authenticated on macOS does not carry a usable session over to the Linux
Chromium build the container runs (verified live: mounting a real,
already-signed-in macOS profile still showed a blank "Sign in" screen, not a
remembered account) - Chrome's cookie/credential encryption is
platform-specific, so the files exist but aren't usable across that
boundary. A scripted sign-in from a brand-new environment is also exactly
the situation Google's own abuse detection is most likely to challenge (see
`MeetSettings`'s / `ZoomSettings`'s / `TeamsSettings`' setup instructions on
`BOT_MANUAL_LOGIN_SECONDS`). Practically, that means the *first* sign-in for
a bot account should happen natively inside a Linux environment, with a
human able to see the browser window to clear any challenge - e.g. run the
image once with a VNC/noVNC server added to the Xvfb display, or do the
bootstrap login directly on a Linux host/VM (not this Mac) - and persist the
resulting `/data/chrome_profile` from *that* run as the volume every future
container reuses. Guest join needs none of this and worked cleanly in every
test below.

## Verifying it works without a real meeting

Same dry-run the bots already support locally, now through the container:

```bash
docker run --rm --env-file .env deleg8-worker \
  python -m src.main join --dry-run --url https://meet.google.com/abc-defg-hij
```

This launches Chromium under Xvfb, signs in if credentials are configured,
opens the pre-join screen, and reports what it found - without clicking
Join or touching PulseAudio's routing at all (the dry run never speaks or
listens). It's the fastest way to confirm the image itself is healthy
(Playwright installed correctly, the display comes up, sign-in works)
independently of whether a particular platform's selectors are still
accurate - see each bot module's own docstring for the current verification
status of its selectors (Google Meet's are verified against a real account;
Zoom's and Teams' are not yet, and will need the same real-account iteration
Meet's went through).

## The dispatcher

This image is deliberately single-meeting: it joins one call and exits. For a
long time that left an obvious hole - the image existed, was verified, and
nothing in the product ever ran it. `src/dispatch/` is that missing piece.

`ContainerDispatcher` (`src/dispatch/container.py`) runs `docker run` per
meeting, per user, through `asyncio.create_subprocess_exec` against the Docker
CLI. No SDK dependency, works the same against Docker Desktop, a remote
`DOCKER_HOST`, Colima and podman's shim, and the exact command lands in the log
(with secrets masked) so a launch can be reproduced by hand. Swap the class for
a Kubernetes Job or an ECS task and nothing above it changes: the seam is the
`Dispatcher` protocol in `src/dispatch/base.py`.

Set `DELEG8_DISPATCH_MODE=container` to insist on it, or leave it at `auto`,
which uses containers when Docker is usable and falls back to an in-process
asyncio task otherwise.

### What a worker is given, and what it is not

A worker gets exactly three things:

* **Its tenant's `/data` directory**, bind-mounted - that account's documents
  and its own Chroma index, and nobody else's.
* **Provider API keys**, already resolved to that tenant's own or the
  platform's (`src/tenancy/credentials.py`).
* **A job token** authorising exactly one meeting.

It is deliberately *not* given `DATABASE_URL`. A connection that can read every
tenant's policies, transcripts and credentials has no business inside a
container that also runs a browser. Instead the worker asks for what it needs
over the job token:

| Worker needs | How it gets it |
|---|---|
| The Representation Policy and dossier | `GET /api/v1/worker/bundle` at startup (`src/dispatch/bundle.py`) |
| To show the console what is happening | `POST /api/v1/worker/events`, batched (`src/dispatch/callback.py`) |
| To ask its principal for approval | `POST /api/v1/worker/escalate` (`src/dispatch/relay.py`) |
| To deliver and archive the briefing | `POST /api/v1/worker/message` and `/report` |

The control plane clears the job token the moment the meeting reaches a
terminal state, so a container that outlives its call can do nothing.

That relay is what makes a containerised meeting equivalent to an in-process
one rather than a degraded version of it. Without it a worker has no Telegram
token, so its YELLOW tier would silently not exist, and the briefing it composed
would be delivered nowhere and archived nowhere.

### Labels and cleanup

Every container is labelled `deleg8.meeting`, `deleg8.user`,
`deleg8.meeting_key` and `deleg8.role=worker`, so a stray one is traceable to
its tenant. A control-plane restart does **not** kill running workers: they are
separate processes still sitting in real calls, and `ContainerDispatcher.adopt`
finds them again by label. When a worker fails, the last meaningful lines of its
log are copied into the meeting row before the container is removed - without
that, a failed join reads "exit 1" and nothing else.

## Verified live (2026-09-23)

The image was actually built and run against the real Meet link in `.env`,
not just reasoned about - this is what that testing found and fixed, in the
order it happened:

1. First build failed: `pip install` couldn't compile `pyaudio` (`No such
   file or directory: 'gcc'`). Fixed by adding `build-essential`.
2. First real run: joined as a guest, reached the real pre-join screen,
   reported "ready to join: True" - the core Xvfb + Chromium + navigation
   path worked on the first real attempt.
3. Sign-in stalled (Google needed a human on the password step) and the
   dry-run degraded exactly as designed: it caught `MeetLoginError`,
   printed the reason, and still completed the guest-view pre-join
   inspection rather than crashing.
4. Investigating the stall surfaced a real security bug: `.env` (real
   Gmail password, API keys, Telegram token) had been baked into the image
   by `COPY . .` and was silently overriding whatever was passed at
   `docker run` time. Fixed with `.dockerignore`.
5. Investigating sign-in further found the cross-OS profile issue described
   above (a macOS-authenticated profile screenshots as a blank sign-in
   form inside Linux Chromium, not a remembered account) - this remains an
   open item; guest join is unaffected by it.
6. The pre-join screen reported "Mic not found" despite PulseAudio's devices
   existing and being visible to `pactl` directly - Chromium's sandbox was
   the first suspect (`--no-sandbox` added, container mode only) and then
   the real cause was found: monitor sources are hidden from Chromium's mic
   picker by design. `module-virtual-source` fixed it - confirmed by
   screenshot: the mic dropdown now reads "Default" and the warning badge is
   gone, leaving only the (expected, no virtual camera exists) camera
   warning.

**Google Meet**: guest join verified end-to-end in the container, including
the mic-detection fix above; signed-in join is blocked on the bootstrap
sign-in issue described above, not on anything specific to this image.

**Zoom, Microsoft Teams**: not run against a live account or meeting in this
environment - no test accounts existed to test with. The bots are written to
the same defensive standard as the Meet bot (multiple selector fallbacks,
screenshot on every failure, never hangs past a timeout), and now inherit
the same `--no-sandbox` / `module-virtual-source` audio fixes automatically
since all three bots share `chromium_pulse_env()`. Get a test account and a
real meeting link, then run:

```bash
python -m src.gateway.zoom_bot --inspect <join-url>
python -m src.gateway.teams_bot --inspect <join-url>
```

(or the containerised `--dry-run` form above) and fix whichever selector in
`zoom_bot.py` / `teams_bot.py` doesn't match what you see -
`BOT_SCREENSHOT_DIR` gets a PNG on every failure specifically to make that
fast.

**Not yet tested**: a real two-way voice exchange (STT hearing a real
participant, TTS being heard by one) - dry-run deliberately never speaks or
listens, so the mic-detection fix proves Chromium can select the device, not
that a full round-trip conversation works. That needs an actual `join` (not
`--dry-run`) against a live meeting with someone on the other end.
