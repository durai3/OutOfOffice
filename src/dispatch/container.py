"""
One container per meeting, per user - the dispatcher the worker image was built for.

``docker/Dockerfile`` has always produced an image that joins exactly one
meeting and exits. This is the piece that finally runs it, and it is what
makes concurrency real: two meetings are two containers with two Chromium
processes, two virtual audio device pairs and two PulseAudio daemons, instead
of two asyncio tasks fighting over one machine's single default microphone.

Isolation falls out of that. A container gets only its own tenant's
credentials, only its own tenant's document volume, and a job token that
authorises exactly one meeting. It cannot see another tenant's memory because
it does not share an address space with one.

The Docker CLI is driven over ``asyncio.create_subprocess_exec`` rather than
through a Python SDK: there is no extra dependency, it works identically
against Docker Desktop, a remote ``DOCKER_HOST``, Colima and podman's
docker-compatible shim, and the exact command is printable in a log line when
someone needs to reproduce a launch by hand.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from pathlib import Path

from src.db.models import MeetingRuntime
from src.dispatch.base import (
    DispatchError,
    DispatchHandle,
    DispatchUnavailable,
    MeetingJob,
)
from src.tenancy.paths import TenantPaths

logger = logging.getLogger("deleg8.dispatch.container")

DEFAULT_IMAGE: str = "deleg8-worker:latest"
STOP_TIMEOUT_SECONDS: int = 90
"""Grace period ``docker stop`` gives the worker to leave the call and send its
briefing before SIGKILL. Matches the server's own shutdown grace."""


def image_name() -> str:
    """``DELEG8_WORKER_IMAGE`` (default ``deleg8-worker:latest``)."""
    return os.getenv("DELEG8_WORKER_IMAGE", "").strip() or DEFAULT_IMAGE


def docker_binary() -> str:
    """``DELEG8_DOCKER_BIN`` (default ``docker``)."""
    return os.getenv("DELEG8_DOCKER_BIN", "").strip() or "docker"


async def _run(*args: str, timeout: float = 60.0) -> tuple[int, str, str]:
    """Run the Docker CLI and capture its output."""
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        raise DispatchError(f"docker command timed out after {timeout:.0f}s: {' '.join(args)}") from exc
    return process.returncode or 0, stdout.decode().strip(), stderr.decode().strip()


class ContainerDispatcher:
    """Launches ``deleg8-worker`` containers, one per meeting."""

    name = "container"

    def __init__(self, image: str | None = None, *, network: str | None = None) -> None:
        """Bind to an image (default :func:`image_name`) and an optional Docker network."""
        self._image = image or image_name()
        self._network = network or os.getenv("DELEG8_WORKER_NETWORK", "").strip() or None
        self._checked: tuple[bool, str] | None = None

    def describe(self) -> str:
        """One line for the health page."""
        where = f" on network {self._network}" if self._network else ""
        return f"container dispatcher: {self._image}{where}"

    async def available(self) -> tuple[bool, str]:
        """Whether Docker answers and the worker image exists. Cached after the first check."""
        if self._checked is not None:
            return self._checked
        binary = docker_binary()
        try:
            code, _out, err = await _run(binary, "info", "--format", "{{.ServerVersion}}", timeout=20.0)
        except (DispatchError, FileNotFoundError) as exc:
            self._checked = (False, f"docker is not usable here: {exc}")
            return self._checked
        if code != 0:
            self._checked = (False, f"docker daemon is not reachable: {err or 'unknown error'}")
            return self._checked
        code, out, _err = await _run(binary, "image", "inspect", self._image, "--format", "{{.Id}}", timeout=20.0)
        if code != 0 or not out:
            self._checked = (
                False,
                f"worker image {self._image} is not built. Build it with: "
                f"docker build -f docker/Dockerfile -t {self._image} .",
            )
            return self._checked
        self._checked = (True, f"docker ready, image {self._image} present")
        return self._checked

    def recheck(self) -> None:
        """Forget the cached availability answer (the image was just built)."""
        self._checked = None

    async def launch(self, job: MeetingJob) -> DispatchHandle:
        """Start one worker container for ``job`` and return its handle."""
        ok, detail = await self.available()
        if not ok:
            raise DispatchUnavailable(detail)

        paths = TenantPaths.for_user(job.user_id).ensure()
        name = job.container_name()
        await self._remove_stale(name)

        argv = [docker_binary(), "run", "--detach", "--name", name]
        for key, value in self._labels(job).items():
            argv += ["--label", f"{key}={value}"]
        for key, value in job.environment().items():
            argv += ["--env", f"{key}={value}"]
        for key, value in self._passthrough_env().items():
            argv += ["--env", f"{key}={value}"]
        argv += ["--volume", f"{paths.root}:/data"]
        # A meeting is one browser plus two audio pipelines; without a floor the
        # default 64 MB /dev/shm makes Chromium fall over in a way that looks
        # like a join failure rather than an out-of-memory.
        argv += ["--shm-size", os.getenv("DELEG8_WORKER_SHM", "1g")]
        memory = os.getenv("DELEG8_WORKER_MEMORY", "").strip()
        if memory:
            argv += ["--memory", memory]
        cpus = os.getenv("DELEG8_WORKER_CPUS", "").strip()
        if cpus:
            argv += ["--cpus", cpus]
        if self._network:
            argv += ["--network", self._network]
        elif _on_docker_desktop():
            # The worker has to reach this server's callback endpoint. On Docker
            # Desktop the host is only addressable by this alias, so it is added
            # unconditionally; on Linux bridge networking it is a harmless no-op.
            argv += ["--add-host", "host.docker.internal:host-gateway"]
        argv += [self._image, *job.command()]

        logger.info("launching worker: %s", " ".join(shlex.quote(a) for a in _redacted(argv)))
        code, out, err = await _run(*argv, timeout=120.0)
        if code != 0:
            raise DispatchError(f"could not start the worker container: {err or out}")
        container_id = out.strip()
        logger.info(
            "meeting %s running in container %s (%s)", job.meeting_key, container_id[:12], name
        )
        return DispatchHandle(
            runtime=MeetingRuntime.CONTAINER,
            reference=container_id,
            detail=f"container {name}",
            extra={"container_name": name, "image": self._image},
        )

    def _labels(self, job: MeetingJob) -> dict[str, str]:
        """Labels that make a stray container traceable to its tenant and meeting."""
        return {
            "deleg8.meeting": str(job.meeting_id),
            "deleg8.user": str(job.user_id),
            "deleg8.meeting_key": job.meeting_key,
            "deleg8.role": "worker",
        }

    @staticmethod
    def _passthrough_env() -> dict[str, str]:
        """Deployment settings a worker needs that are not tenant-specific.

        The database URL is deliberately *not* among them: a worker reports
        through the callback API with a single-meeting token, so a compromised
        worker cannot reach another tenant's rows.
        """
        names = (
            "DELEG8_LOG_LEVEL", "DELEG8_RAG_MIN_SIMILARITY", "DELEG8_RAG_TOP_K",
            "DELEG8_CHUNK_SIZE", "DELEG8_CHUNK_OVERLAP", "DELEG8_EMBEDDING_MODEL",
            "DELEG8_ANTHROPIC_FAST_MODEL", "DELEG8_ANTHROPIC_SMART_MODEL",
            "DELEG8_GEMINI_FAST_MODEL", "DELEG8_GEMINI_SMART_MODEL",
            "DEEPGRAM_MODEL", "CARTESIA_MODEL_ID", "CARTESIA_VOICE_ID",
            "ESCALATION_TIMEOUT_SECONDS", "BOT_DISPLAY_NAME", "BOT_EMAIL", "BOT_PASSWORD",
            "DELEG8_MEMORY_WINDOW",
        )
        return {name: os.environ[name] for name in names if os.environ.get(name, "").strip()}

    async def _remove_stale(self, name: str) -> None:
        """Remove a dead container left over from a previous run of the same meeting."""
        code, out, _err = await _run(
            docker_binary(), "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.ID}}", timeout=20.0
        )
        if code == 0 and out:
            logger.info("removing stale container %s", name)
            await _run(docker_binary(), "rm", "-f", name, timeout=30.0)

    async def stop(self, handle: DispatchHandle, reason: str) -> None:
        """Ask the worker to leave: SIGTERM, then a grace period to brief and exit.

        ``python -m src.main join`` installs a SIGTERM handler that requests a
        clean stop, so the container leaves the call and sends its briefing
        rather than vanishing mid-sentence.
        """
        logger.info("stopping worker %s: %s", handle.reference[:12], reason)
        code, _out, err = await _run(
            docker_binary(), "stop", "--timeout", str(STOP_TIMEOUT_SECONDS), handle.reference,
            timeout=STOP_TIMEOUT_SECONDS + 30.0,
        )
        if code != 0:
            logger.warning("could not stop container %s: %s", handle.reference[:12], err)

    async def poll(self, handle: DispatchHandle) -> tuple[bool, int | None]:
        """Whether the container is still running, and its exit code once it is not."""
        code, out, _err = await _run(
            docker_binary(), "inspect", handle.reference,
            "--format", "{{.State.Running}} {{.State.ExitCode}}", timeout=20.0,
        )
        if code != 0:
            return False, None
        parts = out.split()
        running = parts[0].lower() == "true" if parts else False
        exit_code = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else None
        return running, (None if running else exit_code)

    async def logs(self, handle: DispatchHandle, tail: int = 200) -> str:
        """The worker's recent output - the first thing to look at when a join fails."""
        code, out, err = await _run(
            docker_binary(), "logs", "--tail", str(tail), handle.reference, timeout=30.0
        )
        return out if code == 0 else (err or "no logs available")

    async def remove(self, handle: DispatchHandle) -> None:
        """Delete a finished container."""
        await _run(docker_binary(), "rm", "-f", handle.reference, timeout=30.0)

    async def adopt(self, meeting_id: str) -> DispatchHandle | None:
        """Find a still-running container for a meeting after a server restart.

        Without this a control-plane restart orphans live meetings: the
        containers keep sitting in their calls with nothing tracking them.
        """
        code, out, _err = await _run(
            docker_binary(), "ps", "--filter", f"label=deleg8.meeting={meeting_id}",
            "--format", "{{.ID}} {{.Names}}", timeout=20.0,
        )
        if code != 0 or not out:
            return None
        container_id, _, name = out.splitlines()[0].partition(" ")
        return DispatchHandle(
            runtime=MeetingRuntime.CONTAINER,
            reference=container_id,
            detail=f"adopted container {name.strip()}",
            extra={"container_name": name.strip(), "image": self._image, "adopted": True},
        )


_SECRET_MARKERS = ("API_KEY", "TOKEN", "PASSWORD", "SECRET")


def _redacted(argv: list[str]) -> list[str]:
    """The command line with secret env values masked, safe to log."""
    out: list[str] = []
    for item in argv:
        if "=" in item and any(marker in item.split("=", 1)[0].upper() for marker in _SECRET_MARKERS):
            key = item.split("=", 1)[0]
            out.append(f"{key}=***")
        else:
            out.append(item)
    return out


def _on_docker_desktop() -> bool:
    """True on Docker Desktop (macOS/Windows), where the host needs an explicit alias."""
    return Path("/var/run/docker.sock").exists() and os.uname().sysname.lower() == "darwin"
