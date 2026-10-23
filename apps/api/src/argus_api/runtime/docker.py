"""The Docker calls project runtimes need, behind a small protocol so tests can
fake the daemon. Every call is a subprocess (never inside a database
transaction) with a minimal environment: no Argus token, account secret or
provider credential is visible to Docker or to a build."""

import asyncio
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from argus_core.runtime import Probe

OWNER_KEY = "dev.argus.runtime.owner"
KEY_LABEL = "dev.argus.runtime.key"
# Docker reads these to find the daemon; nothing else of Argus's environment passes.
_ENV_KEEP = (
    "PATH",
    "HOME",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "XDG_RUNTIME_DIR",
)


@dataclass(frozen=True)
class Daemon:
    id: str
    platform: str  # os/arch as Docker names them, e.g. linux/amd64


@dataclass(frozen=True)
class BuildResult:
    ok: bool
    log_tail: str


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    output: str


class Docker(Protocol):
    async def daemon(self) -> Daemon: ...
    async def image_id(self, ref: str) -> str | None: ...
    async def image_label(self, ref: str, label: str) -> str | None: ...
    async def tag(self, image_id: str, ref: str) -> None: ...
    async def build(
        self,
        *,
        dockerfile: str,
        files: dict[str, bytes],
        base: str,
        ref: str,
        key: str,
        timeout_s: int,
    ) -> BuildResult: ...
    async def probe(self, ref: str, probe: Probe, run_as: str) -> ProbeResult: ...
    async def remove_owned_containers(self) -> int: ...


class DockerError(Exception):
    pass


class CliDocker:
    """`docker` on PATH against the daemon this process is configured for."""

    def __init__(self, owner: str, binary: str = "docker") -> None:
        # Probe containers carry this Argus's label, so cleanup removes only its own.
        self.binary = binary
        self.owner_label = f"{OWNER_KEY}={owner}"
        self._env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}

    async def _run(
        self, *args: str, timeout_s: float = 60, stdin: bytes | None = None
    ) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary,
                *args,
                stdin=asyncio.subprocess.PIPE
                if stdin is not None
                else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=self._env,
            )
        except OSError as e:
            raise DockerError(f"docker is not available: {e.strerror or e}") from e
        try:
            out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise DockerError(
                f"docker {args[0]} timed out after {timeout_s:.0f}s"
            ) from None
        return proc.returncode or 0, out.decode(errors="replace")

    async def daemon(self) -> Daemon:
        code, out = await self._run(
            "info", "--format", "{{.ID}}|{{.OSType}}/{{.Architecture}}"
        )
        ident, _, platform = out.strip().partition("|")
        if code != 0 or not ident or not platform:
            raise DockerError(f"cannot read the Docker daemon: {out.strip()[:200]}")
        arch = {"x86_64": "amd64", "aarch64": "arm64"}
        os_name, _, machine = platform.partition("/")
        return Daemon(ident, f"{os_name}/{arch.get(machine, machine)}")

    async def image_id(self, ref: str) -> str | None:
        code, out = await self._run("image", "inspect", "--format", "{{.Id}}", ref)
        return out.strip() if code == 0 and out.strip().startswith("sha256:") else None

    async def image_label(self, ref: str, label: str) -> str | None:
        code, out = await self._run(
            "image",
            "inspect",
            "--format",
            f'{{{{index .Config.Labels "{label}"}}}}',
            ref,
        )
        return out.strip() or None if code == 0 else None

    async def tag(self, image_id: str, ref: str) -> None:
        code, out = await self._run("tag", image_id, ref)
        if code != 0:
            raise DockerError(f"docker tag failed: {out.strip()[:200]}")

    async def build(
        self,
        *,
        dockerfile: str,
        files: dict[str, bytes],
        base: str,
        ref: str,
        key: str,
        timeout_s: int,
    ) -> BuildResult:
        """Build in a fresh context that holds the Dockerfile and `files` only. `base`
        must be a tag of the exact base image (BuildKit cannot take an image id)."""
        with tempfile.TemporaryDirectory(prefix="argus-runtime-ctx-") as tmp:
            ctx = Path(tmp)
            (ctx / "Dockerfile").write_text(dockerfile)
            for name, data in files.items():
                (ctx / name).write_bytes(data)
            code, out = await self._run(
                "build",
                "--build-arg",
                f"BASE={base}",
                "--label",
                f"{KEY_LABEL}={key}",
                "--provenance=false",
                "--sbom=false",
                "-t",
                ref,
                str(ctx),
                timeout_s=timeout_s,
            )
        return BuildResult(code == 0, out[-2000:])

    async def probe(self, ref: str, probe: Probe, run_as: str) -> ProbeResult:
        """Run one probe like a task container runs: its uid, every capability dropped,
        no new privileges, no network, read-only root with scratch space in tmpfs."""
        name = f"argus-rt-probe-{secrets.token_hex(4)}"
        args = [
            "run",
            "--rm",
            "--name",
            name,
            "--label",
            self.owner_label,
            "--user",
            run_as,
            "--cap-drop=ALL",
            "--security-opt",
            "no-new-privileges",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,exec,nosuid,size=512m",  # noqa: S108 - a tmpfs inside the probe container
            "--pids-limit",
            "512",
            "--memory",
            "2g",
            "-e",
            "HOME=/home/node",
            "-e",
            "UV_CACHE_DIR=/tmp/uv-cache",
            "--entrypoint",
            probe.argv[0],
            ref,
            *probe.argv[1:],
        ]
        try:
            code, out = await self._run(*args, timeout_s=180)
        except DockerError as e:
            await self._run("rm", "-f", name)
            return ProbeResult(False, str(e))
        return ProbeResult(code == 0, out[-1500:])

    async def remove_owned_containers(self) -> int:
        code, out = await self._run(
            "ps", "-aq", "--filter", f"label={self.owner_label}"
        )
        ids = out.split() if code == 0 else []
        if ids:
            await self._run("rm", "-f", *ids)
        return len(ids)
