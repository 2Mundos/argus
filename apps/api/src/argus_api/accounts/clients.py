"""Pinned official clients, downloaded on first use (login clients spec).

The Claude and Codex logins run the official client. When neither
`ARGUS_*_LOGIN_IMAGE` nor `ARGUS_*_BIN` is set, the pinned build for this
platform is downloaded once, checked against its SHA-256 and kept under
`<data_dir>/tools/<client>-<version>/<client>`. A file at that path is always a
verified one: it is renamed there only after the check.
"""

import asyncio
import hashlib
import os
import platform
import shutil
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from argus_api.config import Settings

MAX_BYTES = 600 * 1024 * 1024
_CLAUDE = (
    "https://storage.googleapis.com/claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819"
    "/claude-code-releases/2.1.296/{}/claude"
)
_CODEX = (
    "https://github.com/openai/codex/releases/download/rust-v0.162.1"
    "/codex-{0}-unknown-linux-musl.tar.gz"
)


@dataclass(frozen=True)
class Pin:
    version: str
    url: str
    sha256: str
    member: str | None = None  # the binary inside a .tar.gz


def _claude(platform_: str, sha256: str) -> Pin:
    return Pin("2.1.296", _CLAUDE.format(platform_), sha256)


def _codex(arch: str, sha256: str) -> Pin:
    return Pin(
        "0.162.1",
        _CODEX.format(arch),
        sha256,
        member=f"codex-{arch}-unknown-linux-musl",
    )


_CODEX_X64 = _codex(
    "x86_64", "86f268d81b898f3e144c5ecff2ad2fda2c5802e042d6195b72538fe0fa125057"
)
_CODEX_ARM64 = _codex(
    "aarch64", "bf3ca2719f1143b1440fbd85c149379e696c782c50ad7991f8bfcec60b2600c1"
)

# (client, platform) -> pin; platform is linux-<arch>[-musl].
PINS: dict[tuple[str, str], Pin] = {
    ("claude", "linux-x64"): _claude(
        "linux-x64", "24972e3bc859fab2b46ed4c1e51f7d6130f06d3bd550811a114640de3370d0de"
    ),
    ("claude", "linux-arm64"): _claude(
        "linux-arm64",
        "f1f6e96e0d8342b9dbf41d7e88255397a6a52ce3d8736ad6a4c6b59c9b62fefa",
    ),
    ("claude", "linux-x64-musl"): _claude(
        "linux-x64-musl",
        "94a1ccc6ffc9b0889c6b2b80e202885c8a575766775377398daa4db5fd89417f",
    ),
    ("claude", "linux-arm64-musl"): _claude(
        "linux-arm64-musl",
        "b0bbc936a82930933d7d5645081050d5f827a946cf6abc6575383b777a0feec0",
    ),
    # Codex ships static musl builds only; they run on either C library.
    ("codex", "linux-x64"): _CODEX_X64,
    ("codex", "linux-x64-musl"): _CODEX_X64,
    ("codex", "linux-arm64"): _CODEX_ARM64,
    ("codex", "linux-arm64-musl"): _CODEX_ARM64,
}


class ClientDownloadError(Exception):
    pass


def _urlopen(url: str) -> BinaryIO:
    return urllib.request.urlopen(url, timeout=60)  # noqa: S310 (pinned https URLs)


# Replaced in tests; never reached when downloads are off.
fetch: Callable[[str], BinaryIO] = _urlopen


def platform_key() -> str | None:
    if platform.system() != "Linux":
        return None
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}
    a = arch.get(platform.machine().lower())
    if a is None:
        return None
    musl = platform.libc_ver()[0] != "glibc"
    return f"linux-{a}" + ("-musl" if musl else "")


def pin(client: str) -> Pin | None:
    key = platform_key()
    return PINS.get((client, key)) if key else None


def _target(settings: Settings, client: str, p: Pin) -> Path:
    return settings.data_dir / "tools" / f"{client}-{p.version}" / client


def supported(settings: Settings, client: str) -> bool:
    """A download would be attempted (or one is already present)."""
    return installed(settings, client) is not None or (
        settings.client_downloads and pin(client) is not None
    )


def installed(settings: Settings, client: str) -> Path | None:
    """The verified client for the current pin, if present. Never downloads."""
    p = pin(client)
    if p is None:
        return None
    path = _target(settings, client, p)
    return path if path.is_file() and os.access(path, os.X_OK) else None


_locks: dict[tuple[int, str], asyncio.Lock] = {}


def _lock(client: str) -> asyncio.Lock:
    key = (id(asyncio.get_running_loop()), client)
    return _locks.setdefault(key, asyncio.Lock())


async def ensure(settings: Settings, client: str) -> Path:
    """The client's path, downloading and verifying it first if needed."""
    if path := installed(settings, client):
        return path
    p = pin(client)
    if p is None:
        raise ClientDownloadError(f"no {client} build for this platform")
    if not settings.client_downloads:
        raise ClientDownloadError("client downloads are off (ARGUS_CLIENT_DOWNLOADS=0)")
    async with _lock(client):
        if path := installed(settings, client):
            return path
        try:
            return await asyncio.to_thread(_install, settings, client, p)
        except (OSError, tarfile.TarError, KeyError) as exc:
            raise ClientDownloadError(f"download failed: {exc}") from exc


def _install(settings: Settings, client: str, p: Pin) -> Path:
    tools = settings.data_dir / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    target = _target(settings, client, p)
    fd, tmp = tempfile.mkstemp(dir=tools, prefix=f".{client}-")
    binary = tmp + ".bin"
    try:
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "wb") as out, fetch(p.url) as resp:
            while chunk := resp.read(1 << 20):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ClientDownloadError("the download is larger than expected")
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != p.sha256:
            raise ClientDownloadError("the download does not match its pinned checksum")
        if p.member is None:
            os.replace(tmp, binary)
        else:
            with tarfile.open(tmp, "r:gz") as tar:
                member = tar.getmember(p.member)
                src = tar.extractfile(member)
                if src is None or member.size > MAX_BYTES:
                    raise ClientDownloadError(f"unexpected {p.member} in the archive")
                with src, open(binary, "wb") as out:
                    shutil.copyfileobj(src, out)
        os.chmod(binary, 0o700)  # run only by the Argus user, which owns it
        target.parent.mkdir(exist_ok=True)
        os.replace(binary, target)
    finally:
        for leftover in (tmp, binary):
            Path(leftover).unlink(missing_ok=True)
    for old in tools.glob(f"{client}-*"):
        if old.is_dir() and old != target.parent:
            shutil.rmtree(old, ignore_errors=True)
    return target
