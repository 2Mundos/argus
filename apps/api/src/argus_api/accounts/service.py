"""Account registry, leasing, exhaustion fallback, and usage (worker lifecycle 3.9).

The database holds the registry and leases; credentials come from the secret
store and are only ever handed to the agent holding a lease.
"""

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import aiosqlite
from argus_core.accounts import (
    Account,
    AccountCreate,
    AccountLease,
    AccountUpdate,
    ExhaustedReport,
    FallbackResult,
    LeaseRole,
    Usage,
    UsageReport,
)

from argus_api.db import Tx, ts
from argus_api.errors import ConflictError, ForbiddenError, NotFoundError
from argus_api.events import record
from argus_api.secrets import SecretStore, ref_name
from argus_api.tasks import service as tasks

DEFAULT_COOLDOWN = timedelta(hours=1)
NO_ACCOUNT = "no_account"


class NoAccountError(ConflictError):
    code = NO_ACCOUNT


# -- registry ------------------------------------------------------------------------


async def get_account(
    conn: aiosqlite.Connection, account_id: str, now: datetime
) -> Account:
    async with conn.execute(
        _SELECT + " WHERE a.id = ?", (ts(now - timedelta(days=1)), account_id)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"account not found: {account_id}")
    return _to_account(row)


async def list_accounts(conn: aiosqlite.Connection, now: datetime) -> list[Account]:
    async with conn.execute(
        _SELECT + " ORDER BY a.provider, a.priority DESC, a.name",
        (ts(now - timedelta(days=1)),),
    ) as cur:
        return [_to_account(r) for r in await cur.fetchall()]


async def create_account(
    tx: Tx,
    account_id: str,
    body: AccountCreate,
    secret_ref: str,
    actor: str,
    store: SecretStore,
) -> Account:
    name = ref_name(secret_ref)
    missing = name is None or store.get(name) is None
    now = ts(tx.now)
    await tx.conn.execute(
        "INSERT INTO accounts (id, name, provider, kind, secret_ref, priority, max_leases,"
        " state, disabled_reason, auto_disabled, labels, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            account_id,
            body.name,
            body.provider,
            body.kind,
            secret_ref,
            body.priority,
            body.max_leases,
            "disabled" if missing else "available",
            f"secret not found: {secret_ref}" if missing else None,
            int(missing),
            json.dumps(body.labels),
            now,
            now,
        ),
    )
    await record(
        tx, actor, "account", account_id, "account.created", {"provider": body.provider}
    )
    if not missing:
        await release_waiting_tasks(tx)
    return await get_account(tx.conn, account_id, tx.now)


async def update_account(
    tx: Tx, account_id: str, body: AccountUpdate, actor: str
) -> Account:
    row = await _row(tx.conn, account_id)
    fields: dict[str, Any] = {}
    if body.name is not None and body.name != row["name"]:
        async with tx.conn.execute(
            "SELECT 1 FROM accounts WHERE name = ?", (body.name,)
        ) as cur:
            if await cur.fetchone():
                raise ConflictError(
                    f"an account named {body.name!r} already exists", code="name_taken"
                )
        fields["name"] = body.name
    if body.priority is not None:
        fields["priority"] = body.priority
    if "max_leases" in body.model_fields_set:
        fields["max_leases"] = body.max_leases
    if body.labels is not None:
        fields["labels"] = json.dumps(body.labels)
    if body.enabled is False and row["state"] != "disabled":
        fields |= {
            "state": "disabled",
            "disabled_reason": f"disabled by {actor}",
            "exhausted_until": None,
            "auto_disabled": 0,
        }
    elif body.enabled is True and row["state"] == "disabled":
        fields |= {"state": "available", "disabled_reason": None, "auto_disabled": 0}
    if fields:
        fields["updated_at"] = ts(tx.now)
        assignments = ", ".join(f"{k} = ?" for k in fields)
        await tx.conn.execute(
            f"UPDATE accounts SET {assignments} WHERE id = ?",  # noqa: S608 - code-controlled keys
            (*fields.values(), account_id),
        )
        await record(
            tx,
            actor,
            "account",
            account_id,
            "account.updated",
            {"fields": sorted(fields)},
        )
        if fields.get("state") == "available":
            await release_waiting_tasks(tx)
    return await get_account(tx.conn, account_id, tx.now)


async def delete_account(
    tx: Tx, account_id: str, confirm_name: str, actor: str
) -> str | None:
    """Remove an account, its usage history and its ended leases. `confirm_name`
    must be the account's current name, so a stale or wrong target is refused;
    an account with an active lease is refused.

    Returns the name of the Argus-generated secret that is now unreferenced and
    should be removed by the caller, or None. The secret store is not part of
    this SQL transaction; the caller coordinates the two."""
    row = await _row(tx.conn, account_id)
    if confirm_name != row["name"]:
        raise ConflictError(
            "confirmation does not match the account's current name",
            code="name_mismatch",
        )
    async with tx.conn.execute(
        "SELECT count(*) FROM account_leases WHERE account_id = ? AND released_at IS NULL",
        (account_id,),
    ) as cur:
        active = (await cur.fetchone() or (0,))[0]
    if active:
        raise ConflictError(
            f"account has {active} active lease(s); wait for release or disable it",
            code="account_in_use",
        )
    await tx.conn.execute(
        "DELETE FROM account_usage WHERE account_id = ?", (account_id,)
    )
    await tx.conn.execute(
        "DELETE FROM account_leases WHERE account_id = ?", (account_id,)
    )
    await tx.conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    # Only an entry Argus generated (`account-*`) and no other account still
    # references; an operator-managed ref, or one a sibling uses, stays.
    secret = ref_name(row["secret_ref"])
    async with tx.conn.execute(
        "SELECT count(*) FROM accounts WHERE secret_ref = ?", (row["secret_ref"],)
    ) as cur:
        shared = (await cur.fetchone() or (0,))[0]
    removable = (
        secret if secret and secret.startswith("account-") and not shared else None
    )
    await record(
        tx,
        actor,
        "account",
        account_id,
        "account.deleted",
        {
            "name": row["name"],
            "provider": row["provider"],
            "secret_removed": removable is not None,
        },
    )
    return removable


async def reconcile_secrets(tx: Tx, store: SecretStore) -> None:
    """Disable accounts whose secret is missing; re-enable the ones Argus disabled
    for that reason once it is back. Runs at startup and after a reload."""
    async with tx.conn.execute("SELECT * FROM accounts") as cur:
        rows = await cur.fetchall()
    for row in rows:
        name = ref_name(row["secret_ref"])
        present = name is not None and store.get(name) is not None
        if not present and row["state"] != "disabled":
            await _disable(tx, row["id"], f"secret not found: {row['secret_ref']}")
        elif present and row["state"] == "disabled" and row["auto_disabled"]:
            await tx.conn.execute(
                "UPDATE accounts SET state = 'available', disabled_reason = NULL,"
                " auto_disabled = 0, updated_at = ? WHERE id = ?",
                (ts(tx.now), row["id"]),
            )
            await record(tx, "system", "account", row["id"], "account.available")
    await release_waiting_tasks(tx)


# -- leasing ---------------------------------------------------------------------------


async def lease(
    tx: Tx,
    agent_id: str,
    providers: list[str],
    store: SecretStore,
    role: LeaseRole = "engine",
) -> AccountLease:
    """Lease the best usable account among `providers`. An agent holds one lease
    per role (`engine`: its harness; `executor`: a delegated CLI); a new lease
    replaces the previous one of the same role. Raises NoAccountError."""
    await release(tx, agent_id, "replaced", role)
    result = await _try_lease(tx, agent_id, providers, store, role=role)
    if result is None:
        reset = await earliest_reset(tx, providers)
        raise NoAccountError(
            f"no usable account for providers {', '.join(providers)}"
            + (f"; earliest reset at {reset.isoformat()}" if reset else ""),
        )
    return result


async def exhausted(
    tx: Tx,
    agent_id: str,
    account_id: str,
    report: ExhaustedReport,
    store: SecretStore,
    cooldown: timedelta = DEFAULT_COOLDOWN,
) -> FallbackResult:
    """The agent hit a quota on its leased account: mark it exhausted, then fall
    back to another account, same provider first. With nothing left, the agent's
    task (if given) is blocked as `no_account` and resumes when one frees up."""
    held = await _lease_on(tx.conn, agent_id, account_id, report.role)
    if held is None:
        raise ConflictError(
            f"agent {agent_id} does not hold a lease on account {account_id}",
            code="not_leased",
        )
    role = held["role"]
    account = await _row(tx.conn, account_id)
    until = (
        report.until if report.until and report.until > tx.now else tx.now + cooldown
    )
    await tx.conn.execute(
        "UPDATE accounts SET state = 'exhausted', exhausted_until = ?, updated_at = ?"
        " WHERE id = ? AND state != 'disabled'",
        (ts(until), ts(tx.now), account_id),
    )
    await record(
        tx,
        f"agent:{agent_id}",
        "account",
        account_id,
        "account.exhausted",
        {"until": until.isoformat(), "reason": report.reason},
    )
    await release(tx, agent_id, "exhausted", role)
    requested: list[str] = json.loads(held["providers"])
    preference = [
        account["provider"],
        *(p for p in requested if p != account["provider"]),
    ]
    next_lease = await _try_lease(
        tx, agent_id, preference, store, provider_first=True, role=role
    )
    if next_lease is not None:
        return FallbackResult(lease=next_lease)
    blocked = False
    if report.task_id is not None and report.attempt is not None:
        await tasks.block(tx, agent_id, report.task_id, report.attempt, NO_ACCOUNT)
        blocked = True
    return FallbackResult(
        lease=None,
        earliest_reset=await earliest_reset(tx, preference),
        task_blocked=blocked,
    )


async def release(
    tx: Tx, agent_id: str, reason: str, role: LeaseRole | None = None
) -> None:
    """End the agent's active leases: one role, or all of them."""
    await tx.conn.execute(
        "UPDATE account_leases SET released_at = ?, release_reason = ?"
        " WHERE agent_id = ? AND released_at IS NULL AND (? IS NULL OR role = ?)",
        (ts(tx.now), reason, agent_id, role, role),
    )


async def earliest_reset(tx: Tx, providers: list[str]) -> datetime | None:
    marks = ",".join("?" * len(providers))
    async with tx.conn.execute(
        f"SELECT min(exhausted_until) FROM accounts WHERE state = 'exhausted'"  # noqa: S608
        f" AND provider IN ({marks})",
        providers,
    ) as cur:
        row = await cur.fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


async def recover(tx: Tx) -> list[str]:
    """Exhausted accounts past their reset become available; waiting tasks resume."""
    async with tx.conn.execute(
        "SELECT id FROM accounts WHERE state = 'exhausted' AND exhausted_until <= ?",
        (ts(tx.now),),
    ) as cur:
        ids = [r["id"] for r in await cur.fetchall()]
    for account_id in ids:
        await tx.conn.execute(
            "UPDATE accounts SET state = 'available', exhausted_until = NULL,"
            " updated_at = ? WHERE id = ?",
            (ts(tx.now), account_id),
        )
        await record(tx, "system", "account", account_id, "account.available")
    if ids:
        await release_waiting_tasks(tx)
    return ids


async def release_waiting_tasks(tx: Tx) -> list[str]:
    """Unblock tasks blocked for lack of an account, if any account is usable.
    They return to ready; whichever agent claims them leases afresh."""
    async with tx.conn.execute(
        "SELECT 1 FROM accounts WHERE state = 'available' LIMIT 1"
    ) as cur:
        if await cur.fetchone() is None:
            return []
    async with tx.conn.execute(
        "SELECT id FROM tasks WHERE state = 'blocked' AND block_reason = ? ORDER BY id",
        (NO_ACCOUNT,),
    ) as cur:
        ids = [r["id"] for r in await cur.fetchall()]
    for task_id in ids:
        await tasks.unblock(tx, task_id, "system")
    return ids


# -- usage and credentials -----------------------------------------------------------------


async def record_usage(
    tx: Tx, account_id: str, agent_id: str | None, report: UsageReport
) -> None:
    await _row(tx.conn, account_id)
    await tx.conn.execute(
        "INSERT INTO account_usage (account_id, agent_id, task_id, input_tokens,"
        " output_tokens, cost_usd, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            account_id,
            agent_id,
            report.task_id,
            report.input_tokens,
            report.output_tokens,
            report.cost_usd,
            ts(tx.now),
        ),
    )


async def secret_name_for_update(
    conn: aiosqlite.Connection, account_id: str, agent_id: str | None
) -> str:
    """The secret an agent (holding a lease on this account) or an admin
    (`agent_id=None`) may overwrite, e.g. with refreshed OAuth tokens."""
    row = await _row(conn, account_id)
    if agent_id is not None and await _lease_on(conn, agent_id, account_id) is None:
        raise ForbiddenError("only the agent holding a lease may update its credential")
    name = ref_name(row["secret_ref"])
    if name is None:
        raise ConflictError(f"account {account_id} has no writable secret reference")
    return name


async def credential_renewed(
    tx: Tx, account_id: str, kind: str, mode: str, actor: str
) -> None:
    """The account's credential was replaced by an admin. Name, priority, leases,
    labels and enabled/disabled state stay; the derived kind follows the new
    credential. The event names the credential's type, never a value."""
    row = await _row(tx.conn, account_id)
    await tx.conn.execute(
        "UPDATE accounts SET kind = ?, updated_at = ? WHERE id = ?",
        (kind, ts(tx.now), account_id),
    )
    await record(
        tx,
        actor,
        "account",
        account_id,
        "account.credential_renewed",
        {"mode": mode, "kind_changed": row["kind"] != kind},
    )


async def disable_for_write_failure(tx: Tx, account_id: str, error: str) -> None:
    """A refreshed credential that cannot be persisted must not be silently kept."""
    await _disable(tx, account_id, f"credential write failed: {error}", auto=False)


# -- internals -----------------------------------------------------------------------------


async def _try_lease(
    tx: Tx,
    agent_id: str,
    providers: list[str],
    store: SecretStore,
    *,
    provider_first: bool = False,
    role: LeaseRole = "engine",
) -> AccountLease | None:
    """Pick by priority across providers (provider order breaks ties), or, when
    falling back, by provider order first so an agent stays on the same provider."""
    order = {p: i for i, p in enumerate(providers)}
    marks = ",".join("?" * len(providers))
    sql = (  # only placeholders are interpolated
        "SELECT a.*, (SELECT count(*) FROM account_leases l WHERE l.account_id = a.id"  # noqa: S608
        "  AND l.released_at IS NULL) AS active FROM accounts a"
        f" WHERE a.provider IN ({marks}) AND (a.state = 'available'"
        "  OR (a.state = 'exhausted' AND a.exhausted_until <= ?))"
    )
    async with tx.conn.execute(
        sql,
        (*providers, ts(tx.now)),
    ) as cur:
        rows = await cur.fetchall()
    candidates = sorted(
        (r for r in rows if r["max_leases"] is None or r["active"] < r["max_leases"]),
        key=lambda r: (
            (order[r["provider"]], -r["priority"])
            if provider_first
            else (-r["priority"], order[r["provider"]]),
            r["active"],
            r["id"],
        ),
    )
    for row in candidates:
        name = ref_name(row["secret_ref"])
        credential = store.get(name) if name else None
        if credential is None:
            await _disable(tx, row["id"], f"secret not found: {row['secret_ref']}")
            continue
        if row["state"] == "exhausted":  # reset time passed; recover() has not run yet
            await tx.conn.execute(
                "UPDATE accounts SET state = 'available', exhausted_until = NULL,"
                " updated_at = ? WHERE id = ?",
                (ts(tx.now), row["id"]),
            )
        lease_id = str(uuid4())
        await tx.conn.execute(
            "INSERT INTO account_leases (id, account_id, agent_id, providers, leased_at,"
            " role) VALUES (?, ?, ?, ?, ?, ?)",
            (lease_id, row["id"], agent_id, json.dumps(providers), ts(tx.now), role),
        )
        await record(
            tx,
            f"agent:{agent_id}",
            "account",
            row["id"],
            "account.leased",
            {"agent_id": agent_id, "role": role},
        )
        return AccountLease(
            lease_id=lease_id,
            account_id=row["id"],
            name=row["name"],
            provider=row["provider"],
            kind=row["kind"],
            role=role,
            credential=credential,
        )
    return None


async def _disable(tx: Tx, account_id: str, reason: str, *, auto: bool = True) -> None:
    await tx.conn.execute(
        "UPDATE accounts SET state = 'disabled', disabled_reason = ?, exhausted_until = NULL,"
        " auto_disabled = ?, updated_at = ? WHERE id = ?",
        (reason, int(auto), ts(tx.now), account_id),
    )
    await record(
        tx, "system", "account", account_id, "account.disabled", {"reason": reason}
    )


async def _lease_on(
    conn: aiosqlite.Connection,
    agent_id: str,
    account_id: str,
    role: LeaseRole | None = None,
) -> aiosqlite.Row | None:
    """The agent's active lease on this account (of `role`, or any role)."""
    async with conn.execute(
        "SELECT * FROM account_leases WHERE agent_id = ? AND account_id = ?"
        " AND released_at IS NULL AND (? IS NULL OR role = ?)",
        (agent_id, account_id, role, role),
    ) as cur:
        return await cur.fetchone()


async def _row(conn: aiosqlite.Connection, account_id: str) -> aiosqlite.Row:
    async with conn.execute(
        "SELECT * FROM accounts WHERE id = ?", (account_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"account not found: {account_id}")
    return row


_USAGE = (
    "coalesce(sum(u.input_tokens), 0), coalesce(sum(u.output_tokens), 0),"
    " coalesce(sum(u.cost_usd), 0.0)"
)
_SELECT = (  # constant fragments only
    "SELECT a.*,"  # noqa: S608
    " (SELECT count(*) FROM account_leases l WHERE l.account_id = a.id"
    "  AND l.released_at IS NULL) AS active_leases,"
    " (SELECT json_array(" + _USAGE + ") FROM account_usage u"
    "  WHERE u.account_id = a.id AND u.at > ?) AS usage_24h,"
    " (SELECT json_array(" + _USAGE + ") FROM account_usage u"
    "  WHERE u.account_id = a.id) AS usage_total"
    " FROM accounts a"
)


def _usage(raw: str) -> Usage:
    i, o, c = json.loads(raw)
    return Usage(input_tokens=i, output_tokens=o, cost_usd=c)


def _to_account(row: aiosqlite.Row) -> Account:
    data = dict(row)
    data["labels"] = json.loads(data["labels"])
    data["usage_24h"] = _usage(data["usage_24h"])
    data["usage_total"] = _usage(data["usage_total"])
    return Account.model_validate(data)
