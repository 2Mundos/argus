"""Task reads: rows, their `Task` model, and the small lookups the writes share."""

import json
from datetime import datetime, timedelta

import aiosqlite
from argus_core.agents import AgentState
from argus_core.continuation import effective
from argus_core.custom_accounts import CustomConfig
from argus_core.quota import failure_class
from argus_core.task_edit import editable_fields
from argus_core.tasks import Failover, ServingAccount, Task, TaskState

from argus_api.accounts.eligibility import engine_pool
from argus_api.db import ts
from argus_api.errors import NotFoundError
from argus_api.integrations import selection
from argus_api.modeltrace import service as modeltrace
from argus_api.tasks import continuation as cont
from argus_api.tasks.reasons import QUOTA_WAIT_REASONS, RUNTIME_SWITCH_FAILED

S = TaskState
_JSON_FIELDS = ("acceptance", "input", "required_capabilities")


async def get_task(conn: aiosqlite.Connection, task_id: str) -> Task:
    row = await _row(conn, task_id)
    return await _to_task(conn, row)


async def task_row(conn: aiosqlite.Connection, task_id: str) -> aiosqlite.Row:
    return await _row(conn, task_id)


_TICK = timedelta(microseconds=1)


_PIN_FIELDS = ("harness_model", "executor_model")


async def _states_of(
    conn: aiosqlite.Connection, table: str, ids: list[str]
) -> dict[str, str]:
    marks = ",".join("?" * len(ids))
    async with conn.execute(
        f"SELECT id, state FROM {table} WHERE id IN ({marks})",  # noqa: S608
        ids,
    ) as cur:
        return {r["id"]: r["state"] for r in await cur.fetchall()}


async def _agent_state(conn: aiosqlite.Connection, agent_id: str) -> AgentState:
    async with conn.execute(
        "SELECT state FROM agents WHERE id = ?", (agent_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"agent not found: {agent_id}")
    return AgentState(row["state"])


async def _row(conn: aiosqlite.Connection, task_id: str) -> aiosqlite.Row:
    async with conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"task not found: {task_id}")
    return row


async def _states(conn: aiosqlite.Connection, ids: list[str]) -> dict[str, TaskState]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    async with conn.execute(
        f"SELECT id, state FROM tasks WHERE id IN ({marks})",  # noqa: S608
        ids,
    ) as cur:
        return {r["id"]: S(r["state"]) for r in await cur.fetchall()}


async def _deps(conn: aiosqlite.Connection, task_id: str) -> list[str]:
    async with conn.execute(
        "SELECT depends_on_id FROM task_deps WHERE task_id = ? ORDER BY depends_on_id",
        (task_id,),
    ) as cur:
        return [r[0] for r in await cur.fetchall()]


async def _to_task(conn: aiosqlite.Connection, row: aiosqlite.Row) -> Task:
    data = dict(row)
    for f in _JSON_FIELDS:
        data[f] = json.loads(data[f])
    if data["result"] is not None:
        data["result"] = json.loads(data["result"])
    if data["progress_data"] is not None:
        data["progress_data"] = json.loads(data["progress_data"])
    if data["review_disposition"] is not None:
        data["review_disposition"] = json.loads(data["review_disposition"])
    if data["account_providers"] is not None:
        data["account_providers"] = json.loads(data["account_providers"])
    for f in _PIN_FIELDS:
        if data[f] is not None:
            data[f] = json.loads(data[f])
    data["requires_approval"] = bool(data["requires_approval"])
    data["continuation"] = effective(cont.load(row), data["acceptance"])
    data["version"] = row["updated_at"]
    data["editable_fields"] = editable_fields(
        S(row["state"]), archived=row["archived_at"] is not None
    )
    data["depends_on"] = await _deps(conn, row["id"])
    data["refill_at"] = await _refill_at(conn, row)
    data["serving_account"] = await _serving_account(conn, row)
    data["failover"] = await _last_failover(conn, row)
    data["failure_class"] = _failure_class(row, data["failover"])
    data["models"] = (await modeltrace.summaries(conn, [row["id"]]))[row["id"]]
    async with conn.execute(
        "SELECT page_id FROM task_knowledge WHERE task_id = ? ORDER BY page_id",
        (row["id"],),
    ) as cur:
        data["knowledge"] = [r[0] for r in await cur.fetchall()]
    data["integrations"] = await selection.selected(conn, row["id"])
    return Task.model_validate(data)


async def _refill_at(conn: aiosqlite.Connection, row: aiosqlite.Row) -> datetime | None:
    """When the earliest exhausted account a model run could use resets."""
    if row["state"] != S.BLOCKED or row["block_reason"] not in QUOTA_WAIT_REASONS:
        return None
    return (await engine_pool(conn, None)).next_reset


async def _serving_account(
    conn: aiosqlite.Connection, row: aiosqlite.Row
) -> ServingAccount | None:
    """The account the claiming agent's harness runs on (name only, no secret)."""
    if row["state"] != S.CLAIMED:
        return None
    async with conn.execute(
        "SELECT a.id, a.name, a.provider, a.config FROM account_leases l"
        " JOIN accounts a ON a.id = l.account_id WHERE l.agent_id = ?"
        " AND l.role = 'engine' AND l.released_at IS NULL",
        (row["assignee_agent_id"],),
    ) as cur:
        acc = await cur.fetchone()
    if acc is None:
        return None
    config = CustomConfig.model_validate_json(acc["config"]) if acc["config"] else None
    return ServingAccount(
        id=acc["id"],
        name=acc["name"],
        provider=acc["provider"],
        model=config.engine.model if config else None,
        host=config.host if config else None,
    )


async def _last_failover(
    conn: aiosqlite.Connection, row: aiosqlite.Row
) -> Failover | None:
    """The last switch of the serving account, derived from events: the failover
    itself plus what the channel reported after it (applied, or failed to stop the
    old runtime). Never a credential or a prompt."""
    if row["attempt"] == 0:
        return None
    async with conn.execute(
        "SELECT id, at, data FROM events WHERE subject_type = 'task'"
        " AND subject_id = ? AND kind = 'account.failover' ORDER BY id DESC LIMIT 1",
        (row["id"],),
    ) as cur:
        event = await cur.fetchone()
    if event is None:
        return None
    d = json.loads(event["data"])
    attempt = d.get("attempt", row["attempt"])
    async with conn.execute(
        "SELECT kind, at, data FROM events WHERE subject_type = 'task'"
        " AND subject_id = ? AND id > ? AND kind IN"
        " ('account.switch_applied', 'account.switch_failed') ORDER BY id DESC",
        (row["id"], event["id"]),
    ) as cur:
        later = [
            e
            for e in await cur.fetchall()
            if json.loads(e["data"]).get("attempt") in (attempt, attempt + 1)
        ]
    outcome = d.get("outcome")
    apply = d.get("apply")
    apply_error = None
    applied_at = None
    if later:
        last = later[0]
        if last["kind"] == "account.switch_applied":
            apply, applied_at = "applied", datetime.fromisoformat(last["at"])
        else:
            outcome, apply = "switch_failed", "failed"
            apply_error = json.loads(last["data"]).get("error")
    reset_at = d.get("reset_at")
    return Failover(
        from_account=d.get("from"),
        to_account=d.get("to"),
        reason=d.get("reason", ""),
        at=datetime.fromisoformat(event["at"]),
        attempt=attempt,
        reason_code=d.get("reason_code"),
        reset_status=d.get("reset_status"),
        reset_text=d.get("reset_text"),
        reset_at=datetime.fromisoformat(reset_at) if reset_at else None,
        source=d.get("source"),
        outcome=outcome,
        apply=apply,
        apply_error=apply_error,
        applied_at=applied_at,
    )


def _failure_class(row: aiosqlite.Row, failover: Failover | None) -> str | None:
    """Why the task last failed, for REST/MCP/UI. Only `quota_exhausted` and
    `auth_expired` rotate the account; the rest never do."""
    if row["block_reason"] == RUNTIME_SWITCH_FAILED:
        return RUNTIME_SWITCH_FAILED
    if (
        failover is not None
        and failover.reason_code
        and failover.attempt == row["attempt"]
        and row["state"] in (S.READY, S.BLOCKED)
    ):
        return failover.reason_code
    if row["error"]:
        return failure_class(row["error"])
    return None


def _deadline(now: datetime, ttl_s: int) -> str:
    return ts(now + timedelta(seconds=ttl_s))
