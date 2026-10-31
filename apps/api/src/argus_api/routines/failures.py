"""The routine failure threshold (docs/specs/routines.spec.md 3.5). Called from the
task transition that fails a run, in its transaction; no task logic here, so the
task service can import it."""

from argus_core.decisions import Audience, DecisionKind

from argus_api.db import Tx, ts
from argus_api.decisions import store as decisions
from argus_api.events import record

FAILURE_LIMIT = 3


async def after_failed_run(tx: Tx, routine_id: str) -> None:
    """Disable the routine when its last `FAILURE_LIMIT` runs all failed, and ask a
    human (an inbox `policy` decision) whether to re-enable it. Only runs created
    since the routine was last saved or re-enabled count, so a fix starts afresh."""
    async with tx.conn.execute(
        "SELECT project, slug, enabled, updated_at FROM routines WHERE id = ?",
        (routine_id,),
    ) as cur:
        routine = await cur.fetchone()
    if routine is None or not routine["enabled"]:
        return
    async with tx.conn.execute(
        "SELECT id, state FROM tasks WHERE created_by = ? AND created_at >= ?"
        " ORDER BY created_at DESC, id DESC LIMIT ?",
        (f"routine:{routine_id}", routine["updated_at"], FAILURE_LIMIT),
    ) as cur:
        runs = list(await cur.fetchall())
    if len(runs) < FAILURE_LIMIT or any(r["state"] != "failed" for r in runs):
        return
    await tx.conn.execute(
        "UPDATE routines SET enabled = 0, next_run_at = NULL, updated_at = ?"
        " WHERE id = ?",
        (ts(tx.now), routine_id),
    )
    await record(
        tx,
        "system",
        "routine",
        routine_id,
        "routine.disabled",
        {"reason": "consecutive_failures", "task_ids": [r["id"] for r in runs]},
    )
    name = f"{routine['project']}/{routine['slug']}"
    await decisions.open_decision(
        tx,
        subject_type="routine",
        subject_id=routine_id,
        kind=DecisionKind.POLICY,
        question=(
            f"Routine {name} was disabled after {FAILURE_LIMIT} consecutive failed"
            " runs. Re-enable it?"
        ),
        options=["approve", "reject"],
        audience=Audience.HUMAN,
        requested_by="system",
        payload={"reason": "consecutive_failures"},
    )
