import codecs
import hashlib
import re
from collections.abc import AsyncIterator
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

import aiosqlite
import anyio
from pydantic import BaseModel

from argus_api.artifacts.store import LocalArtifactStore
from argus_api.db import Database, ts
from argus_api.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationFailedError,
)
from argus_api.events import record
from argus_api.tasks import service as tasks

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class ArtifactRole(StrEnum):
    """What an artifact is for, declared by its uploader (MCP operations 3.2)."""

    REPORT = "report"  # the worker's own report
    DELEGATE_REPORT = "delegate_report"  # a delegated executor's original report
    POLICY_SNAPSHOT = "policy_snapshot"
    QA_RECEIPT = "qa_receipt"  # proof of a QA or delivery step
    OTHER = "other"


class ArtifactSource(StrEnum):
    WORKER = "worker"  # uploaded by the worker holding the attempt
    ATTACHMENT = "attachment"  # attached by an orchestrator or admin


# Attachments hang off the task, not an attempt; this makes UNIQUE (task_id, attempt,
# name) refuse duplicate names and keeps the storage key shape.
ATTACHMENT_ATTEMPT = 0


class Artifact(BaseModel):
    id: str
    task_id: str
    attempt: int
    name: str
    media_type: str
    size: int
    sha256: str
    created_by: str
    created_at: datetime
    role: ArtifactRole | None = None
    source: ArtifactSource = ArtifactSource.WORKER
    attached_by: str | None = None  # uploader of an attachment; never `agent:…`
    worker_readable: bool = False  # an attachment shared with the task's lease holders


class TaskArtifact(Artifact):
    in_result: bool  # the task's result lists it


class ArtifactText(BaseModel):
    """A byte range of an artifact, decoded as UTF-8 when it is text."""

    artifact: Artifact
    offset: int
    length: int  # bytes covered by `text`; a split character waits for the next read
    next_offset: int | None  # None: the end was reached
    binary: bool  # not a text or JSON media type: no bytes are returned
    text: str | None


class Verification(BaseModel):
    """Whether the stored bytes are the ones uploaded. Says nothing about whether
    the content is acceptable."""

    ok: bool
    size: int | None  # None: the file is missing
    sha256: str | None
    expected_size: int
    expected_sha256: str


def key(task_id: str, attempt: int, name: str) -> str:
    return f"tasks/{task_id}/{attempt}/{name}"


async def agent_bytes_used(conn: aiosqlite.Connection, agent_id: str) -> int:
    async with conn.execute(
        "SELECT coalesce(sum(size), 0) FROM artifacts WHERE created_by = ?",
        (f"agent:{agent_id}",),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


_RFC_TCHARS = r"[A-Za-z0-9!#$%&'*+.^_`|~-]+"
_CONCRETE = r"[A-Za-z0-9!#$%&'+.^_`|~-]+"  # no '*': wildcard types are not concrete
_OWS = r"[ \t]*"
# Tab is allowed only as OWS, never inside a quoted-string.
_QUOTED = r'"(?:[\x20\x21\x23-\x5b\x5d-\x7e]|\\[\x20-\x7e])*"'
_MEDIA_TYPE = re.compile(
    rf"{_CONCRETE}/{_CONCRETE}"
    rf"(?:{_OWS};{_OWS}{_RFC_TCHARS}=(?:{_RFC_TCHARS}|{_QUOTED}))*"
)


def check_media_type(media_type: str) -> str:
    """A concrete `type/subtype[; param=value]` media type of at most 255 characters."""
    if len(media_type) > 255 or not _MEDIA_TYPE.fullmatch(media_type):
        raise ValidationFailedError(
            "Content-Type must be a type/subtype media type of at most 255 characters",
            code="invalid_media_type",
        )
    return media_type


def _check_name(name: str) -> None:
    if not _NAME.fullmatch(name):
        raise ValidationFailedError(
            "artifact name must be 1-200 characters of letters, digits, '.', '_', '-'"
            " and must not start with '.', '_' or '-'",
            code="invalid_name",
        )


def check_integrity(
    size: int, sha256: str, expected_size: int | None, expected_sha256: str | None
) -> None:
    """422 `integrity_mismatch` when a claimed size or hash is not the real one.
    The message names no content."""
    if expected_size is not None and expected_size != size:
        raise ValidationFailedError(
            f"size is {size} bytes, expected {expected_size}",
            code="integrity_mismatch",
        )
    if expected_sha256 is not None and expected_sha256 != sha256:
        raise ValidationFailedError(
            f"sha256 is {sha256}, expected {expected_sha256}",
            code="integrity_mismatch",
        )


async def attach(
    db: Database,
    store: LocalArtifactStore,
    *,
    attached_by: str,
    task_id: str,
    name: str,
    media_type: str,
    chunks: AsyncIterator[bytes],
    role: ArtifactRole | None = None,
    max_per_task: int,
    worker_readable: bool = False,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> Artifact:
    """Attach evidence to a task for a manager: no lease, no agent quota. Same
    staging as `upload`; the staged file is discarded on any error, including a
    size or SHA-256 that differs from what the uploader expected (task inputs 3.1)."""
    _check_name(name)
    staged = await store.stage(chunks)
    promoted: Path | None = None
    try:
        check_integrity(staged.size, staged.sha256, expected_size, expected_sha256)
        async with db.write() as tx:
            await tasks.get_task(tx.conn, task_id)  # 404 for unknown tasks
            async with tx.conn.execute(
                "SELECT count(*) FROM artifacts WHERE task_id = ? AND source = ?",
                (task_id, ArtifactSource.ATTACHMENT),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            if row[0] >= max_per_task:
                raise DomainError(
                    f"task already has {row[0]} attachments (limit {max_per_task})",
                    code="attachment_limit",
                )
            async with tx.conn.execute(
                "SELECT 1 FROM artifacts WHERE task_id = ? AND attempt = ? AND name = ?",
                (task_id, ATTACHMENT_ATTEMPT, name),
            ) as cur:
                if await cur.fetchone() is not None:
                    raise ConflictError(
                        f"attachment {name!r} already exists on this task;"
                        " attachments are immutable, use another name",
                        code="artifact_exists",
                    )
            artifact_id = str(uuid4())
            await tx.conn.execute(
                "INSERT INTO artifacts (id, task_id, attempt, name, media_type, size,"
                " sha256, created_by, created_at, role, source, attached_by,"
                " worker_readable) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact_id,
                    task_id,
                    ATTACHMENT_ATTEMPT,
                    name,
                    media_type,
                    staged.size,
                    staged.sha256,
                    attached_by,
                    ts(tx.now),
                    role,
                    ArtifactSource.ATTACHMENT,
                    attached_by,
                    int(worker_readable),
                ),
            )
            await record(
                tx,
                attached_by,
                "task",
                task_id,
                "task.attachment",
                # Workers never see this event; it names no file either way.
                {"source": "attachment", "worker_readable": worker_readable},
            )
            store.promote(staged, key(task_id, ATTACHMENT_ATTEMPT, name))
            promoted = store.path(key(task_id, ATTACHMENT_ATTEMPT, name))
    except BaseException:
        store.discard(staged)
        if promoted is not None:  # the commit failed: drop the file with no row
            promoted.unlink(missing_ok=True)
        raise
    async with db.read() as conn:
        return await get_artifact(conn, artifact_id)


async def upload(
    db: Database,
    store: LocalArtifactStore,
    *,
    agent_id: str,
    task_id: str,
    attempt: int,
    name: str,
    media_type: str,
    chunks: AsyncIterator[bytes],
    role: ArtifactRole | None = None,
    agent_quota_bytes: int | None = None,
) -> Artifact:
    """Stage the bytes first (no lock held), then check the lease and commit."""
    _check_name(name)
    if agent_quota_bytes is not None:
        async with db.read() as conn:
            used = await agent_bytes_used(conn, agent_id)
        if used >= agent_quota_bytes:
            raise DomainError(
                f"agent artifact quota of {agent_quota_bytes} bytes would be exceeded"
                f" (already using {used})",
                code="artifact_quota",
            )
    else:
        used = 0
    staged = await store.stage(chunks)
    promoted: Path | None = None
    try:
        if agent_quota_bytes is not None and used + staged.size > agent_quota_bytes:
            raise DomainError(
                f"agent artifact quota of {agent_quota_bytes} bytes would be exceeded"
                f" (using {used}, upload {staged.size})",
                code="artifact_quota",
            )
        async with db.write() as tx:
            await tasks.check_lease(tx, task_id, attempt, agent_id)
            async with tx.conn.execute(
                "SELECT 1 FROM artifacts WHERE task_id = ? AND attempt = ? AND name = ?",
                (task_id, attempt, name),
            ) as cur:
                if await cur.fetchone() is not None:
                    raise ConflictError(
                        f"artifact {name!r} already exists for attempt {attempt};"
                        " artifacts are immutable, use another name",
                        code="artifact_exists",
                    )
            artifact_id = str(uuid4())
            await tx.conn.execute(
                "INSERT INTO artifacts (id, task_id, attempt, name, media_type, size,"
                " sha256, created_by, created_at, role)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact_id,
                    task_id,
                    attempt,
                    name,
                    media_type,
                    staged.size,
                    staged.sha256,
                    f"agent:{agent_id}",
                    ts(tx.now),
                    role,
                ),
            )
            await record(
                tx,
                f"agent:{agent_id}",
                "task",
                task_id,
                "task.artifact",
                {"artifact_id": artifact_id, "name": name, "size": staged.size},
            )
            # Last step inside the transaction: if the rename fails, the row rolls
            # back; if the commit fails afterwards, the final file is removed below.
            store.promote(staged, key(task_id, attempt, name))
            promoted = store.path(key(task_id, attempt, name))
    except BaseException:
        store.discard(staged)
        if promoted is not None:
            promoted.unlink(missing_ok=True)
        raise
    async with db.read() as conn:
        return await get_artifact(conn, artifact_id)


async def get_artifact(conn: aiosqlite.Connection, artifact_id: str) -> Artifact:
    async with conn.execute(
        "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"artifact not found: {artifact_id}")
    return Artifact.model_validate(dict(row))


async def list_artifacts(
    conn: aiosqlite.Connection,
    task_id: str,
    *,
    attempt: int | None = None,
    role: ArtifactRole | None = None,
    include_attachments: bool = False,
    include_shared: bool = False,
) -> list[TaskArtifact]:
    """`include_attachments`: every attachment (managers); `include_shared`: only
    those attached with `worker_readable` (workers leased on the task)."""
    task = await tasks.get_task(conn, task_id)  # 404 for unknown tasks
    listed = set(task.result.artifacts) if task.result is not None else set()
    sql = "SELECT * FROM artifacts WHERE task_id = ?"
    params: list[str | int] = [task_id]
    if include_shared and not include_attachments:
        sql += " AND (source = 'worker' OR worker_readable = 1)"
    elif not include_attachments:
        sql += " AND source = 'worker'"
    if attempt is not None:
        sql += " AND attempt = ?"
        params.append(attempt)
    if role is not None:
        sql += " AND role = ?"
        params.append(role)
    async with conn.execute(sql + " ORDER BY attempt, name", params) as cur:
        return [
            TaskArtifact.model_validate({**dict(r), "in_result": r["id"] in listed})
            for r in await cur.fetchall()
        ]


def is_text(media_type: str) -> bool:
    base = media_type.split(";", 1)[0].strip().lower()
    return base.startswith("text/") or base in _JSON or base.endswith("+json")


_JSON = {"application/json", "application/x-ndjson"}


async def read_text(
    store: LocalArtifactStore, artifact: Artifact, offset: int, max_bytes: int
) -> ArtifactText:
    """Read up to `max_bytes` from `offset`, never splitting a UTF-8 character."""
    if not is_text(artifact.media_type):
        return ArtifactText(
            artifact=artifact,
            offset=offset,
            length=0,
            next_offset=None,
            binary=True,
            text=None,
        )
    async with await anyio.open_file(_path(store, artifact), "rb") as f:
        await f.seek(offset)
        chunk = await f.read(max_bytes)
    end = offset + len(chunk)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = decoder.decode(chunk, final=end >= artifact.size)
    length = len(chunk) - len(decoder.getstate()[0])
    nxt = offset + length
    return ArtifactText(
        artifact=artifact,
        offset=offset,
        length=length,
        next_offset=nxt if nxt < artifact.size else None,
        binary=False,
        text=text,
    )


async def verify(store: LocalArtifactStore, artifact: Artifact) -> Verification:
    """Re-hash the stored file, outside any transaction."""
    expected = {"expected_size": artifact.size, "expected_sha256": artifact.sha256}
    path = anyio.Path(_path(store, artifact))
    if not await path.is_file():
        return Verification(ok=False, size=None, sha256=None, **expected)
    digest, size = hashlib.sha256(), 0
    async with await anyio.open_file(path, "rb") as f:
        while chunk := await f.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    sha256 = digest.hexdigest()
    ok = size == artifact.size and sha256 == artifact.sha256
    return Verification(ok=ok, size=size, sha256=sha256, **expected)


def _path(store: LocalArtifactStore, artifact: Artifact) -> Path:
    return store.path(key(artifact.task_id, artifact.attempt, artifact.name))
