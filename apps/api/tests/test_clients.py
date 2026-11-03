"""Pinned client downloads (login clients spec 3.2-3.3), against a fake fetch."""

import asyncio
import hashlib
import io
import tarfile
import threading
from pathlib import Path

import pytest
from argus_api.accounts import clients
from argus_api.accounts.clients import ClientDownloadError, Pin
from argus_api.config import Settings

BINARY = b"#!/bin/sh\necho client\n"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tgz(member: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(member)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class Fetch:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.urls: list[str] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def __call__(self, url: str) -> io.BytesIO:
        self.urls.append(url)
        self.started.set()
        self.release.wait()
        return io.BytesIO(self.body)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, client_downloads=True)


def use(monkeypatch: pytest.MonkeyPatch, p: Pin, body: bytes) -> Fetch:
    fetch = Fetch(body)
    monkeypatch.setattr(clients, "pin", lambda client: p)
    monkeypatch.setattr(clients, "fetch", fetch)
    return fetch


def leftovers(settings: Settings) -> list[str]:
    return sorted(p.name for p in (settings.data_dir / "tools").iterdir())


async def test_downloads_verifies_installs_and_reuses(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = use(monkeypatch, Pin("1.0.0", "https://x/claude", sha(BINARY)), BINARY)
    assert clients.installed(settings, "claude") is None
    path = await clients.ensure(settings, "claude")
    assert path == settings.data_dir / "tools" / "claude-1.0.0" / "claude"
    assert path.read_bytes() == BINARY and path.stat().st_mode & 0o111
    assert await clients.ensure(settings, "claude") == path
    assert fetch.urls == ["https://x/claude"]
    assert clients.installed(settings, "claude") == path
    assert leftovers(settings) == ["claude-1.0.0"]


@pytest.mark.parametrize(
    ("body", "max_bytes", "message"),
    [
        (b"tampered", None, "checksum"),
        (BINARY, 4, "larger than expected"),
    ],
)
async def test_a_bad_download_keeps_nothing(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    max_bytes: int | None,
    message: str,
) -> None:
    use(monkeypatch, Pin("1.0.0", "https://x/claude", sha(BINARY)), body)
    if max_bytes is not None:
        monkeypatch.setattr(clients, "MAX_BYTES", max_bytes)
    with pytest.raises(ClientDownloadError, match=message):
        await clients.ensure(settings, "claude")
    assert leftovers(settings) == []
    assert clients.installed(settings, "claude") is None


async def test_an_archive_yields_its_member(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tgz("codex-x86_64-unknown-linux-musl", BINARY)
    p = Pin(
        "0.1.0", "https://x/codex.tgz", sha(archive), "codex-x86_64-unknown-linux-musl"
    )
    use(monkeypatch, p, archive)
    path = await clients.ensure(settings, "codex")
    assert path.read_bytes() == BINARY
    assert leftovers(settings) == ["codex-0.1.0"]


async def test_concurrent_starts_share_one_download(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = use(monkeypatch, Pin("1.0.0", "https://x/claude", sha(BINARY)), BINARY)
    fetch.release.clear()
    first = asyncio.create_task(clients.ensure(settings, "claude"))
    await asyncio.to_thread(fetch.started.wait)
    second = asyncio.create_task(clients.ensure(settings, "claude"))
    await asyncio.to_thread(lambda: None)  # one loop round: `second` reaches the lock
    fetch.release.set()
    assert await first == await second
    assert len(fetch.urls) == 1


async def test_a_new_pin_removes_the_old_version(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, Pin("1.0.0", "https://x/1", sha(BINARY)), BINARY)
    await clients.ensure(settings, "claude")
    use(monkeypatch, Pin("2.0.0", "https://x/2", sha(BINARY)), BINARY)
    await clients.ensure(settings, "claude")
    assert leftovers(settings) == ["claude-2.0.0"]


async def test_downloads_off_or_no_build_never_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = use(monkeypatch, Pin("1.0.0", "https://x/claude", sha(BINARY)), BINARY)
    off = Settings(data_dir=tmp_path)
    assert not clients.supported(off, "claude")
    with pytest.raises(ClientDownloadError, match="ARGUS_CLIENT_DOWNLOADS"):
        await clients.ensure(off, "claude")
    monkeypatch.setattr(clients, "pin", lambda client: None)
    on = Settings(data_dir=tmp_path, client_downloads=True)
    assert not clients.supported(on, "claude")
    with pytest.raises(ClientDownloadError, match="platform"):
        await clients.ensure(on, "claude")
    assert fetch.urls == []


def test_every_pin_is_https_and_has_a_checksum() -> None:
    for (client, platform_), p in clients.PINS.items():
        assert p.url.startswith("https://"), (client, platform_)
        assert len(p.sha256) == 64 and int(p.sha256, 16) >= 0, (client, platform_)
    assert {c for c, _ in clients.PINS} == {"claude", "codex"}
