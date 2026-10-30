"""Task inputs: shared attachments as a task's inputs, and their receipts
(docs/specs/task-inputs.spec.md 3.2, 3.3).

Receipts are `task.input` events, not a table. They carry ids, the attempt, the
state and a reason code; never a name, a hash or content.
"""

import json
from datetime import datetime
from enum import StrEnum

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from argus_api.access import has_leased_task
from argus_api.artifacts.service import ArtifactSource, check_integrity
from argus_api.db import Tx
from argus_api.errors import ForbiddenError, NotFoundError
from argus_api.events import record
from argus_api.tasks import service as tasks

SHA256 = r"^[0-9a-f]{64}$"


class ReceiptState(StrEnum):
    MATERIALIZED = "materialized"  # a channel wrote the file into the task directory
    VERIFIED = "verified"  # that channel re-read it: size and SHA-256 match
    CONSUMED = "consumed"  # the in-session verifier passed and the model was told
    FAILED = "failed"


class InputFailure(StrEnum):
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    LIMIT = "limit"
    CORRUPT = "corrupt"


class ReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt: int
    state: ReceiptState
    sha256: str | None = Field(default=None, pattern=SHA256)
    size: int | None = Field(default=None, ge=0)
    reason: InputFailure | None = None

    @model_validator(mode="after")
    def _shape(self) -> "ReceiptRequest":
        if self.state in (ReceiptState.VERIFIED, ReceiptState.CONSUMED) and (
            self.sha256 is None or self.size is None
        ):
            raise ValueError(f"a {self.state} receipt needs sha256 and size")
        if (self.state is ReceiptState.FAILED) != (self.reason is not None):
            raise ValueError("a failed receipt needs a reason; other states take none")
        return self


class InputReceipt(BaseModel):
    """The latest receipt of one state in one attempt."""

    attempt: int
    state: ReceiptState
    reason: InputFailure | None = None
    at: datetime
    reporter: str


class TaskInput(BaseModel):
    """A shared attachment as an input: the artifact's authoritative metadata."""

    id: str
    task_id: str
    name: str
    media_type: str
    size: int
    sha256: str
    attached_by: str | None
    created_at: datetime
    receipts: list[InputReceipt]


class TaskInputs(BaseModel):
    task_id: str
    inputs: list[TaskInput]
    # Attachments not shared with workers; managers only (null for workers).
    unshared_attachments: int | None = None


_ORDER = {s: i for i, s in enumerate(ReceiptState)}


async def _receipts(
    conn: aiosqlite.Connection, task_id: str
) -> dict[str, list[InputReceipt]]:
    """Per artifact, the latest receipt per (attempt, state), by attempt then state."""
    latest: dict[str, dict[tuple[int, ReceiptState], InputReceipt]] = {}
    async with conn.execute(
        "SELECT at, actor, data FROM events WHERE subject_type = 'task'"
        " AND subject_id = ? AND kind = 'task.input' ORDER BY id",
        (task_id,),
    ) as cur:
        for row in await cur.fetchall():
            data = json.loads(row["data"])
            receipt = InputReceipt(
                attempt=data["attempt"],
                state=data["state"],
                reason=data.get("reason"),
                at=row["at"],
                reporter=row["actor"],
            )
            by = latest.setdefault(data["artifact_id"], {})
            by[(receipt.attempt, receipt.state)] = receipt
    return {
        artifact_id: [by[k] for k in sorted(by, key=lambda k: (k[0], _ORDER[k[1]]))]
        for artifact_id, by in latest.items()
    }


def _input(row: aiosqlite.Row, receipts: dict[str, list[InputReceipt]]) -> TaskInput:
    return TaskInput.model_validate(
        {**dict(row), "receipts": receipts.get(row["id"], [])}
    )


async def list_inputs(
    conn: aiosqlite.Connection, task_id: str, *, manager: bool
) -> TaskInputs:
    """The task's shared attachments with their receipts. The caller already passed
    the task read rule; `manager` adds the count of unshared attachments."""
    await tasks.get_task(conn, task_id)  # 404 for unknown tasks
    async with conn.execute(
        "SELECT * FROM artifacts WHERE task_id = ? AND source = ?"
        " AND worker_readable = 1 ORDER BY created_at, name",
        (task_id, ArtifactSource.ATTACHMENT),
    ) as cur:
        rows = await cur.fetchall()
    receipts = await _receipts(conn, task_id)
    unshared = None
    if manager:
        async with conn.execute(
            "SELECT count(*) FROM artifacts WHERE task_id = ? AND source = ?"
            " AND worker_readable = 0",
            (task_id, ArtifactSource.ATTACHMENT),
        ) as cur:
            count = await cur.fetchone()
        assert count is not None
        unshared = int(count[0])
    return TaskInputs(
        task_id=task_id,
        inputs=[_input(r, receipts) for r in rows],
        unshared_attachments=unshared,
    )


async def shared_input(
    conn: aiosqlite.Connection, task_id: str, artifact_id: str
) -> aiosqlite.Row:
    """The artifact when it is a shared attachment of this very task; else 404."""
    async with conn.execute(
        "SELECT * FROM artifacts WHERE id = ? AND task_id = ? AND source = ?"
        " AND worker_readable = 1",
        (artifact_id, task_id, ArtifactSource.ATTACHMENT),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(
            f"artifact {artifact_id} is not a shared input of task {task_id}",
            code="input_not_found",
        )
    return row


async def record_receipt(
    tx: Tx, agent_id: str, task_id: str, artifact_id: str, body: ReceiptRequest
) -> TaskInput:
    """A worker write fenced like progress: the caller must have leased the task
    (403 `not_leased`) and hold this attempt (409 `stale_attempt`, ...). An identical
    latest receipt is not written again."""
    await tasks.get_task(tx.conn, task_id)  # 404 for unknown tasks
    if not await has_leased_task(tx.conn, agent_id, task_id):
        raise ForbiddenError(
            "workers may only report inputs of tasks they have leased",
            code="not_leased",
        )
    await tasks.check_lease(tx, task_id, body.attempt, agent_id)
    row = await shared_input(tx.conn, task_id, artifact_id)
    check_integrity(row["size"], row["sha256"], body.size, body.sha256)
    have = {
        (r.attempt, r.state): r
        for r in (await _receipts(tx.conn, task_id)).get(artifact_id, [])
    }
    last = have.get((body.attempt, body.state))
    if last is None or last.reason != body.reason:
        data: dict[str, str | int] = {
            "artifact_id": artifact_id,
            "attempt": body.attempt,
            "state": body.state,
        }
        if body.reason is not None:
            data["reason"] = body.reason
        await record(tx, f"agent:{agent_id}", "task", task_id, "task.input", data)
    return _input(row, await _receipts(tx.conn, task_id))
