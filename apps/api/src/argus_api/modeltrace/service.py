"""Model executions and the routing timeline (docs/specs/model-observability.spec.md).

A leaf module (like `accounts.usage`): the task, account and agent services read and
write through it. Rows are append-only history: a re-sent `key` can only fill gaps or
move a run forward, and a value that contradicts the stored one is a 409.
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import aiosqlite
from argus_core.model_trace import (
    MAX_PER_TASK,
    Execution,
    ExecutionReport,
    LayerModels,
    ModelsSummary,
    ModelTrace,
    Selection,
    SelectionReport,
    UsageRef,
)

from argus_api.db import Tx, ts
from argus_api.errors import ConflictError, NotFoundError, ValidationFailedError
from argus_api.events import record

CLOCK_SKEW = timedelta(minutes=5)
_TERMINAL = ("succeeded", "failed", "cancelled")
_RANK = {"selected": 0, "running": 1, "succeeded": 2, "failed": 2, "cancelled": 2}
# Values that mean "nothing said yet": a later report may replace them.
_EMPTY = {"evidence": "not_reported", "outcome": "unknown"}
_EXECUTION_COLS = [
    "layer",
    "runtime",
    "engine",
    "executor",
    "provider",
    "requested_model",
    "configured_model",
    "observed_model",
    "evidence",
    "policy_revision",
    "account_id",
    "delegation_id",
    "run_key",
    "resumed_from",
    "error_code",
    "started_at",
    "ended_at",
]
_SELECTION_COLS = [
    "layer",
    "kind",
    "reason_code",
    "from_model",
    "to_model",
    "from_provider",
    "to_provider",
    "from_executor",
    "to_executor",
    "from_account_id",
    "to_account_id",
    "execution_key",
    "policy_revision",
    "outcome",
]


def _time(value: datetime | None, now: datetime, what: str) -> str | None:
    if value is None:
        return None
    if value > now + CLOCK_SKEW:
        raise ValidationFailedError(f"{what} is in the future")
    return ts(value)


def _merge(old: aiosqlite.Row, new: dict[str, Any], what: str) -> dict[str, Any]:
    """The stored row completed by `new`. History is never overwritten: a gap is
    filled, a contradiction is refused."""
    merged = {}
    for col, value in new.items():
        stored, empty = old[col], _EMPTY.get(col)
        if stored is None or stored == empty:
            merged[col] = value if value is not None else stored
        elif value is None or value in (empty, stored):
            merged[col] = stored
        else:
            raise ConflictError(
                f"{what} {old['key']!r} already has a different {col}",
                code=f"{what}_conflict",
            )
    return merged


async def _count(tx: Tx, table: str, task_id: str) -> int:
    async with tx.conn.execute(
        f"SELECT count(*) FROM {table} WHERE task_id = ?",  # noqa: S608 - fixed names
        (task_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _check_room(tx: Tx, task_id: str, table: str, adding: int) -> None:
    if adding and await _count(tx, table, task_id) + adding > MAX_PER_TASK:
        raise ValidationFailedError(
            f"a task keeps at most {MAX_PER_TASK} records of this kind",
            code="too_many_records",
        )


async def record_execution(
    tx: Tx, agent_id: str | None, task_id: str, attempt: int, body: ExecutionReport
) -> Execution:
    async with tx.conn.execute(
        "SELECT * FROM model_executions WHERE task_id = ? AND key = ?",
        (task_id, body.key),
    ) as cur:
        old = await cur.fetchone()
    # Times Argus assigns itself fill a gap only; a resend must not move them.
    started = _time(body.started_at, tx.now, "started_at")
    if started is None and body.state == "running" and old is None:
        started = ts(tx.now)
    if body.state in _TERMINAL:
        ended = _time(body.ended_at, tx.now, "ended_at")
        if ended is None and (old is None or old["ended_at"] is None):
            ended = ts(tx.now)
    elif body.ended_at is not None:
        raise ValidationFailedError(
            "ended_at needs a succeeded, failed or cancelled state"
        )
    else:
        ended = None
    new = {
        **{c: getattr(body, c) for c in _EXECUTION_COLS},
        "started_at": started,
        "ended_at": ended,
    }
    if old is None:
        await _check_room(tx, task_id, "model_executions", 1)
        values, state, row_id = new, body.state, uuid4().hex
    else:
        if old["attempt"] != attempt:
            raise ConflictError(
                f"execution {body.key!r} belongs to attempt {old['attempt']}",
                code="execution_conflict",
            )
        values = _merge(old, new, "execution")
        old_state = old["state"]
        if _RANK[body.state] < _RANK[old_state]:
            state = old_state
        elif _RANK[body.state] == _RANK[old_state] == 2 and body.state != old_state:
            raise ConflictError(
                f"execution {body.key!r} already ended as {old_state}",
                code="execution_conflict",
            )
        else:
            state = body.state
        row_id = old["id"]
    # Evidence of a model needs a run that started; nothing was run otherwise.
    if values["observed_model"] and (
        state == "selected" or values["started_at"] is None
    ):
        raise ValidationFailedError("observed_model needs a run that started")
    if (
        values["started_at"]
        and values["ended_at"]
        and values["ended_at"] < values["started_at"]
    ):
        raise ValidationFailedError("ended_at is before started_at")
    if values["resumed_from"] and not await _exists(
        tx, task_id, values["resumed_from"]
    ):
        raise ValidationFailedError("resumed_from is not an execution of this task")
    full = {**values, "state": state}
    if old is not None and all(old[c] == v for c, v in full.items()):
        return await _execution(tx.conn, task_id, row_id)  # nothing new
    if old is None:
        cols = ["id", "task_id", "key", "attempt", "agent_id", "recorded_at", *full]
        args = [
            row_id,
            task_id,
            body.key,
            attempt,
            agent_id,
            ts(tx.now),
            *full.values(),
        ]
        await tx.conn.execute(
            f"INSERT INTO model_executions ({', '.join(cols)}, updated_at)"  # noqa: S608
            f" VALUES ({', '.join('?' * (len(cols) + 1))})",
            [*args, ts(tx.now)],
        )
    else:
        await tx.conn.execute(
            f"UPDATE model_executions SET {', '.join(f'{c} = ?' for c in full)},"  # noqa: S608
            " updated_at = ? WHERE id = ?",
            [*full.values(), ts(tx.now), row_id],
        )
    keep = ("layer", "state", "executor", "engine", "provider", "requested_model")
    keep += ("configured_model", "observed_model", "evidence", "policy_revision")
    await record(
        tx,
        f"agent:{agent_id}" if agent_id else "argus",
        "task",
        task_id,
        "task.model_execution",
        {
            "execution_id": row_id,
            "key": body.key,
            "attempt": attempt,
            **{k: full[k] for k in keep},
        },
    )
    return await _execution(tx.conn, task_id, row_id)


async def _exists(tx: Tx, task_id: str, key: str) -> bool:
    async with tx.conn.execute(
        "SELECT 1 FROM model_executions WHERE task_id = ? AND key = ?", (task_id, key)
    ) as cur:
        return await cur.fetchone() is not None


async def record_selection(
    tx: Tx, agent_id: str | None, task_id: str, attempt: int, body: SelectionReport
) -> Selection:
    async with tx.conn.execute(
        "SELECT * FROM model_selections WHERE task_id = ? AND key = ?",
        (task_id, body.key),
    ) as cur:
        old = await cur.fetchone()
    new = {c: getattr(body, c) for c in _SELECTION_COLS}
    at = _time(body.occurred_at, tx.now, "occurred_at") or ts(tx.now)
    alternatives = json.dumps(
        [a.model_dump(exclude_none=True) for a in body.alternatives]
    )
    if old is None:
        await _check_room(tx, task_id, "model_selections", 1)
        row_id = uuid4().hex
        cols = ["id", "task_id", "key", "attempt", "agent_id", "explanation",
                "alternatives", "at", "recorded_at", *new]  # fmt: skip
        args = [row_id, task_id, body.key, attempt, agent_id, body.explanation,
                alternatives, at, ts(tx.now), *new.values()]  # fmt: skip
        await tx.conn.execute(
            f"INSERT INTO model_selections ({', '.join(cols)})"  # noqa: S608
            f" VALUES ({', '.join('?' * len(cols))})",
            args,
        )
        changed = full = new
    else:
        if old["attempt"] != attempt:
            raise ConflictError(
                f"selection {body.key!r} belongs to attempt {old['attempt']}",
                code="selection_conflict",
            )
        row_id = old["id"]
        full = _merge(old, new, "selection")
        changed = {c: v for c, v in full.items() if old[c] != v}
        if changed:
            await tx.conn.execute(
                f"UPDATE model_selections SET {', '.join(f'{c} = ?' for c in changed)}"  # noqa: S608
                " WHERE id = ?",
                [*changed.values(), row_id],
            )
    if changed:
        keep = (
            "layer",
            "kind",
            "reason_code",
            "from_model",
            "to_model",
            "from_account_id",
        )
        keep += ("to_account_id", "outcome", "policy_revision")
        await record(
            tx,
            f"agent:{agent_id}" if agent_id else "argus",
            "task",
            task_id,
            "task.model_selection",
            {"selection_id": row_id, "key": body.key, "attempt": attempt}
            | {k: full[k] for k in keep},
        )
    return await _selection(tx.conn, task_id, row_id)


async def record_account_switch(
    tx: Tx,
    agent_id: str,
    task_id: str,
    attempt: int,
    from_account_id: str,
    to_account_id: str,
    reason: str,
) -> None:
    """The harness account changed on quota (account selection 3.6). Argus knows the
    accounts and their providers, never the model: the kind is `account_switch` when
    the provider stayed and `provider_switch` when it changed, and the models stay
    unknown rather than guessed."""
    providers = {}
    async with tx.conn.execute(
        "SELECT id, provider FROM accounts WHERE id IN (?, ?)",
        (from_account_id, to_account_id),
    ) as cur:
        providers = {r["id"]: r["provider"] for r in await cur.fetchall()}
    a, b = providers.get(from_account_id), providers.get(to_account_id)
    n = await _count(tx, "model_selections", task_id)
    await record_selection(
        tx,
        agent_id,
        task_id,
        attempt,
        SelectionReport.model_validate(
            {
                "key": f"argus:account:{attempt}:{n + 1}",
                "layer": "harness",
                "kind": "provider_switch" if a and b and a != b else "account_switch",
                "reason_code": "quota",
                "explanation": reason,
                "from_account_id": from_account_id,
                "to_account_id": to_account_id,
                "from_provider": a,
                "to_provider": b,
            }
        ),
    )


# -- reads -----------------------------------------------------------------------

_SELECT_EXECUTION = (
    "SELECT e.*, u.measurement AS u_measurement, u.input_tokens AS u_in,"
    " u.output_tokens AS u_out, u.cached_input_tokens AS u_cached,"
    " u.cost_usd AS u_cost FROM model_executions e LEFT JOIN account_usage u"
    " ON u.account_id = e.account_id AND u.run_key = e.run_key"
)
_ORDER = " ORDER BY coalesce(e.started_at, e.recorded_at), e.rowid"


def _to_execution(row: aiosqlite.Row) -> Execution:
    data = dict(row)
    usage = None
    if data["u_measurement"] is not None:
        usage = UsageRef(
            measurement=data["u_measurement"],
            input_tokens=data["u_in"],
            output_tokens=data["u_out"],
            cached_input_tokens=data["u_cached"],
            cost_usd=data["u_cost"],
        )
    return Execution.model_validate({**data, "usage": usage})


async def _execution(
    conn: aiosqlite.Connection, task_id: str, row_id: str
) -> Execution:
    async with conn.execute(
        _SELECT_EXECUTION + " WHERE e.task_id = ? AND e.id = ?", (task_id, row_id)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    return _to_execution(row)


def _to_selection(row: aiosqlite.Row) -> Selection:
    return Selection.model_validate(
        {**dict(row), "alternatives": json.loads(row["alternatives"])}
    )


async def _selection(
    conn: aiosqlite.Connection, task_id: str, row_id: str
) -> Selection:
    async with conn.execute(
        "SELECT * FROM model_selections WHERE task_id = ? AND id = ?", (task_id, row_id)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    return _to_selection(row)


def layer_models(run: Execution, count: int) -> LayerModels:
    fields = set(LayerModels.model_fields) - {"execution_id", "executions"}
    return LayerModels(
        **run.model_dump(include=fields), execution_id=run.id, executions=count
    )


def _summarize(executions: list[Execution], switches: int) -> ModelsSummary:
    layers: dict[str, list[Execution]] = defaultdict(list)
    for e in executions:  # oldest first, so the last one is the current one
        layers[e.layer].append(e)
    out = {
        layer: layer_models(items[-1], len(items)) for layer, items in layers.items()
    }
    return ModelsSummary(**out, executions=len(executions), switches=switches)


async def summaries(
    conn: aiosqlite.Connection, task_ids: list[str]
) -> dict[str, ModelsSummary]:
    """Compact card summaries for many tasks in two queries; a task with no records
    has an empty summary (`null` layers, zero counts), never invented values."""
    marks = ",".join("?" * len(task_ids))
    by_task: dict[str, list[Execution]] = defaultdict(list)
    async with conn.execute(
        f"{_SELECT_EXECUTION} WHERE e.task_id IN ({marks}){_ORDER}",
        task_ids,
    ) as cur:
        for row in await cur.fetchall():
            by_task[row["task_id"]].append(_to_execution(row))
    switches: dict[str, int] = {}
    async with conn.execute(
        "SELECT task_id, count(*) FROM model_selections"  # noqa: S608
        f" WHERE kind != 'initial' AND task_id IN ({marks}) GROUP BY task_id",
        task_ids,
    ) as cur:
        switches = {r[0]: r[1] for r in await cur.fetchall()}
    return {i: _summarize(by_task.get(i, []), switches.get(i, 0)) for i in task_ids}


async def get_trace(conn: aiosqlite.Connection, task_id: str) -> ModelTrace:
    async with conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)) as cur:
        if await cur.fetchone() is None:
            raise NotFoundError(f"task not found: {task_id}")
    async with conn.execute(
        f"{_SELECT_EXECUTION} WHERE e.task_id = ?{_ORDER}", (task_id,)
    ) as cur:
        executions = [_to_execution(r) for r in await cur.fetchall()]
    async with conn.execute(
        "SELECT * FROM model_selections WHERE task_id = ? ORDER BY at, rowid",
        (task_id,),
    ) as cur:
        timeline = [_to_selection(r) for r in await cur.fetchall()]
    switches = sum(s.kind != "initial" for s in timeline)
    return ModelTrace(
        task_id=task_id,
        summary=_summarize(executions, switches),
        executions=executions,
        timeline=timeline,
    )


async def agent_history(
    conn: aiosqlite.Connection, agent_id: str, limit: int = 50
) -> list[Execution]:
    """An agent's latest runs across tasks, newest first, each with its evidence."""
    async with conn.execute(
        f"{_SELECT_EXECUTION} WHERE e.agent_id = ?"
        " ORDER BY coalesce(e.started_at, e.recorded_at) DESC, e.rowid DESC LIMIT ?",
        (agent_id, limit),
    ) as cur:
        return [_to_execution(r) for r in await cur.fetchall()]
