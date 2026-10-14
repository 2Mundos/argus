"""Provider credentials in one SOPS-encrypted file (worker lifecycle 3.9.1).

Argus decrypts the file into memory at startup with the age identity sops finds
(`SOPS_AGE_KEY_FILE` / `SOPS_AGE_KEY`). It is the only writer: every change is
applied with `sops set|unset` to a temporary copy in the same directory, which
is fsynced and renamed over the original, so a crash never leaves a half-written
file and plaintext never touches the disk. Values go to sops over stdin, never
on the command line.
"""

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from argus_api.errors import DomainError

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SOPS_REF_PREFIX = "sops:"


class SecretStoreError(DomainError):
    status = 503
    code = "secret_store_unavailable"


class SecretStore(Protocol):
    def get(self, name: str) -> dict[str, Any] | None: ...
    def names(self) -> set[str]: ...
    async def put(self, name: str, value: dict[str, Any]) -> None: ...
    async def delete(self, name: str) -> None: ...
    async def reload(self) -> None: ...


def ref_name(secret_ref: str) -> str | None:
    """`sops:<name>` → `<name>`; anything else is not a ref this store resolves."""
    if secret_ref.startswith(SOPS_REF_PREFIX):
        return secret_ref.removeprefix(SOPS_REF_PREFIX)
    return None


def _check(name: str) -> None:
    if not _NAME.match(name):
        raise ValueError(f"invalid secret name: {name!r}")


class MemorySecretStore:
    """For tests and for running without a secrets file."""

    def __init__(self, data: dict[str, dict[str, Any]] | None = None) -> None:
        self._data = dict(data or {})

    def get(self, name: str) -> dict[str, Any] | None:
        return self._data.get(name)

    def names(self) -> set[str]:
        return set(self._data)

    async def put(self, name: str, value: dict[str, Any]) -> None:
        _check(name)
        self._data[name] = value

    async def delete(self, name: str) -> None:
        self._data.pop(name, None)

    async def reload(self) -> None:
        return None


class SopsSecretStore:
    def __init__(self, path: Path, sops: str = "sops") -> None:
        self.path = path
        self.sops = sops
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def get(self, name: str) -> dict[str, Any] | None:
        return self._data.get(name)

    def names(self) -> set[str]:
        return set(self._data)

    async def reload(self) -> None:
        """Re-read the file (after an operator edited it with `sops`)."""
        async with self._lock:
            await self._load()

    async def put(self, name: str, value: dict[str, Any]) -> None:
        _check(name)
        async with self._lock:
            if not self.path.exists():
                await self._create()
            await self._edit(["set", "--value-stdin"], name, json.dumps(value))
            self._data[name] = value

    async def delete(self, name: str) -> None:
        _check(name)
        async with self._lock:
            if name not in self._data:
                return
            await self._edit(["unset"], name, None)
            self._data.pop(name, None)

    async def _load(self) -> None:
        if not self.path.exists():
            self._data = {}
            return
        out = await self._run(["decrypt", "--output-type", "json", str(self.path)])
        data = json.loads(out)
        data.pop("sops", None)
        self._data = {k: v for k, v in data.items() if isinstance(v, dict)}

    async def _create(self) -> None:
        if not os.environ.get("SOPS_AGE_RECIPIENTS"):
            raise SecretStoreError(
                f"{self.path} does not exist yet; set SOPS_AGE_RECIPIENTS to the age"
                " public key(s) to encrypt it to",
                code="secret_store_not_configured",
            )
        # The new file starts empty, so there is nothing secret in this
        # temporary plaintext; values are added afterwards via stdin.
        empty = self.path.with_name(f".{uuid4().hex}.empty.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        empty.write_text("{}")
        try:
            encrypted = await self._run(
                [
                    "encrypt",
                    "--input-type",
                    "json",
                    "--output-type",
                    "yaml",
                    "--filename-override",
                    str(self.path),
                    str(empty),
                ]
            )
        finally:
            empty.unlink(missing_ok=True)
        self._replace_with(encrypted)

    async def _edit(self, args: list[str], name: str, stdin: str | None) -> None:
        tmp = self.path.with_name(f".{uuid4().hex}.{self.path.name}")
        await asyncio.to_thread(shutil.copy2, self.path, tmp)
        try:
            await self._run([*args, str(tmp), f"[{json.dumps(name)}]"], stdin=stdin)
            await asyncio.to_thread(_fsync_and_replace, tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def _replace_with(self, content: str) -> None:
        tmp = self.path.with_name(f".{uuid4().hex}.{self.path.name}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    async def _run(self, args: list[str], stdin: str | None = None) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.sops,
                *args,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise SecretStoreError(f"sops binary not found: {self.sops}") from exc
        out, err = await proc.communicate(stdin.encode() if stdin is not None else None)
        if proc.returncode != 0:
            # sops errors name keys and paths, never decrypted values.
            raise SecretStoreError(
                f"sops {args[0]} failed: {err.decode().strip()[:500]}"
            )
        return out.decode()


def _fsync_and_replace(tmp: Path, target: Path) -> None:
    with open(tmp, "rb+") as f:
        os.fsync(f.fileno())
    os.replace(tmp, target)


async def open_store(path: Path, sops: str = "sops") -> tuple[SecretStore, str | None]:
    """Load the store; a broken store is reported, never fatal (single-node 3.5)."""
    store = SopsSecretStore(path, sops)
    try:
        await store.reload()
    except SecretStoreError as exc:
        return store, exc.message
    return store, None
