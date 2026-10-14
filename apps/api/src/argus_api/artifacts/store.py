"""Artifact bytes on the local filesystem (single-node 3.4).

Uploads stream into a temporary file first, outside any write transaction; the
caller then commits metadata and `promote()`s the file in one short transaction.
An S3-compatible store can implement the same three operations later.
"""

import hashlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import anyio

from argus_api.errors import TooLargeError


@dataclass(frozen=True)
class StagedFile:
    path: Path
    size: int
    sha256: str


class LocalArtifactStore:
    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self._staging = root / ".staging"

    async def stage(self, chunks: AsyncIterator[bytes]) -> StagedFile:
        """Write an upload to a temporary file, enforcing the size limit."""
        await anyio.Path(self._staging).mkdir(parents=True, exist_ok=True)
        path = self._staging / str(uuid4())
        digest, size = hashlib.sha256(), 0
        try:
            async with await anyio.open_file(path, "wb") as f:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise TooLargeError(
                            f"artifact exceeds the {self.max_bytes}-byte limit"
                        )
                    digest.update(chunk)
                    await f.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return StagedFile(path, size, digest.hexdigest())

    def promote(self, staged: StagedFile, key: str) -> None:
        """Move a staged file to its final key (same filesystem: atomic rename)."""
        final = self.path(key)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged.path, final)

    def discard(self, staged: StagedFile) -> None:
        staged.path.unlink(missing_ok=True)

    def path(self, key: str) -> Path:
        return self.root / key
