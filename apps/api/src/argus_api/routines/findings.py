"""Routine finding intake (docs/specs/routines.spec.md 3.3). Called from the task
transition that succeeds a run, in its transaction. Findings are untrusted data: a
bad entry is skipped and recorded, never raised, so it cannot fail the completion."""

import json
from datetime import timedelta
from typing import Any

import aiosqlite
from argus_core.routines import Finding, FindingIntake
from argus_core.tasks import TaskCreate
from pydantic import ValidationError

from argus_api.db import Tx, ts
from argus_api.errors import DomainError
from argus_api.events import record
from argus_api.tasks import service as tasks

# Finding tasks are created by Argus itself: an agent cannot forge one (a task it
# creates with `input.routine_finding` is `agent:<id>`), so dedup only trusts these.
ACTOR = "system"
# Entries past this many are counted as truncated without being looked at.
MAX_ENTRIES = 1000


async def after_succeeded_run(tx: Tx, routine_id: str, run_id: str) -> None:
    """File the run's `result.data.findings` as child tasks of the run, by the
    routine's `findings` mode, deduplicated by `(routine, key)` in its project."""
    async with tx.conn.execute(
        "SELECT project, findings, template FROM routines WHERE id = ?", (routine_id,)
    ) as cur:
        routine = await cur.fetchone()
    if routine is None or routine["findings"] == "off":
        return
    entries = await _entries(tx.conn, run_id)
    if entries is None:
        return
    try:
        intake = FindingIntake.model_validate(json.loads(routine["template"]))
    except ValidationError:  # checked on save; a hand-edited row gets the defaults
        intake = FindingIntake()
    closed_since = ts(tx.now - timedelta(seconds=intake.dedup_window))
    skipped: list[dict[str, Any]] = []
    keys: set[str] = set()
    filed = 0
    truncated = 0
    if not isinstance(entries, list):
        skipped.append({"index": None, "error": "findings is not a list"})
        entries = []
    for index, entry in enumerate(entries):
        if filed == intake.max_findings or index == MAX_ENTRIES:
            truncated = len(entries) - index
            break
        try:
            finding = Finding.model_validate(entry)
            body = TaskCreate.model_validate(
                {
                    "title": finding.title,
                    "spec": finding.spec,
                    "acceptance": finding.acceptance,
                    "priority": finding.priority,
                    "input": {
                        "routine_finding": {"routine": routine_id, "key": finding.key}
                    },
                    "parent_id": run_id,
                    "project": routine["project"],
                    "requires_approval": routine["findings"] == "approval",
                }
            )
        except ValidationError as exc:
            skipped.append({"index": index, "error": _first_error(exc)})
            continue
        if finding.key in keys:  # a duplicate within this result
            continue
        keys.add(finding.key)
        existing = await _open_or_recent(
            tx.conn, routine["project"], routine_id, finding.key, closed_since
        )
        if existing is not None:
            await record(
                tx,
                ACTOR,
                "task",
                existing,
                "routine.finding_seen",
                {"task_id": run_id},
            )
            continue
        try:
            async with tx.savepoint():
                await tasks.create_task(tx, body, ACTOR, untrusted=True)
        except DomainError as exc:
            skipped.append({"index": index, "error": exc.message})
            continue
        filed += 1
    if skipped:
        await record(
            tx,
            ACTOR,
            "routine",
            routine_id,
            "routine.findings_skipped",
            {"task_id": run_id, "skipped": skipped},
        )
    if truncated:
        await record(
            tx,
            ACTOR,
            "routine",
            routine_id,
            "routine.findings_truncated",
            {"task_id": run_id, "count": truncated},
        )


async def _entries(conn: aiosqlite.Connection, run_id: str) -> object:
    """`result.data.findings` as stored, any shape; None when absent."""
    async with conn.execute("SELECT result FROM tasks WHERE id = ?", (run_id,)) as cur:
        row = await cur.fetchone()
    result = json.loads(row["result"]) if row and row["result"] else {}
    data = result.get("data")
    return data.get("findings") if isinstance(data, dict) else None


async def _open_or_recent(
    conn: aiosqlite.Connection, project: str, routine_id: str, key: str, since: str
) -> str | None:
    """The newest finding task for `(routine, key)` that is open or closed at or
    after `since` (its last transition), if any. The unary `+` keeps the planner on
    the `tasks_routine_finding` index rather than the creator's."""
    async with conn.execute(
        "SELECT id FROM tasks"
        " WHERE json_extract(input, '$.routine_finding.routine') = ?"
        " AND json_extract(input, '$.routine_finding.key') = ?"
        " AND +project = ? AND +created_by = ?"
        " AND (state NOT IN ('succeeded', 'failed', 'cancelled') OR updated_at >= ?)"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        (routine_id, key, project, ACTOR, since),
    ) as cur:
        row = await cur.fetchone()
    return None if row is None else row["id"]


def _first_error(exc: ValidationError) -> str:
    """Location and message only: the untrusted input is not echoed."""
    e = exc.errors(include_input=False, include_url=False)[0]
    loc = ".".join(str(p) for p in e["loc"])
    return f"{loc}: {e['msg']}" if loc else e["msg"]
