"""Account registry, leasing, exhaustion fallback, and usage (worker lifecycle 3.9).

The database holds the registry and leases; credentials come from the secret
store and are only ever handed to the agent holding a lease.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import aiosqlite
from argus_core.accounts import (
    LOCAL_PROVIDER,
    Account,
    AccountCreate,
    AccountLease,
    AccountUpdate,
    ExhaustedReport,
    FallbackResult,
    LeaseRole,
    QuotaWindow,
    Usage,
    UsageReport,
    fallback_tier,
    quota_exempt,
    role_accepts,
)
from argus_core.custom_accounts import (
    PROVIDER as CUSTOM_PROVIDER,
)
from argus_core.custom_accounts import (
    CustomConfig,
    LeaseSettings,
    lease_settings,
    serves_role,
)
from argus_core.quota import QuotaHit, classify_quota, sanitize_notice
from argus_core.tasks import Task

from argus_api.accounts import usage
from argus_api.accounts.eligibility import engine_pool
from argus_api.db import Tx, ts
from argus_api.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationFailedError,
)
from argus_api.events import record
from argus_api.modeltrace import service as modeltrace
from argus_api.secrets import SecretStore, ref_name
from argus_api.tasks import service as tasks

log = logging.getLogger(__name__)
DEFAULT_COOLDOWN = timedelta(hours=1)
NO_ACCOUNT = "no_account"  # the lease error code; tasks wait as `awaiting_quota`
# The one reason a local account is disabled by Argus itself, and the only one the
# local probe may undo (a missing secret stays for `reconcile_secrets`).
LOCAL_UNREACHABLE = "local model unreachable"


class NoAccountError(ConflictError):
    code = NO_ACCOUNT


# -- registry ------------------------------------------------------------------------


async def get_account(
    conn: aiosqlite.Connection, account_id: str, now: datetime
) -> Account:
    async with conn.execute(
        _SELECT + " WHERE a.id = :id",
        {"since": ts(now - timedelta(days=1)), "id": account_id},
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"account not found: {account_id}")
    return await _to_account(conn, row, now)


async def list_accounts(conn: aiosqlite.Connection, now: datetime) -> list[Account]:
    async with conn.execute(
        _SELECT + " ORDER BY a.provider, a.priority DESC, a.name",
        {"since": ts(now - timedelta(days=1))},
    ) as cur:
        rows = await cur.fetchall()
    return [await _to_account(conn, r, now) for r in rows]


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
        " state, disabled_reason, auto_disabled, labels, created_at, updated_at,"
        " token_limits, config) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            json.dumps(body.token_limits) if body.token_limits else None,
            body.config.model_dump_json() if body.config else None,
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
    if "billing_day" in body.model_fields_set:
        fields["billing_day"] = body.billing_day
    if "token_limits" in body.model_fields_set:
        fields["token_limits"] = (
            json.dumps(body.token_limits) if body.token_limits else None
        )
    if body.config is not None:
        if row["provider"] != CUSTOM_PROVIDER:
            raise ValidationFailedError("config applies to custom accounts only")
        fields["config"] = body.config.model_dump_json()
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
    """Remove an account and its ended leases. Usage rows are detached
    (`account_id` and `role` cleared) so finished attempts keep their outcome
    (account usage 3.10) and show in Cost as unattributed. `confirm_name` must
    be the account's current name, so a stale or wrong target is refused; an
    account with an active lease is refused.

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
    # Detach, do not delete: attempt outcomes stay for coverage and Cost.
    await tx.conn.execute(
        "UPDATE account_usage SET account_id = NULL, role = NULL WHERE account_id = ?",
        (account_id,),
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
        elif (
            present
            and row["state"] == "disabled"
            and row["auto_disabled"]
            # An unreachable local model is only brought back by its own probe.
            and not (row["disabled_reason"] or "").startswith(LOCAL_UNREACHABLE)
        ):
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
    runtime_providers: list[str],
    store: SecretStore,
    role: LeaseRole = "engine",
    *,
    reuse_held: bool = False,
    task_id: str,
    attempt: int,
) -> AccountLease:
    """Lease the best usable account for `role`, ranked globally by priority (then
    least leased, then name); the local model is a tier below every cloud account,
    so it serves only when none of them can, unless the task is trivial, which tries
    it first and takes the cloud in the same call when it cannot serve. `runtime_providers` is only what the
    runtime can technically apply, never an order or allowlist. An agent holds one lease per
    role (`engine`: its harness; `executor`: a delegated CLI; `repository`); a new
    lease replaces the previous one of the same role. Raises NoAccountError."""
    if not runtime_providers:
        raise ValidationFailedError("runtime_providers must be non-empty")
    await tasks.check_lease(tx, task_id, attempt, agent_id)
    allowed = await _narrowed(tx, task_id, role, runtime_providers)
    await release(tx, agent_id, "replaced", role)
    result = await _try_lease(
        tx,
        agent_id,
        store,
        role,
        allowed,
        reuse_held=reuse_held,
        prefer_local=await _prefer_local(tx, task_id, role),
        only_account=await _pinned_account(tx, task_id, role),
    )
    if result is None:
        reset = await earliest_reset(tx, role, allowed)
        raise NoAccountError(
            f"no usable {role} account"
            + (f"; earliest reset at {reset.isoformat()}" if reset else ""),
        )
    return result


async def _holds_engine(tx: Tx, agent_id: str) -> bool:
    async with tx.conn.execute(
        "SELECT EXISTS (SELECT 1 FROM account_leases WHERE agent_id = ?"
        " AND role = 'engine' AND released_at IS NULL)",
        (agent_id,),
    ) as cur:
        row = await cur.fetchone()
    return bool(row and row[0])


async def _lease_available(
    tx: Tx,
    agent_id: str,
    task: aiosqlite.Row,
    runtime_providers: list[str],
    store: SecretStore,
) -> bool:
    """Would `lease` give this task an engine account now? The same narrowing,
    ranking and credential test, without writing."""
    allowed = await _narrowed(tx, task["id"], "engine", runtime_providers)
    ranked = _only(
        await _ranked(
            tx,
            agent_id,
            "engine",
            allowed,
            prefer_local=await _prefer_local(tx, task["id"], "engine"),
        ),
        await _pinned_account(tx, task["id"], "engine"),
    )
    return any((name := ref_name(r["secret_ref"])) and store.get(name) for r in ranked)


async def claim(
    tx: Tx,
    agent_id: str,
    capabilities: list[str],
    runtime_providers: list[str] | None,
    store: SecretStore,
    *,
    default_timeout_s: int = 0,
) -> tuple[Task | None, AccountLease | None]:
    """Claim a task and, when the agent holds no engine lease, reserve its account
    in the same transaction: a task is only claimed when an account leases for it
    now, so a claim never ends in a lease failure. An agent that already holds an
    engine lease keeps it (the worker re-checks the fit)."""
    reserve = (
        bool(runtime_providers)
        and await tasks.manages_accounts(tx.conn)
        and not await _holds_engine(tx, agent_id)
    )

    async def can_lease(tx: Tx, task: aiosqlite.Row) -> bool:
        return await _lease_available(
            tx, agent_id, task, runtime_providers or [], store
        )

    task = await tasks.claim(
        tx,
        agent_id,
        capabilities,
        default_timeout_s=default_timeout_s,
        runtime_providers=runtime_providers,
        can_lease=can_lease if reserve else None,
    )
    if task is None or not reserve:
        return task, None
    leased = await lease(
        tx,
        agent_id,
        runtime_providers or [],
        store,
        task_id=task.id,
        attempt=task.attempt,
    )
    return task, leased


async def exhausted(
    tx: Tx,
    agent_id: str,
    account_id: str,
    report: ExhaustedReport,
    store: SecretStore,
    cooldown: timedelta = DEFAULT_COOLDOWN,
) -> FallbackResult:
    """The agent hit a quota on its leased account: mark it exhausted, then fall
    back to the best remaining usable account of any provider (priority order, no
    preference for the previous provider). With nothing left, the agent's task (if
    given) is blocked as `awaiting_quota` and resumes when one frees up. The task
    keeps its attempt and failures either way."""
    if report.task_id is None or report.attempt is None:
        raise ConflictError("task_id and attempt are required")
    held = await _lease_on(tx.conn, agent_id, account_id, report.role)
    if held is None:
        raise ConflictError(
            f"agent {agent_id} does not hold a lease on account {account_id}",
            code="not_leased",
        )
    task_id = report.task_id
    attempt = report.attempt
    # Fence before any write: a stale or expired claim must not exhaust anything.
    await tasks.check_lease(tx, task_id, attempt, agent_id)
    async with tx.conn.execute(
        "SELECT provider FROM accounts WHERE id = ?", (account_id,)
    ) as cur:
        account_row = await cur.fetchone()
    if account_row is None or account_row["provider"] not in report.runtime_providers:
        raise ConflictError(
            "the exhausted account provider must be declared in runtime_providers",
            code="provider_not_declared",
        )
    role = held["role"]
    reason = sanitize_notice(report.reason)
    detail = _reset_detail(
        report.until,
        report.reset_status,
        report.reset_text,
        report.source,
        report.reason_code,
    )
    previous, reset_at = await _mark_exhausted(
        tx, agent_id, account_id, report.until, reason, cooldown, detail
    )
    await release(tx, agent_id, "exhausted", role)
    allowed = await _narrowed(tx, task_id, role, report.runtime_providers)
    next_candidate = _only(
        await _ranked(
            tx,
            agent_id,
            role,
            allowed,
            prefer_local=await _prefer_local(tx, task_id, role),
        ),
        await _pinned_account(tx, task_id, role),
    )
    # The switch itself happens at the next claim, through a lease bound to it.
    await _failover(
        tx,
        agent_id,
        task_id,
        attempt,
        previous,
        next_candidate[0]["name"] if next_candidate else None,
        reason,
        account_id,
        next_candidate[0]["id"] if next_candidate else None,
        detail | {"reset_at": reset_at},
    )
    if next_candidate:
        return FallbackResult(
            lease=None,
            next_account_id=next_candidate[0]["id"],
            next_account_name=next_candidate[0]["name"],
            next_provider=next_candidate[0]["provider"],
        )
    await tasks.block(tx, agent_id, task_id, attempt, tasks.AWAITING_QUOTA)
    return FallbackResult(
        lease=None,
        earliest_reset=await earliest_reset(tx, role, allowed),
        task_blocked=True,
    )


async def fail_task(
    tx: Tx,
    agent_id: str,
    task_id: str,
    attempt: int,
    error: str,
    reason: str,
    store: SecretStore,
    cooldown: timedelta = DEFAULT_COOLDOWN,
) -> Task:
    """`POST /tasks/{id}/fail`. A `provider_failure` whose error is a known quota
    notice of the provider the agent's engine account belongs to is not a task
    failure: the account is exhausted, the agent moves to the next one, and the task
    returns to `ready` (or waits as `awaiting_quota` when nothing is left) with
    `failures` unchanged. Anything else fails the attempt as before."""
    if reason == tasks.RUNTIME_SWITCH_FAILED:
        return await _switch_failed(tx, agent_id, task_id, attempt, error)
    held = None
    if reason == "provider_failure":
        async with tx.conn.execute(
            "SELECT l.providers, a.id, a.name, a.provider FROM account_leases l"
            " JOIN accounts a ON a.id = l.account_id WHERE l.agent_id = ?"
            " AND l.role = 'engine' AND l.released_at IS NULL",
            (agent_id,),
        ) as cur:
            held = await cur.fetchone()
    hit = classify_quota(error, held["provider"], tx.now) if held else None
    if held is None or hit is None:
        return await tasks.fail(tx, agent_id, task_id, attempt, error, reason)
    await tasks.check_lease(tx, task_id, attempt, agent_id, allow_expired=True)
    detail = _hit_detail(
        hit, "structured" if error.lstrip().startswith("{") else "notice"
    )
    previous, reset_at = await _mark_exhausted(
        tx, agent_id, held["id"], hit.until, hit.reason, cooldown, detail
    )
    await release(tx, agent_id, "exhausted", "engine")
    runtime = await _narrowed(
        tx, task_id, "engine", json.loads(held["providers"]) or None
    )
    # No lease here: the next claim leases (bound to it) and the channel applies it.
    next_candidate = _only(
        await _ranked(
            tx,
            agent_id,
            "engine",
            runtime,
            prefer_local=await _prefer_local(tx, task_id, "engine"),
        ),
        await _pinned_account(tx, task_id, "engine"),
    )
    task = await tasks.pause_for_quota(
        tx, agent_id, task_id, attempt, hit.reason, wait=not next_candidate
    )
    await _failover(
        tx,
        agent_id,
        task_id,
        attempt,
        previous,
        next_candidate[0]["name"] if next_candidate else None,
        hit.reason,
        held["id"],
        next_candidate[0]["id"] if next_candidate else None,
        detail | {"reset_at": reset_at},
    )
    return await tasks.get_task(tx.conn, task.id)


async def _switch_failed(
    tx: Tx, agent_id: str, task_id: str, attempt: int, error: str
) -> Task:
    """The old runtime did not stop, so the new account was never applied: drop the
    engine lease, record it, and block the task without spending an attempt."""
    await tasks.check_lease(tx, task_id, attempt, agent_id, allow_expired=True)
    async with tx.conn.execute(
        "SELECT a.id, a.name FROM account_leases l JOIN accounts a"
        " ON a.id = l.account_id WHERE l.agent_id = ? AND l.role = 'engine'"
        " AND l.released_at IS NULL",
        (agent_id,),
    ) as cur:
        held = await cur.fetchone()
    note = sanitize_notice(error)
    await release(tx, agent_id, "switch_failed", "engine")
    await record(
        tx,
        f"agent:{agent_id}",
        "task",
        task_id,
        "account.switch_failed",
        {
            "attempt": attempt,
            "account": held["name"] if held else None,
            "error": note,
        },
    )
    return await tasks.block_switch_failed(tx, agent_id, task_id, attempt, note)


async def switch_applied(
    tx: Tx, agent_id: str, task_id: str, attempt: int, account_id: str
) -> None:
    """The channel applied the leased account and recycled the runtime: the failover
    trail of that attempt turns from `pending` to `applied`."""
    await tasks.check_lease(tx, task_id, attempt, agent_id)
    row = await _row(tx.conn, account_id)
    await record(
        tx,
        f"agent:{agent_id}",
        "task",
        task_id,
        "account.switch_applied",
        {"attempt": attempt, "account": row["name"]},
    )


async def mark_awaiting_quota(
    tx: Tx, agent_id: str, capabilities: list[str], runtime_providers: list[str]
) -> list[str]:
    """The agent found no usable account, so it never claims. Mark the `ready` tasks
    it could have claimed as `awaiting_quota` (refill time shown from the exhausted
    accounts). Refused while any account is usable: nobody can park work by asking."""
    if not runtime_providers:
        raise ValidationFailedError("runtime_providers must be non-empty")
    if await _ranked(tx, agent_id, "engine", runtime_providers):
        raise ConflictError("a usable account exists", code="account_available")
    return await tasks.await_quota_ready(tx, agent_id, capabilities)


async def _mark_exhausted(
    tx: Tx,
    agent_id: str,
    account_id: str,
    until: datetime | None,
    reason: str,
    cooldown: timedelta,
    detail: dict[str, Any] | None = None,
) -> tuple[str, datetime | None]:
    """Exhaust the account until the reported reset (else the cooldown); never
    shorten an existing mark. Returns the account name and the mark it now has
    (None for a quota-exempt account). The event says whether the time was the
    provider's (`until_kind: stated`) or the cooldown.

    A quota-exempt provider (the local model) has no limit to hit: the report is
    recorded and the account stays as it is, available or not by its own probe."""
    row = await _row(tx.conn, account_id)
    if quota_exempt(row["provider"]):
        await record(
            tx,
            f"agent:{agent_id}",
            "account",
            account_id,
            "account.exhausted",
            {"until": None, "reason": reason, "quota_exempt": True} | (detail or {}),
        )
        return row["name"], None
    stated = bool(until and until > tx.now)
    new = until if until and stated else tx.now + cooldown
    current = row["exhausted_until"]
    if (
        row["state"] == "exhausted"
        and current
        and datetime.fromisoformat(current) > new
    ):
        new = datetime.fromisoformat(current)
    await tx.conn.execute(
        "UPDATE accounts SET state = 'exhausted', exhausted_until = ?, updated_at = ?"
        " WHERE id = ? AND state != 'disabled'",
        (ts(new), ts(tx.now), account_id),
    )
    await record(
        tx,
        f"agent:{agent_id}",
        "account",
        account_id,
        "account.exhausted",
        {
            "until": new.isoformat(),
            "until_kind": "stated" if stated else "cooldown",
            "reason": reason,
        }
        | (detail or {}),
    )
    return row["name"], new


async def _failover(
    tx: Tx,
    agent_id: str,
    task_id: str,
    attempt: int,
    from_name: str,
    to_name: str | None,
    reason: str,
    from_id: str,
    to_id: str | None,
    detail: dict[str, Any] | None = None,
) -> None:
    """The visible record of an account switch: an event on the task and one
    structured log line. Names only; no credential ever reaches either. `outcome`
    is `switched` (another account is ranked; applied at the next claim, `apply`
    pending) or `awaiting_quota` (none left)."""
    reset_at = (detail or {}).get("reset_at")
    data = {
        "from": from_name,
        "to": to_name,
        "reason": sanitize_notice(reason, 500),
        "task_id": task_id,
        "attempt": attempt,
        "outcome": "switched" if to_id else "awaiting_quota",
        "apply": "pending" if to_id else "not_needed",
        **{k: v for k, v in (detail or {}).items() if k != "reset_at"},
        "reset_at": reset_at.isoformat() if reset_at else None,
    }
    await record(tx, f"agent:{agent_id}", "task", task_id, "account.failover", data)
    if to_id is not None:  # a switch happened (model observability 3.4)
        await modeltrace.record_account_switch(
            tx, agent_id, task_id, attempt, from_id, to_id, reason
        )
    log.info(
        "account_failover %s",
        json.dumps({"event": "account_failover", **data}),
        extra={"event": "account_failover", **data},
    )


async def release(
    tx: Tx, agent_id: str, reason: str, role: LeaseRole | None = None
) -> None:
    """End the agent's active leases: one role, or all of them. A freed slot wakes
    as many quota-waiting tasks as can now be served (`tasks.wake_waiting`), except
    when the lease is only being `replaced` by a new one in the same call."""
    cur = await tx.conn.execute(
        "UPDATE account_leases SET released_at = ?, release_reason = ?"
        " WHERE agent_id = ? AND released_at IS NULL AND (? IS NULL OR role = ?)",
        (ts(tx.now), reason, agent_id, role, role),
    )
    if cur.rowcount and reason != "replaced":
        await tasks.wake_waiting(tx)


async def earliest_reset(
    tx: Tx, role: LeaseRole, runtime_providers: list[str] | None
) -> datetime | None:
    if role == "engine" and runtime_providers is None:
        return (await engine_pool(tx.conn, None)).next_reset
    async with tx.conn.execute(
        "SELECT provider, config, exhausted_until FROM accounts"
        " WHERE state = 'exhausted' AND exhausted_until IS NOT NULL"
    ) as cur:
        rows = await cur.fetchall()
    marks = [
        datetime.fromisoformat(r["exhausted_until"])
        for r in rows
        if role_accepts(role, r["provider"], runtime_providers) and _serves(r, role)
    ]
    return min(marks, default=None)


def _reset_detail(
    until: datetime | None,
    status: str | None,
    text: str | None,
    source: str | None,
    code: str | None,
) -> dict[str, Any]:
    """The provenance of a report: why it counts, and whether its reset time is the
    provider's (`stated`), unplaceable (`ambiguous`) or missing (`absent`)."""
    return {
        "reason_code": code or "quota_exhausted",
        "reset_status": status or ("stated" if until else "absent"),
        "reset_text": sanitize_notice(text, 120) if text else None,
        "source": source or "notice",
    }


def _hit_detail(hit: QuotaHit, source: str) -> dict[str, Any]:
    return _reset_detail(hit.until, hit.reset_status, hit.reset_text, source, hit.code)


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


async def sync_local_health(
    tx: Tx, reachable: bool | None, detail: str = ""
) -> list[str]:
    """The local model's own probe is the only availability signal for `local`
    accounts (they have no quota and no credential to lose): an unreachable server
    disables them, a reachable one brings back exactly the ones Argus disabled for
    that reason. Not probed (`None`) changes nothing. The probe itself runs before
    the transaction: no network call happens in here."""
    async with tx.conn.execute(
        "SELECT id, state, auto_disabled, disabled_reason FROM accounts"
        " WHERE provider = ?",
        (LOCAL_PROVIDER,),
    ) as cur:
        rows = list(await cur.fetchall())
    if reachable is None or not rows:
        return []
    changed: list[str] = []
    for row in rows:
        if not reachable and row["state"] != "disabled":
            await _disable(tx, row["id"], f"{LOCAL_UNREACHABLE}: {detail}"[:500])
            changed.append(row["id"])
        elif (
            reachable
            and row["state"] == "disabled"
            and row["auto_disabled"]
            and (row["disabled_reason"] or "").startswith(LOCAL_UNREACHABLE)
        ):
            await tx.conn.execute(
                "UPDATE accounts SET state = 'available', disabled_reason = NULL,"
                " auto_disabled = 0, updated_at = ? WHERE id = ?",
                (ts(tx.now), row["id"]),
            )
            await record(tx, "system", "account", row["id"], "account.available")
            changed.append(row["id"])
    if changed and reachable:
        await release_waiting_tasks(tx)
    return changed


async def release_waiting_tasks(tx: Tx) -> list[str]:
    """Unblock tasks waiting for quota (`awaiting_quota`, or the older `no_account`)
    when an account becomes usable: as many as the accounts can serve now, highest
    priority first (`tasks.wake_waiting`). Whichever agent claims one leases afresh;
    one that still finds nothing parks it again without spending an attempt."""
    return await tasks.wake_waiting(tx)


# -- usage and credentials -----------------------------------------------------------------


async def record_usage(
    tx: Tx, account_id: str, agent_id: str, report: UsageReport
) -> None:
    """One engine run on an account the agent holds (account usage 3.2 / 3.10)."""
    await _row(tx.conn, account_id)
    if await _lease_on(tx.conn, agent_id, account_id) is None:
        raise ForbiddenError("only an agent holding a lease may report its usage")
    attempt = await usage.resolve_attempt(
        tx.conn, agent_id, report.task_id, report.attempt
    )
    fields = report.model_dump(exclude={"task_id", "rate_limits", "attempt"})
    await usage.insert_usage(
        tx,
        account_id,
        agent_id,
        report.task_id,
        attempt=attempt,
        **fields,
    )
    if report.rate_limits:
        await usage.upsert_quota(
            tx, account_id, report.rate_limits, "reported", f"agent:{agent_id}"
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


async def _prefer_local(tx: Tx, task_id: str, role: LeaseRole) -> bool:
    """Does this task rank the local model first? Only a trivial task, only for the
    engine role (task complexity spec 3); never a restriction, just the order."""
    return role == "engine" and await tasks.prefers_local(tx.conn, task_id)


async def _pinned_account(tx: Tx, task_id: str, role: LeaseRole) -> str | None:
    """The one account the task's engine lease may use (its harness account pin,
    task models spec 3.4); None for any other role or an unpinned task."""
    return await tasks.harness_account(tx.conn, task_id) if role == "engine" else None


def _only(rows: list[aiosqlite.Row], account_id: str | None) -> list[aiosqlite.Row]:
    """The ranked candidates cut down to the pinned account, when there is one."""
    return rows if account_id is None else [r for r in rows if r["id"] == account_id]


async def _narrowed(
    tx: Tx, task_id: str, role: LeaseRole, runtime_providers: list[str] | None
) -> list[str] | None:
    """`runtime_providers` cut down to the task's `account_providers` (and pinned
    harness) for the engine role (local harness spec 3), or to its pinned executor's
    provider for the executor role (task models spec 3.2): the restriction only
    narrows, never widens. An empty result means no account is eligible."""
    if runtime_providers is None:
        return None
    if role == "engine":
        restriction = await tasks.account_providers(tx.conn, task_id)
    elif role == "executor" and (p := await tasks.executor_provider(tx.conn, task_id)):
        restriction = [p]
    else:
        restriction = None
    if restriction is None:
        return runtime_providers
    return [p for p in runtime_providers if p in restriction]


async def _try_lease(
    tx: Tx,
    agent_id: str,
    store: SecretStore,
    role: LeaseRole,
    runtime_providers: list[str] | None,
    *,
    reuse_held: bool = False,
    prefer_local: bool = False,
    only_account: str | None = None,
) -> AccountLease | None:
    """The best usable account for `role` across every provider: cloud before the
    local model (the reverse with `prefer_local`, whose fallback is the cloud in this
    same call), then priority, the least leased, and provider/name/id so ties are
    deterministic. Skips
    disabled, still-exhausted, full (`max_leases`) and credential-less accounts.

    With `reuse_held` (executor role), an account the agent already holds as its
    engine stays a candidate even at `max_leases`, and winning returns that
    engine lease as is (no second row, no extra slot).

    With `only_account` (a task's harness account pin, task models spec 3.4) that
    account is the only candidate: when it cannot serve, nothing is leased."""
    reuse = reuse_held and role == "executor"
    candidates = await _ranked(
        tx, agent_id, role, runtime_providers, reuse=reuse, prefer_local=prefer_local
    )
    candidates = _only(candidates, only_account)
    for row in candidates:
        name = ref_name(row["secret_ref"])
        credential = store.get(name) if name else None
        if credential is None:
            await _disable(tx, row["id"], f"secret not found: {row['secret_ref']}")
            continue
        if reuse and row["mine"] is not None:
            return AccountLease(
                lease_id=row["mine"],
                account_id=row["id"],
                name=row["name"],
                provider=row["provider"],
                kind=row["kind"],
                role="engine",
                credential=credential,
                settings=_settings(row, "engine"),
            )
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
            (
                lease_id,
                row["id"],
                agent_id,
                json.dumps(runtime_providers or []),
                ts(tx.now),
                role,
            ),
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
            settings=_settings(row, role),
        )
    return None


def _serves(row: aiosqlite.Row, role: LeaseRole) -> bool:
    """A custom account with no model for `role` cannot serve it (only that role)."""
    config = CustomConfig.model_validate_json(row["config"]) if row["config"] else None
    return serves_role(row["provider"], config, role)


def _settings(row: aiosqlite.Row, role: LeaseRole) -> LeaseSettings | None:
    """Where and how to call a custom account, for the role it is leased in."""
    if row["provider"] != CUSTOM_PROVIDER or not row["config"]:
        return None
    return lease_settings(CustomConfig.model_validate_json(row["config"]), role)


async def _ranked(
    tx: Tx,
    agent_id: str,
    role: LeaseRole,
    runtime_providers: list[str] | None,
    *,
    reuse: bool = False,
    prefer_local: bool = False,
) -> list[aiosqlite.Row]:
    """The usable accounts for `role` in lease order (cloud before the local
    model, or the reverse with `prefer_local`, then priority, least leased, then provider/name/id). An exhausted
    account past its reset is usable."""
    async with tx.conn.execute(
        "SELECT a.*, (SELECT count(*) FROM account_leases l WHERE l.account_id = a.id"
        "  AND l.released_at IS NULL) AS active,"
        " (SELECT id FROM account_leases l WHERE l.account_id = a.id"
        "  AND l.agent_id = ? AND l.role = 'engine' AND l.released_at IS NULL) AS mine"
        " FROM accounts a WHERE a.state = 'available'"
        "  OR (a.state = 'exhausted' AND a.exhausted_until <= ?)",
        (agent_id, ts(tx.now)),
    ) as cur:
        rows = await cur.fetchall()
    return sorted(
        (
            r
            for r in rows
            if role_accepts(role, r["provider"], runtime_providers)
            and _serves(r, role)
            and (
                (reuse and r["mine"] is not None)
                or r["max_leases"] is None
                or r["active"] < r["max_leases"]
            )
        ),
        key=lambda r: (
            fallback_tier(r["provider"], prefer_local=prefer_local),
            -r["priority"],
            r["active"],
            r["provider"],
            r["name"],
            r["id"],
        ),
    )


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
    " coalesce(sum(u.cached_input_tokens), 0), coalesce(sum(u.cost_usd), 0.0),"
    " count(*), coalesce(sum(u.measurement = 'not_reported'), 0),"
    " coalesce(sum(u.measurement = 'measured'), 0)"
)


def _usage_sql(since: str) -> str:
    """Totals, plus unmeasured reasons and runs by role, of the account's rows
    (`since` is "" or a `u.at > :since` filter). Constant fragments only."""
    where = f" AND u.at > {since}" if since else ""
    grouped = (
        "(SELECT json_group_object(k, n) FROM (SELECT coalesce(u.{col}, 'unknown')"  # noqa: S608
        " AS k, count(*) AS n FROM account_usage u WHERE u.account_id = a.id{more}"
        + where
        + " GROUP BY k))"
    )
    return (
        "(SELECT json_array("  # noqa: S608
        + _USAGE
        + ", "
        + grouped.format(
            col="unmeasured_reason", more=" AND u.measurement = 'not_reported'"
        )
        + ", "
        + grouped.format(col="role", more="")
        + ") FROM account_usage u WHERE u.account_id = a.id"
        + where
        + ")"
    )


_SELECT = (  # constant fragments only
    "SELECT a.*,"  # noqa: S608
    " (SELECT count(*) FROM account_leases l WHERE l.account_id = a.id"
    "  AND l.released_at IS NULL) AS active_leases,"
    " " + _usage_sql(":since") + " AS usage_24h,"
    " " + _usage_sql("") + " AS usage_total,"
    " (SELECT json_group_array(json_object('window', q.window,"
    "  'window_minutes', q.window_minutes, 'used_percent', q.used_percent,"
    "  'resets_at', q.resets_at, 'exhausted_until', q.exhausted_until,"
    "  'observed_at', q.observed_at, 'source', q.source))"
    "  FROM (SELECT * FROM account_quota WHERE account_id = a.id"
    "   ORDER BY window_minutes IS NULL, window_minutes, window) q) AS quota"
    " FROM accounts a"
)


def _counts(raw: dict | str | None) -> dict[str, int]:
    # A JSON object that crossed a subquery boundary comes back as text.
    return (json.loads(raw) if isinstance(raw, str) else raw) or {}


def _usage(raw: str) -> Usage:
    i, o, cached, cost, runs, unmeasured, measured, reasons, roles = json.loads(raw)
    return Usage(
        input_tokens=i,
        output_tokens=o,
        cached_input_tokens=cached,
        cost_usd=cost,
        runs=runs,
        unmeasured_runs=unmeasured,
        measured_runs=measured,
        unmeasured_reasons=_counts(reasons),
        runs_by_role=_counts(roles),
    )


async def _to_account(
    conn: aiosqlite.Connection, row: aiosqlite.Row, now: datetime
) -> Account:
    data = dict(row)
    data["labels"] = json.loads(data["labels"])
    data["usage_24h"] = _usage(data["usage_24h"])
    data["usage_total"] = _usage(data["usage_total"])
    data["quota"] = json.loads(data["quota"])
    data["credit"] = await usage.get_credit(conn, data["id"], data["provider"])
    data["quota_status"] = await usage.quota_status(
        conn, data, [QuotaWindow.model_validate(q) for q in data["quota"]], now
    )
    data["token_limits"] = (
        json.loads(data["token_limits"]) if data["token_limits"] else None
    )
    data["config"] = json.loads(data["config"]) if data["config"] else None
    return Account.model_validate(data)
