"""Routine runs: the scheduler loop and run now (docs/specs/routines.spec.md 3.2
and 3.4). A run is an ordinary task created with the task create path."""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Literal

import aiosqlite
from argus_core.routines import RoutineRunResult
from argus_core.tasks import TERMINAL, TaskCreate, TaskState
from pydantic import ValidationError

from argus_api.db import Database, Tx, ts
from argus_api.errors import DomainError, NotFoundError, ValidationFailedError
from argus_api.events import record
from argus_api.routines.schedule import next_slots, parse_schedule, parse_timezone
from argus_api.settings import service as settings
from argus_api.tasks import service as tasks

log = logging.getLogger(__name__)

SCHEDULER_INTERVAL_S = 30
ENABLED_KEY = "routines.enabled"
# TaskCreate's title limit; the run time suffix must fit inside it.
TITLE_MAX = 200

Trigger = Literal["schedule", "manual"]


async def run(db: Database, path: Path, interval: float = SCHEDULER_INTERVAL_S) -> None:
    """The scheduler loop. `path` is the model policy file the settings reader takes."""
    while True:
        try:
            async with db.write() as tx:
                created = await tick(tx, path)
            if created:
                log.info("routines: created %d run tasks", len(created))
        except Exception:
            log.exception("routine scheduler pass failed")
        await asyncio.sleep(interval)


async def tick(tx: Tx, path: Path) -> list[str]:
    """Start every due routine once and move it to its next slot after `tx.now`, so
    downtime yields one catch-up run, never a backfill. Returns the run task ids.
    While `routines.enabled` is off nothing is touched: resuming gives one catch-up."""
    enabled = await settings.get_setting(tx.conn, path, settings.GLOBAL, ENABLED_KEY)
    if not enabled.value:
        return []
    async with tx.conn.execute(
        "SELECT * FROM routines WHERE enabled = 1 AND next_run_at <= ?"
        " ORDER BY next_run_at, id",
        (ts(tx.now),),
    ) as cur:
        rows = await cur.fetchall()
    created = []
    for row in rows:
        slot = next_slots(
            parse_schedule(row["schedule"]), parse_timezone(row["timezone"]), tx.now
        )[0]
        await tx.conn.execute(
            "UPDATE routines SET next_run_at = ? WHERE id = ?", (ts(slot), row["id"])
        )
        result = await _start(tx, row, "system", "schedule")
        if result.task_id is not None:
            created.append(result.task_id)
    return created


async def run_now(tx: Tx, routine_id: str, actor: str) -> RoutineRunResult:
    """Run the routine now, subject to the overlap rule. Allowed while the routine
    is disabled or routines are paused; `next_run_at` is left as it is."""
    async with tx.conn.execute(
        "SELECT * FROM routines WHERE id = ?", (routine_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"routine not found: {routine_id}")
    return await _start(tx, row, actor, "manual")


async def _start(
    tx: Tx, row: aiosqlite.Row, actor: str, trigger: Trigger
) -> RoutineRunResult:
    routine_id = row["id"]
    previous = row["last_task_id"]
    if previous is not None and await _running(tx.conn, previous):
        await record(
            tx,
            actor,
            "routine",
            routine_id,
            "routine.skipped",
            {
                "reason": "previous_running",
                "previous_task_id": previous,
                "trigger": trigger,
            },
        )
        return RoutineRunResult(
            routine_id=routine_id, task_id=None, skipped="previous_running"
        )
    try:
        # A savepoint: one routine whose template no longer passes the create rules
        # (a removed kind or page) must not hold back the others in the tick.
        async with tx.savepoint():
            task = await tasks.create_task(
                tx, _run_body(row, tx.now), f"routine:{routine_id}"
            )
    except DomainError as exc:
        if trigger == "manual":
            raise
        log.warning("routine %s: run not created: %s", routine_id, exc.message)
        await record(
            tx,
            actor,
            "routine",
            routine_id,
            "routine.skipped",
            {"reason": "create_failed", "error": exc.message, "trigger": trigger},
        )
        return RoutineRunResult(routine_id=routine_id, task_id=None)
    await tx.conn.execute(
        "UPDATE routines SET last_run_at = ?, last_task_id = ? WHERE id = ?",
        (ts(tx.now), task.id, routine_id),
    )
    await record(
        tx,
        actor,
        "routine",
        routine_id,
        "routine.run",
        {"task_id": task.id, "trigger": trigger},
    )
    return RoutineRunResult(routine_id=routine_id, task_id=task.id)


async def _running(conn: aiosqlite.Connection, task_id: str) -> bool:
    async with conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)) as cur:
        found = await cur.fetchone()
    return found is not None and TaskState(found["state"]) not in TERMINAL


def _run_body(row: aiosqlite.Row, run_at: datetime) -> TaskCreate:
    """The run task: the template, titled with the run time on the routine's clock,
    with `input.routine` (routines spec 3.2)."""
    template = json.loads(row["template"])
    tz = parse_timezone(row["timezone"])
    suffix = f" ({run_at.astimezone(tz):%Y-%m-%d %H:%M} {tz.key})"
    title = (template.get("title") or row["title"])[: TITLE_MAX - len(suffix)]
    routine = {
        "id": row["id"],
        "slug": row["slug"],
        "run_at": ts(run_at),
        "previous_run_at": row["last_run_at"],
    }
    try:
        return TaskCreate.model_validate(
            template
            | {
                "title": title + suffix,
                "input": template.get("input", {}) | {"routine": routine},
                "project": row["project"],
            }
        )
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False)
        ]
        raise ValidationFailedError(
            "invalid run task: " + "; ".join(errors), details=errors
        ) from exc
