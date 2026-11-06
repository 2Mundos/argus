"""Read-only route diagnostic of one task (task models spec 3.5).

It answers "why did this task run where it ran, and what would it use now" from the
registry and the recorded attempt evidence only. It grants nothing, writes nothing,
makes no network call and returns no secret: account rows are reduced to id, name
and status, and free text is redacted and bounded. Account availability is registry
state; whether one agent may use an account through the credential gateway, or has
an executable installed, is never inferred from it: it is `unknown` until an attempt
reported the failure.
"""

import json
import re
from datetime import datetime
from typing import Literal

import aiosqlite
from argus_core.accounts import (
    ENGINE_PROVIDERS,
    EXECUTOR_ONLY,
    REPOSITORY_PROVIDERS,
)
from argus_core.continuation import clean
from argus_core.quota import failure_class
from argus_core.tasks import (
    EXECUTOR_PROVIDERS,
    ExecutorModel,
    HarnessModel,
    TaskState,
)
from pydantic import BaseModel

from argus_api.tasks import service

_TEXT_MAX = 500
NOTE = (
    "Account status is registry state. It does not prove that this agent may use"
    " the account through the credential gateway, nor that the executor binary is"
    " installed; those are `unknown` until an attempt reports them."
)
# What an attempt reports when the gateway refused the agent, or the CLI is absent.
_GATEWAY_DENIED = re.compile(
    r"\baccess_restricted\b|\bapp_not_connected\b|\b401\b[^\n]{0,80}\b(gateway|onecli)\b",
    re.IGNORECASE,
)
_BINARY_MISSING = re.compile(
    r"\bnot_installed\b|\bcommand not found\b|\bbinary (is )?missing\b"
    r"|\bnot installed\b|\bENOENT\b",
    re.IGNORECASE,
)


class RouteAccount(BaseModel):
    id: str
    name: str
    # `exhausted` also covers every slot leased out (`max_leases`).
    status: Literal["usable", "exhausted", "disabled"]


class RouteProvider(BaseModel):
    provider: str
    # Whether a harness can run on it, and the `executor_model` pin naming it.
    engine: Literal["supported", "unsupported"]
    executor: str | None  # None: no executor pin can name this provider
    # The task's `account_providers` allows it for the engine role.
    engine_allowed: bool
    accounts: list[RouteAccount]


class RouteExecution(BaseModel):
    layer: str
    state: str
    engine: str | None
    executor: str | None
    provider: str | None
    account_id: str | None
    error_code: str | None


class RouteAttempt(BaseModel):
    attempt: int
    agent_id: str
    claimed_at: datetime
    executions: list[RouteExecution]


class Observation(BaseModel):
    status: Literal["unknown", "denied", "missing"]
    evidence: str | None = None  # the redacted line that reported it


class RouteWorker(BaseModel):
    agent_id: str
    name: str
    state: str
    last_heartbeat_at: datetime
    # Self-reported by the runtime: what it can technically apply, not a grant.
    runtime_providers: list[str] | None
    gateway_access: Observation
    executor_binary: Observation


class RouteResolution(BaseModel):
    """What the engine role would use now, for the worker of the current attempt."""

    engine_providers: list[str] | None  # None: every engine provider
    engine_account: str | None
    fallback_reason: str | None  # a pin fell back: exhausted, disabled, offline
    executor_provider: str | None  # the executor pin's provider


class TaskRoute(BaseModel):
    task_id: str
    state: TaskState
    block_reason: str | None
    version: str
    account_providers: list[str] | None
    harness_model: HarnessModel | None
    executor_model: ExecutorModel | None
    harness_account: str | None
    resolution: RouteResolution
    attempt: RouteAttempt | None
    error: str | None
    failure_class: str | None
    providers: list[RouteProvider]
    worker: RouteWorker | None
    note: str = NOTE


async def diagnose_route(
    conn: aiosqlite.Connection, now: datetime, task_id: str
) -> TaskRoute:
    """The task's route, pins, evidence and readiness; read-only (spec 3.5)."""
    row = await service.task_row(conn, task_id)
    stored = (
        None
        if row["account_providers"] is None
        else json.loads(row["account_providers"])
    )
    harness = (
        None
        if row["harness_model"] is None
        else HarnessModel.model_validate_json(row["harness_model"])
    )
    executor = (
        None
        if row["executor_model"] is None
        else ExecutorModel.model_validate_json(row["executor_model"])
    )
    attempt = await _attempt(conn, task_id, row["attempt"])
    error = clean(row["error"], _TEXT_MAX)
    worker = None
    if attempt is not None:
        worker = await _worker(conn, attempt, error)
    choice = await service.engine_choice_for(
        conn,
        now,
        row,
        worker.agent_id if worker else None,
        worker.runtime_providers if worker else None,
    )
    return TaskRoute(
        task_id=task_id,
        state=TaskState(row["state"]),
        block_reason=row["block_reason"],
        version=row["updated_at"],
        account_providers=stored,
        harness_model=harness,
        executor_model=executor,
        harness_account=row["harness_account"],
        resolution=RouteResolution(
            engine_providers=choice.providers,
            engine_account=choice.account,
            fallback_reason=choice.reason,
            executor_provider=executor.provider if executor else None,
        ),
        attempt=attempt,
        error=error,
        failure_class=(
            failure_class(row["error"], row["block_reason"] or "")
            if row["error"]
            else None
        ),
        providers=await _providers(conn, now, stored),
        worker=worker,
    )


async def _attempt(
    conn: aiosqlite.Connection, task_id: str, number: int
) -> RouteAttempt | None:
    async with conn.execute(
        "SELECT agent_id, claimed_at FROM task_attempts WHERE task_id = ?"
        " AND attempt = ? ORDER BY id DESC LIMIT 1",
        (task_id, number),
    ) as cur:
        claim = await cur.fetchone()
    if claim is None:
        return None
    async with conn.execute(
        "SELECT layer, state, engine, executor, provider, account_id, error_code"
        " FROM model_executions WHERE task_id = ? AND attempt = ?"
        " ORDER BY recorded_at, id",
        (task_id, number),
    ) as cur:
        runs = [RouteExecution(**dict(r)) for r in await cur.fetchall()]
    return RouteAttempt(
        attempt=number,
        agent_id=claim["agent_id"],
        claimed_at=claim["claimed_at"],
        executions=runs,
    )


async def _worker(
    conn: aiosqlite.Connection, attempt: RouteAttempt, error: str | None
) -> RouteWorker | None:
    async with conn.execute(
        "SELECT id, name, state, last_heartbeat_at, runtime_providers FROM agents"
        " WHERE id = ?",
        (attempt.agent_id,),
    ) as cur:
        agent = await cur.fetchone()
    if agent is None:
        return None
    reports = [error or ""] + [r.error_code or "" for r in attempt.executions]
    return RouteWorker(
        agent_id=agent["id"],
        name=agent["name"],
        state=agent["state"],
        last_heartbeat_at=agent["last_heartbeat_at"],
        runtime_providers=(
            None
            if agent["runtime_providers"] is None
            else json.loads(agent["runtime_providers"])
        ),
        gateway_access=_observe(reports, _GATEWAY_DENIED, "denied"),
        executor_binary=_observe(reports, _BINARY_MISSING, "missing"),
    )


def _observe(
    reports: list[str], pattern: re.Pattern[str], failed: Literal["denied", "missing"]
) -> Observation:
    for text in reports:
        for line in text.splitlines():
            if pattern.search(line):
                return Observation(status=failed, evidence=clean(line, 200))
    return Observation(status="unknown")


async def _providers(
    conn: aiosqlite.Connection, now: datetime, stored: list[str] | None
) -> list[RouteProvider]:
    async with conn.execute(
        "SELECT a.id, a.name, a.provider, a.state, a.exhausted_until, a.max_leases,"
        " (SELECT count(*) FROM account_leases l WHERE l.account_id = a.id"
        "  AND l.released_at IS NULL) AS active FROM accounts a"
        " ORDER BY a.provider, a.priority, a.name",
    ) as cur:
        rows = await cur.fetchall()
    by_provider: dict[str, list[RouteAccount]] = {}
    for r in rows:
        if r["provider"] in REPOSITORY_PROVIDERS:
            continue
        by_provider.setdefault(r["provider"], []).append(
            RouteAccount(id=r["id"], name=r["name"], status=_status(r, now))
        )
    pins = {p: e for e, p in EXECUTOR_PROVIDERS.items()}
    names = sorted(ENGINE_PROVIDERS | EXECUTOR_ONLY | set(by_provider))
    return [
        RouteProvider(
            provider=p,
            engine="supported" if p in ENGINE_PROVIDERS else "unsupported",
            executor=pins.get(p),
            engine_allowed=p in ENGINE_PROVIDERS and (stored is None or p in stored),
            accounts=by_provider.get(p, []),
        )
        for p in names
    ]


def _status(
    r: aiosqlite.Row, now: datetime
) -> Literal["usable", "exhausted", "disabled"]:
    if r["state"] == "disabled":
        return "disabled"
    until = r["exhausted_until"] and datetime.fromisoformat(r["exhausted_until"])
    held_back = r["state"] == "exhausted" and (not until or until > now)
    full = r["max_leases"] is not None and r["active"] >= r["max_leases"]
    return "exhausted" if held_back or full else "usable"
