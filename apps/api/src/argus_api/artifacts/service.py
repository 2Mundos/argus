import re
from collections.abc import AsyncIterator
from datetime import datetime
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel

from argus_api.artifacts.store import LocalArtifactStore
from argus_api.db import Database, ts
from argus_api.errors import ConflictError, NotFoundError, ValidationFailedError
from argus_api.events import record
from argus_api.tasks import service as tasks

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


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


def key(task_id: str, attempt: int, name: str) -> str:
    return f"tasks/{task_id}/{attempt}/{name}"


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
) -> Artifact:
    """Stage the bytes first (no lock held), then check the lease and commit."""
    if not _NAME.match(name):
        raise ValidationFailedError(
            "artifact name must be 1-200 characters of letters, digits, '.', '_', '-'"
            " and must not start with '.', '_' or '-'",
            code="invalid_name",
        )
    staged = await store.stage(chunks)
    try:
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
                " sha256, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            # back; if the commit fails afterwards, an orphan file is harmless.
            store.promote(staged, key(task_id, attempt, name))
    except BaseException:
        store.discard(staged)
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


async def list_artifacts(conn: aiosqlite.Connection, task_id: str) -> list[Artifact]:
    await tasks.get_task(conn, task_id)  # 404 for unknown tasks
    async with conn.execute(
        "SELECT * FROM artifacts WHERE task_id = ? ORDER BY attempt, name", (task_id,)
    ) as cur:
        return [Artifact.model_validate(dict(r)) for r in await cur.fetchall()]
