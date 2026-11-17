"""One execution-chain resolver shared by admission, settings and diagnostics.

No network calls. Credentials stay in the existing lease delivery, never snapshots.
"""

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Literal

import aiosqlite
from argus_core.accounts import AccountLease, LeaseRole
from argus_core.execution_chains import (
    Alternative,
    ExecutionChains,
    ExecutionReport,
    ExecutionSelection,
    PairedAlternative,
    ResolvedSelector,
    Selector,
    required_capabilities,
    resolve_chains,
)
from argus_core.settings import SettingValue
from pydantic import BaseModel

from argus_api.db import Tx, ts
from argus_api.errors import ConflictError, ValidationFailedError
from argus_api.events import record
from argus_api.secrets import SecretStore

KEY = "execution.chains"
WAIT = "awaiting_execution_route"
EXHAUSTED = "execution_chain_exhausted"


def revision(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def locked() -> bool:
    return any(
        os.environ.get(k, "").strip()
        for k in ("ARGUS_MODEL_POLICY", "ARGUS_DIFFICULTY_EXECUTORS")
    )


async def policy_setting(conn: aiosqlite.Connection, scope: str) -> SettingValue:
    async with conn.execute(
        "SELECT scope, value FROM settings WHERE key = ? AND scope IN ('global', ?)",
        (KEY, scope),
    ) as cur:
        stored = {r["scope"]: json.loads(r["value"]) for r in await cur.fetchall()}
    base = stored.get("global", {"version": 1, "difficulties": {}})
    own = stored.get(scope)
    levels = {**base["difficulties"], **(own or {}).get("difficulties", {})}
    sources = {
        d: (
            "project"
            if scope != "global" and d in (own or {}).get("difficulties", {})
            else "global"
            if d in base["difficulties"]
            else "legacy"
        )
        for d in ("trivial", "simple", "standard", "hard")
    }
    value = {"version": 1, "difficulties": levels}
    return SettingValue(
        key=KEY,
        value=value,
        scoped_value=own or {"version": 1, "difficulties": {}},
        source="project"
        if scope != "global" and own
        else "global"
        if base["difficulties"]
        else "default",
        revision=revision(value),
        difficulty_revisions={
            str(d): c.revision
            for d, c in resolve_chains(
                ExecutionChains.model_validate(base),
                ExecutionChains.model_validate(own)
                if scope != "global" and own
                else None,
            ).items()
        },
        difficulty_sources=sources,
        notice="legacy_env_lock" if locked() else None,
    )


async def validate_references(
    conn: aiosqlite.Connection, policy: ExecutionChains
) -> None:
    from argus_api.accounts import service as accounts

    if policy.difficulties and locked():
        raise ValidationFailedError("legacy_env_lock", code="legacy_env_lock")
    for difficulty, alternatives in policy.difficulties.items():
        for index, alternative in enumerate(alternatives):
            selectors: list[tuple[LeaseRole, Selector]] = [
                ("engine", alternative.harness)
            ]
            if isinstance(alternative, PairedAlternative):
                selectors.append(("executor", alternative.executor))
            for role, selector in selectors:
                async with conn.execute(
                    "SELECT * FROM accounts WHERE id = ?", (selector.account_id,)
                ) as cur:
                    account = await cur.fetchone()
                reason = (
                    "account_missing"
                    if account is None
                    else "provider_mismatch"
                    if account["provider"] != selector.provider
                    else "role_unavailable"
                    if not accounts._serves(account, role)
                    else None
                )
                if reason:
                    raise ValidationFailedError(
                        "invalid execution selector",
                        details=[f"difficulties/{difficulty}/{index}/{role}: {reason}"],
                    )


class RoutePolicy(BaseModel):
    source: Literal["global", "project"]
    revision: str
    alternatives: list[Alternative]
    bypass: str | None = None
    cursor: int = 0
    generation: int = 0
    skips: list[dict[str, Any]] = []
    selection: ExecutionSelection | None = None


async def resolve(conn: aiosqlite.Connection, row: aiosqlite.Row) -> RoutePolicy | None:
    setting = await policy_setting(conn, row["project"])
    alternatives = setting.value["difficulties"].get(row["difficulty"])
    if alternatives is None:
        return None
    bypass = (
        "legacy_task_pin"
        if any(
            row[k] is not None
            for k in ("harness_model", "harness_account", "executor_model")
        )
        else None
    )
    policy = RoutePolicy(
        source="project"
        if (setting.difficulty_sources or {})[row["difficulty"]] == "project"
        else "global",
        revision=(setting.difficulty_revisions or {})[row["difficulty"]],
        alternatives=alternatives,
        bypass=bypass,
    )
    async with conn.execute(
        "SELECT * FROM task_execution_routes WHERE task_id = ?", (row["id"],)
    ) as cur:
        state = await cur.fetchone()
    if state is not None:
        policy.generation = state["generation"]
        if state["fingerprint"] == fingerprint(row, policy):
            policy.cursor = state["cursor"]
            policy.skips = json.loads(state["skips"])
        if state["selection"]:
            policy.selection = ExecutionSelection.model_validate_json(
                state["selection"]
            )
    return policy


def fingerprint(row: aiosqlite.Row, policy: RoutePolicy) -> str:
    return revision(
        {
            "revision": policy.revision,
            **{
                k: row[k]
                for k in (
                    "difficulty",
                    "project",
                    "account_providers",
                    "harness_account",
                    "harness_model",
                    "executor_model",
                    "title",
                    "spec",
                    "acceptance",
                )
            },
        }
    )


class Unavailable(Exception):
    pass


async def _reserve_selector(
    tx: Tx,
    agent_id: str,
    selector: Selector,
    role: LeaseRole,
    store: SecretStore,
    served: Mapping[str, tuple[str, ...] | None] | None,
    *,
    reuse: bool = False,
) -> tuple[AccountLease, ResolvedSelector]:
    from argus_api.accounts import service as accounts

    # Exact-account narrowing is mandatory; never use a provider's priority winner.
    lease = await accounts._try_lease(
        tx,
        agent_id,
        store,
        role,
        [selector.provider],
        only_account=selector.account_id,
        reuse_held=reuse,
        local_served_models=served,
    )
    if lease is None:
        raise Unavailable(f"{role}_account_unavailable")
    if selector.model.source == "explicit":
        model = selector.model.id
    else:
        model = (
            lease.settings.model if lease.settings else None
        ) or lease.credential.get("model")
        if not model and selector.provider == "local" and served:
            known = served.get(selector.account_id)
            if known and len(known) == 1:
                model = known[0]
    if not isinstance(model, str) or not model:
        raise Unavailable(f"{role}_model_unknown")
    if (
        selector.provider == "local"
        and served
        and served.get(selector.account_id) is not None
        and model not in (served.get(selector.account_id) or ())
    ):
        raise Unavailable("model_not_served")
    lease = lease.model_copy(update={"model": model})
    if lease.settings is not None:
        lease = lease.model_copy(
            update={"settings": lease.settings.model_copy(update={"model": model})}
        )
    if selector.provider == "local":
        lease = lease.model_copy(
            update={"credential": {**lease.credential, "model": model}}
        )
    return lease, ResolvedSelector(
        selector=selector,
        account_id=lease.account_id,
        model=model,
        lease_id=lease.lease_id,
    )


async def reserve(
    tx: Tx,
    row: aiosqlite.Row,
    policy: RoutePolicy,
    agent_id: str,
    providers: list[str],
    capabilities: list[str],
    store: SecretStore,
    served: Mapping[str, tuple[str, ...] | None] | None,
) -> tuple[ExecutionSelection, AccountLease, AccountLease | None] | None:

    if locked():
        policy.skips = [{"reason": "legacy_env_lock"}]
        return None
    if "execution-chains-v1" not in capabilities:
        return None  # old workers must not claim an explicit route
    async with tx.conn.execute(
        "SELECT 1 FROM account_leases WHERE agent_id = ? AND role IN ('engine', 'executor') AND released_at IS NULL",
        (agent_id,),
    ) as cur:
        if await cur.fetchone():
            raise ConflictError(
                "stop the idle runtime and release model leases before chain admission",
                code="execution_runtime_reset_required",
            )
    skips = []
    async with tx.conn.execute(
        "SELECT fingerprint, skips FROM task_execution_routes WHERE task_id=?",
        (row["id"],),
    ) as cur:
        previous = await cur.fetchone()
    if previous is not None and previous["fingerprint"] != fingerprint(row, policy):
        await record(
            tx,
            "system",
            "task",
            row["id"],
            "task.execution_route_invalidated",
            {
                "revision": policy.revision,
                "difficulty": row["difficulty"],
                "generation": policy.generation,
            },
        )
    restriction = (
        json.loads(row["account_providers"]) if row["account_providers"] else None
    )
    for index in range(policy.cursor, len(policy.alternatives)):
        alternative = policy.alternatives[index]
        try:
            async with tx.savepoint():
                if not required_capabilities(alternative) <= set(capabilities):
                    raise Unavailable("worker_capability_missing")
                if alternative.harness.provider not in providers or (
                    restriction is not None
                    and alternative.harness.provider not in restriction
                ):
                    raise Unavailable("harness_provider_restricted")
                engine, harness = await _reserve_selector(
                    tx, agent_id, alternative.harness, "engine", store, served
                )
                executor_lease = None
                executor = None
                if isinstance(alternative, PairedAlternative):
                    executor_lease, executor = await _reserve_selector(
                        tx,
                        agent_id,
                        alternative.executor,
                        "executor",
                        store,
                        served,
                        reuse=alternative.executor.provider != "custom",
                    )
                selection = ExecutionSelection(
                    source=policy.source,
                    revision=policy.revision,
                    difficulty=row["difficulty"],
                    alternative=alternative,
                    index=index,
                    generation=policy.generation + 1,
                    candidates=policy.alternatives,
                    harness=harness,
                    executor=executor,
                )
                await tx.conn.execute(
                    "INSERT INTO task_execution_routes (task_id, fingerprint, cursor, generation, selection, applied, skips) VALUES (?, ?, ?, ?, ?, 0, ?) ON CONFLICT(task_id) DO UPDATE SET fingerprint=excluded.fingerprint, cursor=excluded.cursor, generation=excluded.generation, selection=excluded.selection, applied=0, skips=excluded.skips",
                    (
                        row["id"],
                        fingerprint(row, policy),
                        index,
                        selection.generation,
                        selection.model_dump_json(),
                        json.dumps(skips),
                    ),
                )
                await record(
                    tx,
                    f"agent:{agent_id}",
                    "task",
                    row["id"],
                    "task.execution_route_selected",
                    selection.model_dump(mode="json"),
                )
                if skips and (
                    previous is None
                    or json.loads(previous["skips"]) != skips
                    or previous["fingerprint"] != fingerprint(row, policy)
                ):
                    await record(
                        tx,
                        "system",
                        "task",
                        row["id"],
                        "task.execution_route_skipped",
                        {"revision": policy.revision, "skips": skips},
                    )
                return selection, engine, executor_lease
        except Unavailable as exc:
            skips.append({"index": index, "id": alternative.id, "reason": str(exc)})
    policy.skips = skips
    async with tx.conn.execute(
        "SELECT skips FROM task_execution_routes WHERE task_id = ?", (row["id"],)
    ) as cur:
        previous = await cur.fetchone()
    if previous is None or json.loads(previous[0]) != skips:
        await record(
            tx,
            "system",
            "task",
            row["id"],
            "task.execution_route_skipped",
            {"revision": policy.revision, "skips": skips},
        )
    await tx.conn.execute(
        "INSERT INTO task_execution_routes(task_id, fingerprint, cursor, generation, skips) VALUES (?, ?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET fingerprint=excluded.fingerprint, cursor=excluded.cursor, skips=excluded.skips",
        (
            row["id"],
            fingerprint(row, policy),
            policy.cursor,
            policy.generation,
            json.dumps(skips),
        ),
    )
    return None


async def snapshot(
    tx: Tx, task_id: str, attempt: int, selection: ExecutionSelection
) -> None:
    async with tx.conn.execute(
        "SELECT id, definition FROM task_attempts WHERE task_id = ? AND attempt = ? ORDER BY id DESC LIMIT 1",
        (task_id, attempt),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise ConflictError("execution attempt snapshot missing")
    definition = json.loads(row["definition"])
    definition["execution"] = selection.model_dump(mode="json")
    await tx.conn.execute(
        "UPDATE task_attempts SET definition = ? WHERE id = ?",
        (json.dumps(definition), row["id"]),
    )


async def current(
    conn: aiosqlite.Connection, task_id: str, attempt: int
) -> ExecutionSelection | None:
    async with conn.execute(
        "SELECT definition FROM task_attempts WHERE task_id = ? AND attempt = ? ORDER BY id DESC LIMIT 1",
        (task_id, attempt),
    ) as cur:
        row = await cur.fetchone()
    value = json.loads(row[0]).get("execution") if row else None
    return ExecutionSelection.model_validate(value) if value else None


async def report(tx: Tx, task_id: str, agent_id: str, body: ExecutionReport) -> None:
    from argus_core.tasks import Action

    from argus_api.tasks import service as tasks
    from argus_api.tasks.transitions import _apply

    await tasks.check_lease(tx, task_id, body.attempt, agent_id)
    selection = await current(tx.conn, task_id, body.attempt)
    async with tx.conn.execute(
        "SELECT * FROM task_execution_routes WHERE task_id = ?", (task_id,)
    ) as cur:
        state = await cur.fetchone()
    if (
        selection is None
        or state is None
        or body.generation != selection.generation
        or state["generation"] != body.generation
    ):
        raise ConflictError(
            "stale execution selection", code="execution_generation_mismatch"
        )
    for role, resolved in (
        ("engine", selection.harness),
        ("executor", selection.executor),
    ):
        if resolved is None:
            continue
        actual_role = (
            "engine" if resolved.lease_id == selection.harness.lease_id else role
        )
        async with tx.conn.execute(
            "SELECT 1 FROM account_leases WHERE id=? AND account_id=? AND agent_id=? AND role=? AND released_at IS NULL",
            (resolved.lease_id, resolved.account_id, agent_id, actual_role),
        ) as cur:
            if await cur.fetchone() is None:
                raise ConflictError(
                    "execution tuple no longer owned", code="execution_lease_missing"
                )
    if body.action == "applied":
        if not state["applied"]:
            await tx.conn.execute(
                "UPDATE task_execution_routes SET applied=1 WHERE task_id = ?",
                (task_id,),
            )
            await record(
                tx,
                f"agent:{agent_id}",
                "task",
                task_id,
                "task.execution_route_applied",
                {"generation": body.generation},
            )
        return
    if body.reason is None:
        raise ValidationFailedError("advance requires a classified reason")
    # The worker attests it stopped the complete runtime before reporting advance.
    if not body.stopped:
        raise ValidationFailedError(
            "advance requires complete runtime stop acknowledgement"
        )
    failed = selection.harness if body.failed_role == "harness" else selection.executor
    if failed is None:
        raise ValidationFailedError("single layer has no executor failure")
    if body.reason == "quota":
        from argus_api.accounts import service as accounts

        await accounts._mark_exhausted(
            tx,
            agent_id,
            failed.account_id,
            None,
            "execution_route_quota",
            timedelta(minutes=5),
        )
    for resolved in {
        s.lease_id: s for s in (selection.harness, selection.executor) if s is not None
    }.values():
        changed = await tx.conn.execute(
            "UPDATE account_leases SET released_at=?, release_reason='execution_route_advanced' "
            "WHERE id=? AND account_id=? AND agent_id=? AND released_at IS NULL",
            (ts(tx.now), resolved.lease_id, resolved.account_id, agent_id),
        )
        if changed.rowcount != 1:
            raise ConflictError(
                "execution lease no longer owned", code="execution_lease_missing"
            )
    cursor = selection.index + 1
    await tx.conn.execute(
        "UPDATE task_execution_routes SET cursor=?, generation=generation+1, applied=0 WHERE task_id=?",
        (cursor, task_id),
    )
    exhausted = cursor >= len(selection.candidates)
    if body.reason in {"quota", "provider_unavailable"}:
        await tasks.pause_for_quota(
            tx, agent_id, task_id, body.attempt, body.reason, wait=False
        )
    else:
        await tasks.fail(
            tx, agent_id, task_id, body.attempt, body.reason, "worker_fail"
        )
    after = await tasks.task_row(tx.conn, task_id)
    if exhausted and after["state"] == "ready":
        await _apply(
            tx,
            await tasks.task_row(tx.conn, task_id),
            Action.BLOCK,
            f"agent:{agent_id}",
            set_={"block_reason": EXHAUSTED},
            data={"reason": EXHAUSTED},
        )
    await record(
        tx,
        f"agent:{agent_id}",
        "task",
        task_id,
        "task.execution_route_exhausted"
        if exhausted
        else "task.execution_route_advanced",
        {
            "generation": body.generation,
            "from": selection.alternative.id,
            "cursor": cursor,
            "reason": body.reason,
        },
    )


async def executor_delivery(
    conn: aiosqlite.Connection,
    selection: ExecutionSelection,
    agent_id: str,
    store: SecretStore,
) -> AccountLease | None:
    from argus_api.accounts import service as accounts
    from argus_api.secrets import ref_name

    resolved = selection.executor
    if resolved is None or resolved.lease_id == selection.harness.lease_id:
        return None
    async with conn.execute(
        "SELECT a.* FROM accounts a JOIN account_leases l ON l.account_id=a.id WHERE l.id=? AND l.agent_id=? AND l.role='executor' AND l.released_at IS NULL",
        (resolved.lease_id, agent_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise ConflictError(
            "execution executor lease missing", code="execution_lease_missing"
        )
    name = ref_name(row["secret_ref"])
    credential = store.get(name) if name else None
    if credential is None:
        raise ConflictError(
            "execution credential missing", code="execution_lease_missing"
        )
    settings = accounts._settings(row, "executor", credential)
    if settings is not None:
        settings = settings.model_copy(update={"model": resolved.model})
    return AccountLease(
        lease_id=resolved.lease_id,
        account_id=resolved.account_id,
        name=row["name"],
        provider=row["provider"],
        kind=row["kind"],
        role="executor",
        credential=credential,
        settings=settings,
        model=resolved.model,
    )


async def reset(tx: Tx, task_id: str, actor: str) -> None:
    await tx.conn.execute(
        "UPDATE task_execution_routes SET cursor=0, generation=generation+1, selection=NULL, applied=0, skips='[]' WHERE task_id=?",
        (task_id,),
    )
    await record(tx, actor, "task", task_id, "task.execution_route_reset")


async def wake(tx: Tx) -> None:
    from argus_api.tasks.manage import unblock

    async with tx.conn.execute(
        "SELECT id, block_reason FROM tasks WHERE state='blocked' AND block_reason IN (?, ?)",
        (WAIT, EXHAUSTED),
    ) as cur:
        waiting = await cur.fetchall()
    async with tx.conn.execute(
        "SELECT capabilities, runtime_providers FROM agents WHERE state='active'"
    ) as cur:
        workers = await cur.fetchall()
    from argus_api.tasks.reads import task_row

    for waiting_task in waiting:
        task_id = waiting_task["id"]
        row = await task_row(tx.conn, task_id)
        policy = await resolve(tx.conn, row)
        if (
            waiting_task["block_reason"] == EXHAUSTED
            and policy is not None
            and policy.cursor >= len(policy.alternatives)
        ):
            continue
        if (
            policy is None
            or policy.bypass
            or any(
                [
                    await eligible(
                        tx.conn,
                        tx.now,
                        row,
                        policy,
                        json.loads(w["capabilities"]),
                        json.loads(w["runtime_providers"] or "[]"),
                    )
                    for w in workers
                ]
            )
        ):
            await unblock(tx, task_id, "system")


async def check_generation(
    tx: Tx, task_id: str, attempt: int, generation: int | None
) -> None:
    selection = await current(tx.conn, task_id, attempt)
    if selection is None:
        return
    async with tx.conn.execute(
        "SELECT generation FROM task_execution_routes WHERE task_id=?", (task_id,)
    ) as cur:
        row = await cur.fetchone()
    if generation != selection.generation or row is None or row[0] != generation:
        raise ConflictError(
            "stale execution selection", code="execution_generation_mismatch"
        )


async def eligible(
    conn: aiosqlite.Connection,
    now: Any,
    row: aiosqlite.Row,
    policy: RoutePolicy,
    capabilities: list[str],
    providers: list[str],
) -> bool:
    """Read-only capacity/role evidence; credential delivery is checked at reservation.

    The same exact tuple checks gate wake and reservation. Unknown secrets or local
    discovery are not claimed as proof of reachability.
    """
    from argus_api.accounts import service as accounts
    from argus_api.accounts.eligibility import LEASE_BLOCK_SQL

    if locked():
        return False
    restriction = (
        json.loads(row["account_providers"]) if row["account_providers"] else None
    )
    for alternative in policy.alternatives[policy.cursor :]:
        if not required_capabilities(alternative) <= set(capabilities):
            continue
        if alternative.harness.provider not in providers or (
            restriction is not None and alternative.harness.provider not in restriction
        ):
            continue
        selectors: list[tuple[LeaseRole, Selector]] = [("engine", alternative.harness)]
        if isinstance(alternative, PairedAlternative):
            selectors.append(("executor", alternative.executor))
        demand: dict[str, int] = {}
        usable = True
        for role, selector in selectors:
            async with conn.execute(
                "SELECT a.*, (SELECT count(*) FROM account_leases l WHERE l.account_id=a.id AND l.released_at IS NULL) AS active "  # noqa: S608
                "FROM accounts a WHERE id=? AND (state='available' OR (state='exhausted' AND exhausted_until<=?)) "
                f"AND {LEASE_BLOCK_SQL} IS NULL",
                (selector.account_id, ts(now)),
            ) as cur:
                account = await cur.fetchone()
            if (
                account is None
                or account["provider"] != selector.provider
                or not accounts._serves(account, role)
            ):
                usable = False
                break
            reuse = (
                role == "executor"
                and selector.account_id == alternative.harness.account_id
                and selector.provider != "custom"
            )
            demand[selector.account_id] = demand.get(selector.account_id, 0) + (
                0 if reuse else 1
            )
            if (
                account["max_leases"] is not None
                and account["active"] + demand[selector.account_id]
                > account["max_leases"]
            ):
                usable = False
                break
        if usable:
            return True
    return False
