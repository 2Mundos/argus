"""Engine choice: which engine and account may run a task, and capacity to run it."""

import json
from datetime import datetime
from typing import Any

import aiosqlite
from argus_core.accounts import ENGINE_PROVIDERS, LOCAL_PROVIDER
from argus_core.custom_accounts import CustomConfig, serves_role
from argus_core.difficulty import Difficulty
from argus_core.tasks import ExecutorModel, HarnessModel

from argus_api.accounts.eligibility import LEASE_BLOCK_SQL, engine_pool
from argus_api.db import Tx, ts
from argus_api.engine_choice import EngineAccount, EngineChoice
from argus_api.errors import ConflictError
from argus_api.events import record
from argus_api.tasks.manage import unblock
from argus_api.tasks.reads import _row
from argus_api.tasks.reasons import QUOTA_WAIT_REASONS


async def prefers_local(conn: aiosqlite.Connection, task_id: str) -> bool:
    """A trivial task ranks the local model first (task difficulty spec 3)."""
    row = await _row(conn, task_id)
    return row["difficulty"] == Difficulty.TRIVIAL


async def executor_provider(conn: aiosqlite.Connection, task_id: str) -> str | None:
    """The provider of the task's pinned executor, None when not pinned."""
    pin = (await _row(conn, task_id))["executor_model"]
    return None if pin is None else ExecutorModel.model_validate_json(pin).provider


def pinned_provider(row: aiosqlite.Row) -> str | None:
    """The provider the task's harness model pin needs, None when not pinned."""
    pin = row["harness_model"]
    return None if pin is None else HarnessModel.model_validate_json(pin).provider


def engine_choice(
    row: aiosqlite.Row,
    accounts: list[EngineAccount],
    known: set[str],
    runtime_providers: list[str] | None = None,
) -> EngineChoice:
    """The one decision of which engine providers and account a task may use now: a
    harness pin is a preference, kept while its provider (or account) is usable and
    otherwise dropped for the stored `account_providers` (task models spec 3.2-3.4).
    The claim gate, waking, lease, `exhausted` and `fail_task` all read it. A pinned
    account that no longer exists never falls back: the task waits for its edit."""
    stored = (
        None
        if row["account_providers"] is None
        else json.loads(row["account_providers"])
    )
    pin = pinned_provider(row)
    if pin is not None and stored is not None and pin not in stored:
        return EngineChoice([])  # edits refuse this; never widen if it happens anyway
    online = [
        a
        for a in accounts
        if runtime_providers is None or a.provider in runtime_providers
    ]
    reason = None
    if (account_id := row["harness_account"]) is not None:
        if account_id not in known:
            return EngineChoice([], account_id)
        mine = [a for a in accounts if a.id == account_id]
        if any(a.status == "usable" for a in online if a.id == account_id):
            return EngineChoice([mine[0].provider], account_id)
        reason = _unusable_reason(mine, online)
    if pin is not None:
        mine = [a for a in accounts if a.provider == pin]
        if any(a.status == "usable" for a in online if a.provider == pin):
            return EngineChoice([pin], None, reason)
        reason = reason or _unusable_reason(mine, online)
    return EngineChoice(stored, None, reason)


def _unusable_reason(mine: list[EngineAccount], online: list[EngineAccount]) -> str:
    """Why the pinned account(s) cannot serve: `offline` when no runtime applies
    them (or none is known), `exhausted` when any is spent or full, else `disabled`."""
    reachable = [a for a in mine if a in online]
    if not reachable:
        return "offline"
    if any(a.status == "exhausted" for a in reachable):
        return "exhausted"
    return "disabled"


async def known_engine_accounts(conn: aiosqlite.Connection) -> set[str]:
    async with conn.execute("SELECT id FROM accounts") as cur:
        return {r[0] for r in await cur.fetchall()}


async def engine_accounts(
    conn: aiosqlite.Connection, now: datetime, agent_id: str | None = None
) -> list[EngineAccount]:
    """Every engine account with its `status` for the pin preference. An account the
    caller holds counts even at `max_leases`."""
    async with conn.execute(
        "SELECT a.id, a.provider, a.config, a.state, a.exhausted_until, a.max_leases,"
        " (SELECT count(*) FROM account_leases l WHERE l.account_id = a.id"
        "  AND l.released_at IS NULL) AS active,"
        " EXISTS (SELECT 1 FROM account_leases l WHERE l.account_id = a.id"
        "  AND l.agent_id = ? AND l.role = 'engine'"
        "  AND l.released_at IS NULL) AS mine FROM accounts a",
        (agent_id,),
    ) as cur:
        rows = await cur.fetchall()
    out = []
    for r in rows:
        config = CustomConfig.model_validate_json(r["config"]) if r["config"] else None
        if r["provider"] not in ENGINE_PROVIDERS or not serves_role(
            r["provider"], config, "engine"
        ):
            continue
        until = r["exhausted_until"] and datetime.fromisoformat(r["exhausted_until"])
        full = (
            r["max_leases"] is not None
            and r["active"] >= r["max_leases"]
            and not r["mine"]
        )
        held_back = r["state"] == "exhausted" and (not until or until > now)
        if r["state"] == "disabled":
            status = "disabled"
        else:
            status = "exhausted" if held_back or full else "usable"
        out.append(EngineAccount(r["id"], r["provider"], status))
    return out


async def engine_choice_for(
    conn: aiosqlite.Connection,
    now: datetime,
    row: aiosqlite.Row,
    agent_id: str | None,
    runtime_providers: list[str] | None,
) -> EngineChoice:
    """`engine_choice` over the registry as the caller sees it."""
    return engine_choice(
        row,
        await engine_accounts(conn, now, agent_id),
        await known_engine_accounts(conn),
        runtime_providers,
    )


async def record_model_switch(
    tx: Tx,
    task_id: str,
    attempt: int,
    agent_id: str,
    reason: str,
    leased: dict[str, str],
) -> dict[str, Any] | None:
    """A harness pin fell back to the account the engine lease landed on (task models
    spec 3.3): a `task.model_switched` event and the attempt snapshot's `model_switch`.
    `from` is the pin (provider, model, account id), `to` the leased account (ids and
    names only; its model is the channel's policy default, unknown here). Once per
    attempt and account. Returns the record, None when already recorded."""
    row = await _row(tx.conn, task_id)
    pin = row["harness_model"] and HarnessModel.model_validate_json(
        row["harness_model"]
    )
    provider = pin.provider if pin else None
    if row["harness_account"] is not None:
        async with tx.conn.execute(
            "SELECT provider FROM accounts WHERE id = ?", (row["harness_account"],)
        ) as cur:
            found = await cur.fetchone()
        provider = found["provider"] if found else provider
    switch = {
        "from": {
            "provider": provider,
            "model": pin.model if pin else None,
            "account_id": row["harness_account"],
        },
        "to": {
            "provider": leased["provider"],
            "model": None,
            "account_id": leased["id"],
            "account_name": leased["name"],
        },
        "reason": reason,
        "attempt": attempt,
    }
    async with tx.conn.execute(
        "SELECT id, definition FROM task_attempts WHERE task_id = ? AND attempt = ?"
        " ORDER BY id DESC LIMIT 1",
        (task_id, attempt),
    ) as cur:
        snapshot = await cur.fetchone()
    if snapshot is None:
        return None
    definition = json.loads(snapshot["definition"])
    if (definition.get("model_switch") or {}).get("to", {}).get("account_id") == leased[
        "id"
    ]:
        return None
    definition["model_switch"] = switch
    await tx.conn.execute(
        "UPDATE task_attempts SET definition = ? WHERE id = ?",
        (json.dumps(definition), snapshot["id"]),
    )
    await record(
        tx, f"agent:{agent_id}", "task", task_id, "task.model_switched", switch
    )
    return switch


async def attempt_model_switch(
    conn: aiosqlite.Connection, task_id: str, attempt: int
) -> dict[str, Any] | None:
    """The `model_switch` the attempt snapshot carries, None when the pin held."""
    async with conn.execute(
        "SELECT definition FROM task_attempts WHERE task_id = ? AND attempt = ?"
        " ORDER BY id DESC LIMIT 1",
        (task_id, attempt),
    ) as cur:
        row = await cur.fetchone()
    return json.loads(row["definition"]).get("model_switch") if row else None


async def _restriction_met(
    tx: Tx,
    providers: list[str],
    runtime_providers: list[str] | None,
    agent_id: str | None = None,
    account_id: str | None = None,
) -> bool:
    """Can the caller start a task allowing `providers` now: the runtime applies one
    of them and either the caller already holds an engine lease of one, or an account
    of one is usable (available or past its reset, below `max_leases`; an
    unreachable local model is disabled by its probe, so it never counts). With
    `account_id` (a harness account pin) only that account counts, held or usable."""
    wanted = sorted(set(providers) & set(runtime_providers or []))
    if not wanted:
        return False
    async with tx.conn.execute(
        "SELECT EXISTS (SELECT 1 FROM account_leases l JOIN accounts a"  # noqa: S608
        "  ON a.id = l.account_id WHERE l.agent_id = ? AND l.role = 'engine'"
        "  AND l.released_at IS NULL"
        "  AND a.provider IN (SELECT value FROM json_each(?))"
        "  AND (? IS NULL OR a.id = ?))"
        " OR EXISTS (SELECT 1 FROM accounts a"
        " WHERE a.provider IN (SELECT value FROM json_each(?))"
        "  AND (? IS NULL OR a.id = ?)"
        "  AND (a.state = 'available'"
        "   OR (a.state = 'exhausted' AND a.exhausted_until <= ?))"
        f"  AND {LEASE_BLOCK_SQL} IS NULL"
        "  AND (a.max_leases IS NULL OR a.max_leases >"
        "   (SELECT count(*) FROM account_leases l"
        "    WHERE l.account_id = a.id AND l.released_at IS NULL)))",
        (
            agent_id,
            json.dumps(wanted),
            account_id,
            account_id,
            json.dumps(wanted),
            account_id,
            account_id,
            ts(tx.now),
        ),
    ) as cur:
        row = await cur.fetchone()
    return bool(row and row[0])


async def manages_accounts(conn: aiosqlite.Connection) -> bool:
    """Does the fleet manage model accounts at all (else claims are never gated)?"""
    async with conn.execute(
        "SELECT EXISTS (SELECT 1 FROM accounts"
        " WHERE provider IN (SELECT value FROM json_each(?)))",
        (json.dumps(sorted(ENGINE_PROVIDERS)),),
    ) as cur:
        row = await cur.fetchone()
    return bool(row and row[0])


async def _record_runtime_providers(
    tx: Tx, agent_id: str, runtime_providers: list[str]
) -> bool:
    """Store the providers the agent's runtime applies (its latest claim). True
    when that adds a provider no live agent declared before."""
    wanted = sorted(set(runtime_providers))
    before = await _served_providers(tx.conn)
    async with tx.conn.execute(
        "SELECT runtime_providers FROM agents WHERE id = ?", (agent_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None or (row[0] and json.loads(row[0]) == wanted):
        return False
    await tx.conn.execute(
        "UPDATE agents SET runtime_providers = ? WHERE id = ?",
        (json.dumps(wanted), agent_id),
    )
    return before is not None and not set(wanted) <= before


async def _served_providers(conn: aiosqlite.Connection) -> set[str] | None:
    """Providers some live (active) agent's runtime applies, as declared on its
    claims; None when no live agent declared any (older workers): then every
    provider counts, as before."""
    async with conn.execute(
        "SELECT DISTINCT p.value FROM agents a, json_each(a.runtime_providers) p"
        " WHERE a.state = 'active' AND a.runtime_providers IS NOT NULL"
    ) as cur:
        rows = await cur.fetchall()
    return {r[0] for r in rows} or None


async def engine_capacity(tx: Tx) -> list[list[Any]]:
    """`[provider, slots, account_id]` of each usable engine account, cloud before
    the local model: how many more tasks it can serve now. Slots are the free lease slots plus
    the engine leases held by agents with no claimed task (they can take one at
    once); None means no lease limit. Accounts with no slot are left out, and so are
    accounts of a provider no live agent's runtime can apply (a `custom` account no
    worker runs is no capacity)."""
    served = await _served_providers(tx.conn)
    async with tx.conn.execute(
        "SELECT a.id, a.provider, a.max_leases,"  # noqa: S608
        " (SELECT count(*) FROM account_leases l WHERE l.account_id = a.id"
        "  AND l.released_at IS NULL) AS active,"
        " (SELECT count(*) FROM account_leases l WHERE l.account_id = a.id"
        "  AND l.released_at IS NULL AND l.role = 'engine' AND NOT EXISTS ("
        "   SELECT 1 FROM tasks t WHERE t.state = 'claimed'"
        "   AND t.assignee_agent_id = l.agent_id)) AS idle"
        " FROM accounts a WHERE (a.state = 'available'"
        "  OR (a.state = 'exhausted' AND a.exhausted_until <= ?))"
        f" AND {LEASE_BLOCK_SQL} IS NULL"
        " ORDER BY a.priority DESC, a.id",
        (ts(tx.now),),
    ) as cur:
        rows = await cur.fetchall()
    out: list[list[Any]] = []
    for r in rows:
        if r["provider"] not in ENGINE_PROVIDERS:
            continue
        if served is not None and r["provider"] not in served:
            continue
        if r["max_leases"] is None:
            out.append([r["provider"], None, r["id"]])
            continue
        slots = max(0, r["max_leases"] - r["active"]) + r["idle"]
        if slots > 0:
            out.append([r["provider"], slots, r["id"]])
    out.sort(key=lambda c: c[0] == LOCAL_PROVIDER)  # stable: cloud first
    return out


async def _capacity_choice(
    tx: Tx, row: aiosqlite.Row, capacity: list[list[Any]]
) -> EngineChoice:
    """`engine_choice` over the accounts that have a slot now (waking, parking)."""
    return engine_choice(
        row,
        [
            EngineAccount(c[2], c[0], "usable")
            for c in capacity
            if c[1] is None or c[1] > 0
        ],
        await known_engine_accounts(tx.conn),
    )


async def can_serve(tx: Tx, row: aiosqlite.Row) -> bool:
    """Could any agent run this task's engine role now, under its effective provider
    set (pin preference included)?"""
    capacity = await engine_capacity(tx)
    choice = await _capacity_choice(tx, row, capacity)
    return any(choice.fits(c[0], c[2]) for c in capacity)


async def wake_waiting(tx: Tx, actor: str = "system") -> list[str]:
    """Return tasks waiting for an account (`awaiting_quota`, or the older
    `no_account`) to ready, highest priority first, but only as many as the usable
    engine accounts can serve now: each woken task takes one slot of an account its
    `account_providers` allow. No thundering herd: the rest keep waiting for the
    next release, refill or account change. Returns the woken task ids."""
    capacity = await engine_capacity(tx)
    if not capacity:
        return []
    async with tx.conn.execute(
        "SELECT id, account_providers, harness_model, harness_account FROM tasks"
        " WHERE state = 'blocked'"
        " AND block_reason IN (?, ?) ORDER BY priority DESC, created_at, id",
        QUOTA_WAIT_REASONS,
    ) as cur:
        rows = await cur.fetchall()
    woken: list[str] = []
    for r in rows:
        choice = await _capacity_choice(tx, r, capacity)
        slot = next(
            (
                c
                for c in capacity
                if (c[1] is None or c[1] > 0) and choice.fits(c[0], c[2])
            ),
            None,
        )
        if slot is None:
            continue
        if slot[1] is not None:
            slot[1] -= 1
        await unblock(tx, r["id"], actor)
        woken.append(r["id"])
    return woken


async def awaiting_quota(conn: aiosqlite.Connection, now: datetime | None) -> bool:
    """True when some engine account is held back and none is usable: a claim would
    only burn an attempt on a provider limit (one rule: `engine_pool`)."""
    return (await engine_pool(conn, now)).awaiting_quota


async def _check_engine_lease(tx: Tx, agent_id: str) -> None:
    """A claim whose engine lease sits on an account that is disabled, or exhausted
    with its reset ahead, cannot run: `409 lease_account_unusable`. The channel
    drops that account, leases again and recycles the runtime."""
    async with tx.conn.execute(
        "SELECT a.name, a.provider, a.state, a.exhausted_until FROM account_leases l"
        " JOIN accounts a ON a.id = l.account_id WHERE l.agent_id = ?"
        " AND l.role = 'engine' AND l.released_at IS NULL",
        (agent_id,),
    ) as cur:
        rows = await cur.fetchall()
    for r in rows:
        if r["provider"] == LOCAL_PROVIDER:
            continue
        held_back = r["state"] == "exhausted" and (
            not r["exhausted_until"]
            or datetime.fromisoformat(r["exhausted_until"]) > tx.now
        )
        if r["state"] == "disabled" or held_back:
            raise ConflictError(
                f"engine account {r['name']} is {r['state']}; lease another",
                code="lease_account_unusable",
            )
