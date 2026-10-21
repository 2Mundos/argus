"""Live log of a task attempt's output (docs/specs/task-live-log.spec.md)."""

import re
from datetime import datetime
from typing import Literal

import aiosqlite
from argus_core.redact import REDACTED, redact, redact_private_keys
from pydantic import BaseModel

from argus_api.db import Snapshot, Tx, ts
from argus_api.errors import NotFoundError, RateLimitedError, TooLargeError
from argus_api.events import transient
from argus_api.tasks import service

MAX_LINES = 200  # per batch
MAX_LINE_CHARS = 2000  # longer lines are cut, with "…"
REDACT_LINE_CHARS = (
    8 * 1024
)  # lines are cut to this before redaction, bounding its cost
MAX_BATCH_BYTES = 64 * 1024  # UTF-8, before cleaning
MIN_INTERVAL_S = 1.0  # per task and source
RING_LINES = 2000  # per attempt
KEEP_ATTEMPTS = 3  # the current attempt and the two before it
DEFAULT_TAIL = 200
MAX_READ = 1000
MAX_SEQ = 2**63 - 1  # SQLite INTEGER: a larger bound would overflow (500, not 422)

Source = Literal["worker", "delegate"]

# a bare \r (progress bars) ends a line too: kept until redaction is done
_CONTROL = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class LogLine(BaseModel):
    seq: int
    source: Source
    at: datetime
    line: str


class LogPage(BaseModel):
    attempt: int
    lines: list[LogLine]  # oldest first
    # of the last line returned, else `after`; an empty tail gives the task's log_seq,
    # so the next line of the attempt (log_seq + 1) is known to follow without a gap
    last_seq: int


class LogAccepted(BaseModel):
    accepted: int  # lines stored (blank lines are dropped)
    last_seq: int  # of the task's log so far
    output_at: datetime | None


def clean(lines: list[str], secrets: tuple[str, ...]) -> list[str]:
    """Strip terminal escapes, redact, split lines (a bare CR too), cut long lines,
    drop blanks.
    Escapes go first, so none can split a secret (`ghp_\x1b[0m…`). Private key blocks
    are masked on the whole batch, so one spanning lines cannot slip through; the
    other rules run on the batch with each line cut to REDACT_LINE_CHARS, before the
    final cut (so a secret about to be cut is still whole), and again after it.
    Redaction runs before a bare CR splits a line, so a CR used as a space
    (`Bearer\r…`) splits no secret; a line whose CR splits a secret mid-token is
    kept whole instead (`_join_split_secret`)."""
    text = _CONTROL.sub("", "\n".join(lines).replace("\r\n", "\n"))
    text = redact_private_keys(text)
    text = "\n".join(
        _join_split_secret(raw[:REDACT_LINE_CHARS], secrets) for raw in text.split("\n")
    )
    out: list[str] = []
    for row in redact(text, secrets).split("\n"):
        for raw in row.split("\r"):
            line = raw.rstrip()
            if not line:
                continue
            if len(line) > MAX_LINE_CHARS:
                line = line[: MAX_LINE_CHARS - 1] + "…"
            out.append(redact(line, secrets))
    return out


def _join_split_secret(row: str, secrets: tuple[str, ...]) -> str:
    """`row` without its bare CRs when that leaves less unmasked (a CR split a secret
    mid-token: `ghp_…\r…`), else as is."""
    if "\r" not in row:
        return row
    joined = row.replace("\r", "")
    shown = lambda s: len(s.replace(REDACTED, ""))  # noqa: E731
    split = sum(shown(p) for p in redact(row, secrets).split("\r"))
    return joined if shown(redact(joined, secrets)) < split else row


def prepare(lines: list[str], secrets: tuple[str, ...] = ()) -> list[str]:
    """Check a batch's size and clean it: the CPU-bound part of `append`, done before
    taking the writer."""
    if sum(len(line.encode()) for line in lines) > MAX_BATCH_BYTES:
        raise TooLargeError(
            f"a log batch is at most {MAX_BATCH_BYTES} bytes",
            code="log_batch_too_large",
        )
    return clean(lines, secrets)


async def admit(
    tx: Tx | Snapshot, agent_id: str, task_id: str, attempt: int, source: Source
) -> aiosqlite.Row:
    """The lease and rate checks of `append`, without writing. The route runs them on
    a read snapshot first, so a refused batch costs no cleaning; `append` repeats them
    in the writer, where they are authoritative."""
    row = await service.check_lease(tx, task_id, attempt, agent_id)
    async with tx.conn.execute(
        "SELECT at FROM task_log WHERE task_id = ? AND attempt = ? AND source = ?"
        " ORDER BY seq DESC LIMIT 1",
        (task_id, attempt, source),
    ) as cur:
        last = await cur.fetchone()
    if last is not None:
        wait = (
            MIN_INTERVAL_S
            - (tx.now - datetime.fromisoformat(last["at"])).total_seconds()
        )
        if wait > 0:
            raise RateLimitedError(
                f"at most one {source} log batch per {MIN_INTERVAL_S:g} s;"
                f" retry in {wait:.1f} s"
            )
    return row


async def append(
    tx: Tx,
    agent_id: str,
    task_id: str,
    attempt: int,
    source: Source,
    kept: list[str],
) -> LogAccepted:
    """Store a batch (already through `prepare`) for the attempt the caller holds the
    live lease on, then publish it as a transient `task.log` event. Does not renew
    the lease (heartbeats and progress do)."""
    row = await admit(tx, agent_id, task_id, attempt, source)
    first = row["log_seq"] + 1
    if kept:
        at = ts(tx.now)
        await tx.conn.executemany(
            "INSERT INTO task_log (task_id, attempt, seq, source, at, line)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (task_id, attempt, first + i, source, at, line)
                for i, line in enumerate(kept)
            ],
        )
        last_seq = first + len(kept) - 1
        await tx.conn.execute(
            "UPDATE tasks SET log_seq = ?, output_at = ? WHERE id = ?",
            (last_seq, at, task_id),
        )
        await _trim(tx.conn, task_id, attempt, last_seq)
        transient(
            tx,
            f"agent:{agent_id}",
            "task",
            task_id,
            "task.log",
            {
                "task_id": task_id,
                "attempt": attempt,
                "source": source,
                "seq": first,  # line i has seq + i
                "at": at,
                "lines": kept,
            },
        )
    return LogAccepted(
        accepted=len(kept),
        last_seq=row["log_seq"] + len(kept),
        output_at=tx.now if kept else _dt(row["output_at"]),
    )


async def _trim(
    conn: aiosqlite.Connection, task_id: str, attempt: int, last_seq: int
) -> None:
    # One attempt writes at a time, so its seqs are dense: the ring is a seq window.
    await conn.execute(
        "DELETE FROM task_log WHERE task_id = ? AND attempt = ? AND seq <= ?",
        (task_id, attempt, last_seq - RING_LINES),
    )
    await conn.execute(
        "DELETE FROM task_log WHERE task_id = ? AND attempt <= ?",
        (task_id, attempt - KEEP_ATTEMPTS),
    )


async def read(
    conn: aiosqlite.Connection,
    task_id: str,
    attempt: int | None,
    after: int | None,
    limit: int,
) -> LogPage:
    """Lines of one attempt (default: the task's current one). With `after`, the
    lines after that seq, oldest first; without, the newest `limit` lines."""
    # log_seq is read before the lines: every line of the attempt up to it is in the page
    async with conn.execute(
        "SELECT attempt, log_seq FROM tasks WHERE id = ?", (task_id,)
    ) as cur:
        task = await cur.fetchone()
    if task is None:
        raise NotFoundError(f"task not found: {task_id}")
    attempt = task["attempt"] if attempt is None else attempt
    log_seq = task["log_seq"]
    if after is None:
        sql = (
            "SELECT * FROM (SELECT seq, source, at, line FROM task_log"
            " WHERE task_id = ? AND attempt = ? ORDER BY seq DESC LIMIT ?) ORDER BY seq"
        )
        params: tuple = (task_id, attempt, limit)
    else:
        sql = (
            "SELECT seq, source, at, line FROM task_log"
            " WHERE task_id = ? AND attempt = ? AND seq > ? ORDER BY seq LIMIT ?"
        )
        params = (task_id, attempt, after, limit)
    async with conn.execute(sql, params) as cur:
        lines = [LogLine.model_validate(dict(r)) for r in await cur.fetchall()]
    return LogPage(
        attempt=attempt,
        lines=lines,
        last_seq=lines[-1].seq if lines else (log_seq if after is None else after),
    )


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
