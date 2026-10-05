# The containerised meeting worker: one container = one bot in one call.
#
# Everything above the audio-capture layer (the brain, the voice loop, memory,
# escalation) is already just API calls and needs nothing platform-specific
# to run in a container. The only thing that changed to get here is *how*
# Chromium gets a display and a microphone/speaker - see docker/entrypoint.sh
# and src/gateway/bot_base.py's "Container audio routing" section.
#
# Build:   docker build -f docker/Dockerfile -t deleg8-worker .
# Run:     docker run --rm --env-file .env -e BOT_HEADLESS=false deleg8-worker
#          (BOT_HEADLESS must stay false/unset - see the entrypoint's own check)
#
# This image joins exactly one meeting and exits when the call ends (the same
# `python -m src.main join` behaviour as running it directly); a dispatcher
# that starts one container per meeting - Kubernetes Job, ECS/Fargate task,
# Cloud Run job - is what turns this into "thousands of concurrent meetings",
# not anything inside the image itself.

FROM python:3.13-slim

# build-essential  - a C/C++ toolchain (gcc, make, ...). pyaudio has no
#                    prebuilt wheel for linux/arm64 (and several others), so
#                    pip compiles its C extension from source here - without
#                    this the build fails with "No such file or directory: 'gcc'".
# portaudio19-dev  - the headers/library pyaudio's C extension (and
#                    sounddevice) link against - they are the ones actually
#                    reading/writing the two virtual devices the entrypoint creates.
# xvfb             - the virtual display Chromium runs against.
# pulseaudio(-utils) - the virtual audio devices themselves (module-null-sink),
#                    replacing the BlackHole cables a human installs locally.
# dbus-x11         - PulseAudio's per-user daemon mode wants a session bus.
# curl, ca-certificates - Playwright's own installer and general TLS.
# procps           - ps/kill, useful for diagnosing a stuck container.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        portaudio19-dev \
        xvfb \
        pulseaudio \
        pulseaudio-utils \
        dbus-x11 \
        curl \
        ca-certificates \
        procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium plus every OS package it needs at runtime. Deliberately run
# *after* `pip install` (not baked into a pinned Playwright base image) so
# the installed browser build always matches whatever `playwright` version
# requirements.txt actually resolved to - a mismatch between the two is the
# most common cause of a cryptic "Executable doesn't exist" failure.
RUN python -m playwright install --with-deps chromium

# Pre-download the sentence-transformers model into the image.
#
# Without this every worker fetches ~90 MB from Hugging Face on its way into a
# call - tens of seconds of a meeting that has already started, and an outright
# failure on a host with no egress to huggingface.co. Baked in, the store opens
# in about a second and the container works offline.
ENV HF_HOME=/opt/hf \
    SENTENCE_TRANSFORMERS_HOME=/opt/hf \
    TOKENIZERS_PARALLELISM=false \
    HF_HUB_DISABLE_PROGRESS_BARS=1
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2', device='cpu')"

COPY . .

RUN chmod +x docker/entrypoint.sh

# /data is one tenant's directory, bind-mounted by the dispatcher. The paths
# below therefore point at that tenant's own vector index and documents - and
# at nobody else's, because nobody else's is mounted. DELEG8_PROFILES_DIR and
# DELEG8_HISTORY_DIR are vestigial in a dispatched worker: the Representation
# Policy arrives in the job bundle and the briefing is archived by the control
# plane, so neither directory is written. They stay for the standalone
# single-user run (`docker run` with your own .env and no job token).
ENV PYTHONUNBUFFERED=1 \
    BOT_HEADLESS=false \
    BOT_CHROME_PROFILE_DIR=/data/chrome_profile \
    BOT_SCREENSHOT_DIR=/data/screenshots \
    DELEG8_CHROMA_DIR=/data/chroma \
    DELEG8_PROFILES_DIR=/data/profiles \
    DELEG8_HISTORY_DIR=/data/history \
    DELEG8_DOCUMENTS_DIR=/data/documents

VOLUME ["/data"]

ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["python", "-m", "src.main", "join"]
