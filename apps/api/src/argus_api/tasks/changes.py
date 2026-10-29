"""Diff snapshot of a task attempt's workspace (docs/specs/task-changes.spec.md)."""

import json
from datetime import datetime
from typing import Annotated, Literal

import aiosqlite
from argus_core.redact import redact, redact_private_keys
from pydantic import BaseModel, Field, StringConstraints

from argus_api.db import Snapshot, Tx, ts
from argus_api.errors import NotFoundError, RateLimitedError, TooLargeError
from argus_api.events import transient
from argus_api.tasks import service
from argus_api.tasks.log import _CONTROL

MAX_FILES = 500
MAX_UNTRACKED = 200
MAX_COMMITS = 50
MAX_PATCH_BYTES = 256 * 1024  # UTF-8; a longer patch is cut at a line
MAX_BODY_BYTES = 384 * 1024  # the snapshot as received, serialized
MIN_INTERVAL_S = 5.0  # per task; the host puts at most one per 10 s
KEEP_ATTEMPTS = 3  # the current attempt and the two before it
CUT_MARK = "… (cut)\n"

Sha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{4,64}$")]
Path = Annotated[str, StringConstraints(min_length=1, max_length=4096)]


class BaseRef(BaseModel):
    ref: str = Field(min_length=1, max_length=200)
    sha: Sha


class Commit(BaseModel):
    sha: Sha
    subject: str = Field(max_length=1000)


class ChangedFile(BaseModel):
    path: Path
    # git's --name-status letters
    status: Literal["A", "M", "D", "R", "C", "T", "U", "X"]
    added: int | None = Field(default=None, ge=0)  # None for a binary file
    deleted: int | None = Field(default=None, ge=0)
    binary: bool = False


class UntrackedFile(BaseModel):
    path: Path
    bytes: int = Field(ge=0)


class Truncated(BaseModel):
    files: bool = False
    patch: bool = False


class ChangesBody(BaseModel):
    """What the worker computed in the container, for one attempt."""

    at: datetime  # when the worker took the snapshot
    branch: str = Field(min_length=1, max_length=255)
    base: BaseRef | None = None  # None: diffed against the empty tree
    head: Sha | None = None  # None: unborn branch
    commits: list[Commit] = Field(default_factory=list, max_length=MAX_COMMITS)
    files: list[ChangedFile] = Field(default_factory=list, max_length=MAX_FILES)
    untracked: list[UntrackedFile] = Field(
        default_factory=list, max_length=MAX_UNTRACKED
    )
    patch: str = ""
    truncated: Truncated = Field(default_factory=Truncated)


class ChangesWrite(ChangesBody):
    attempt: int


class Changes(ChangesBody):
    task_id: str
    attempt: int
    received_at: datetime


class ChangesAccepted(BaseModel):
    at: datetime
    files: int
    added: int
    deleted: int


def _clean(text: str, secrets: tuple[str, ...]) -> str:
    return redact(redact_private_keys(_CONTROL.sub("", text)), secrets)


def cut_patch(patch: str) -> tuple[str, bool]:
    """`patch` within MAX_PATCH_BYTES, cut after a whole line, and whether it was cut."""
    raw = patch.encode()
    if len(raw) <= MAX_PATCH_BYTES:
        return patch, False
    room = MAX_PATCH_BYTES - len(CUT_MARK.encode())
    head = raw[:room].decode(errors="ignore")
    end = head.rfind("\n")
    return (head[: end + 1] if end >= 0 else "") + CUT_MARK, True


def prepare(body: ChangesWrite, secrets: tuple[str, ...] = ()) -> ChangesWrite:
    """Check the size and redact every string: the CPU-bound part of `store`, done
    before taking the writer. The worker redacts too; this pass is authoritative."""
    if len(body.model_dump_json().encode()) > MAX_BODY_BYTES:
        raise TooLargeError(
            f"a changes snapshot is at most {MAX_BODY_BYTES} bytes",
            code="changes_too_large",
        )
    patch, cut = cut_patch(_clean(body.patch, secrets))
    return body.model_copy(
        update={
            "branch": _clean(body.branch, secrets),
            "base": body.base
            and body.base.model_copy(update={"ref": _clean(body.base.ref, secrets)}),
            "commits": [
                c.model_copy(update={"subject": _clean(c.subject, secrets)})
                for c in body.commits
            ],
            "files": [
                f.model_copy(update={"path": _clean(f.path, secrets)})
                for f in body.files
            ],
            "untracked": [
                u.model_copy(update={"path": _clean(u.path, secrets)})
                for u in body.untracked
            ],
            "patch": patch,
            "truncated": body.truncated.model_copy(
                update={"patch": body.truncated.patch or cut}
            ),
        }
    )


async def admit(
    tx: Tx | Snapshot, agent_id: str, task_id: str, attempt: int
) -> aiosqlite.Row:
    """The lease and rate checks of `store`, without writing. The route runs them on a
    read snapshot first, so a refused snapshot costs no redaction; `store` repeats them
    in the writer, where they are authoritative."""
    row = await service.check_lease(tx, task_id, attempt, agent_id)
    async with tx.conn.execute(
        "SELECT received_at FROM task_changes WHERE task_id = ? AND attempt = ?",
        (task_id, attempt),
    ) as cur:
        last = await cur.fetchone()
    if last is not None:
        wait = (
            MIN_INTERVAL_S
            - (tx.now - datetime.fromisoformat(last["received_at"])).total_seconds()
        )
        if wait > 0:
            raise RateLimitedError(
                f"at most one changes snapshot per {MIN_INTERVAL_S:g} s;"
                f" retry in {wait:.1f} s"
            )
    return row


async def store(
    tx: Tx, agent_id: str, task_id: str, body: ChangesWrite
) -> ChangesAccepted:
    """Replace the attempt's snapshot (already through `prepare`) for the attempt the
    caller holds the live lease on, then publish its summary as a transient
    `task.changes` event. Does not renew the lease."""
    await admit(tx, agent_id, task_id, body.attempt)
    dump = body.model_dump(mode="json")
    await tx.conn.execute(
        "INSERT OR REPLACE INTO task_changes (task_id, attempt, at, received_at,"
        " branch, base_ref, base_sha, head, commits, files, untracked, patch,"
        " truncated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            body.attempt,
            ts(body.at),
            ts(tx.now),
            body.branch,
            body.base.ref if body.base else None,
            body.base.sha if body.base else None,
            body.head,
            json.dumps(dump["commits"]),
            json.dumps(dump["files"]),
            json.dumps(dump["untracked"]),
            body.patch,
            json.dumps(dump["truncated"]),
        ),
    )
    await tx.conn.execute(
        "DELETE FROM task_changes WHERE task_id = ? AND attempt <= ?",
        (task_id, body.attempt - KEEP_ATTEMPTS),
    )
    added = sum(f.added or 0 for f in body.files)
    deleted = sum(f.deleted or 0 for f in body.files)
    transient(
        tx,
        f"agent:{agent_id}",
        "task",
        task_id,
        "task.changes",
        {
            "task_id": task_id,
            "attempt": body.attempt,
            "at": ts(body.at),
            "head": body.head,
            "files": len(body.files),
            "added": added,
            "deleted": deleted,
        },
    )
    return ChangesAccepted(
        at=body.at, files=len(body.files), added=added, deleted=deleted
    )


async def read(
    conn: aiosqlite.Connection, task_id: str, attempt: int | None
) -> Changes:
    """The stored snapshot of one attempt (default: the task's current one)."""
    async with conn.execute(
        "SELECT attempt FROM tasks WHERE id = ?", (task_id,)
    ) as cur:
        task = await cur.fetchone()
    if task is None:
        raise NotFoundError(f"task not found: {task_id}")
    attempt = task["attempt"] if attempt is None else attempt
    async with conn.execute(
        "SELECT * FROM task_changes WHERE task_id = ? AND attempt = ?",
        (task_id, attempt),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(
            f"no changes for task {task_id} attempt {attempt}", code="no_changes"
        )
    return Changes(
        task_id=task_id,
        attempt=attempt,
        at=row["at"],
        received_at=row["received_at"],
        branch=row["branch"],
        base={"ref": row["base_ref"], "sha": row["base_sha"]}
        if row["base_ref"]
        else None,
        head=row["head"],
        commits=json.loads(row["commits"]),
        files=json.loads(row["files"]),
        untracked=json.loads(row["untracked"]),
        patch=row["patch"],
        truncated=json.loads(row["truncated"]),
    )
