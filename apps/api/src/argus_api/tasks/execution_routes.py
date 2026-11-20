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
# Independent reviewers: same shape, keyed by the reviewed task's difficulty.
REVIEW_KEY = "review.chains"
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


async def policy_setting(
    conn: aiosqlite.Connection, scope: str, key: str = KEY
) -> SettingValue:
    async with conn.execute(
        "SELECT scope, value FROM settings WHERE key = ? AND scope IN ('global', ?)",
        (key, scope),
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
        key=key,
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
        warnings=await readiness(conn, levels),
    )


async def readiness(conn: aiosqlite.Connection, levels: Mapping[str, Any]) -> list[str]:
    """Bounded, non-secret readiness notes for accounts a saved chain references.

    A disabled or exhausted account may be saved; admission refuses it with the
    same reason. Deletion or a role change is reported here, not repaired.
    """
    from argus_api.accounts import service as accounts

    wanted: dict[str, set[LeaseRole]] = {}
    for alternatives in levels.values():
        for alternative in alternatives:
            selectors: list[tuple[LeaseRole, Any]] = [
                ("engine", alternative["harness"])
            ]
            executor = alternative.get("executor")
            if executor is not None:
                selectors.append(("executor", executor))
            for role, selector in selectors:
                if selector.get("account_id") is not None:
                    wanted.setdefault(selector["account_id"], set()).add(role)
    warnings: list[str] = []
    for account_id in sorted(wanted):
        async with conn.execute(
            "SELECT provider, state, exhausted_until, config FROM accounts WHERE id = ?",
            (account_id,),
        ) as cur:
            account = await cur.fetchone()
        if account is None:
            warnings.append(f"account {account_id} is missing")
            continue
        if account["state"] == "disabled":
            warnings.append(f"account {account_id} is disabled")
        elif account["state"] == "exhausted":
            warnings.append(f"account {account_id} is exhausted")
        warnings.extend(
            f"account {account_id} does not serve the {role} role"
            for role in sorted(wanted[account_id])
            if not accounts._serves(account, role)
        )
    return warnings


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
                if selector.account_id is None:
                    continue  # any account: admission picks one and reports why not
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


class SkippedAlternative(BaseModel):
    id: str
    reason: Literal["author", "unavailable", "failed"]


class ReviewRoute(BaseModel):
    """Why an independent reviewer runs on this route (settings.spec.md 3.5)."""

    parent_id: str
    difficulty: str
    chain_source: Literal["global", "project"]
    selected: str
    skipped: list[SkippedAlternative] = []


class RoutePolicy(BaseModel):
    source: Literal["global", "project", "review"]
    revision: str
    alternatives: list[Alternative]
    bypass: str | None = None
    cursor: int = 0
    generation: int = 0
    skips: list[dict[str, Any]] = []
    selection: ExecutionSelection | None = None
    review: ReviewRoute | None = None


def explicit_route(alternatives: list[Alternative], review: ReviewRoute) -> str:
    """The `tasks.execution_route` value; the generic chain never re-routes it."""
    value = {
        "source": "review",
        "alternatives": [a.model_dump(mode="json") for a in alternatives],
        "review": review.model_dump(mode="json"),
    }
    return json.dumps({**value, "revision": revision(value)})


async def resolve(conn: aiosqlite.Connection, row: aiosqlite.Row) -> RoutePolicy | None:
    if row["execution_route"]:
        policy = RoutePolicy.model_validate_json(row["execution_route"])
    else:
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


class AccountRejected(Unavailable):
    """The leased account cannot run the selector; an unpinned selector tries the next."""

    def __init__(self, reason: str, account_id: str) -> None:
        super().__init__(reason)
        self.account_id = account_id


def shared_native(alternative: Alternative) -> bool:
    """One native identity per provider: a same-provider pair runs on one account."""
    return (
        isinstance(alternative, PairedAlternative)
        and alternative.harness.provider == alternative.executor.provider
        and alternative.harness.provider != "custom"
    )


def pins(alternative: Alternative) -> tuple[str | None, str | None]:
    """Configured harness/executor account pins; a shared native pair adopts either."""
    harness = alternative.harness.account_id
    if not isinstance(alternative, PairedAlternative):
        return harness, None
    executor = alternative.executor.account_id
    if shared_native(alternative):
        harness = executor = harness or executor
    return harness, executor


async def _reserve_selector(
    tx: Tx,
    agent_id: str,
    selector: Selector,
    account_id: str | None,
    role: LeaseRole,
    store: SecretStore,
    served: Mapping[str, tuple[str, ...] | None] | None,
    *,
    reuse: bool = False,
) -> tuple[AccountLease, ResolvedSelector]:
    """Lease the pinned account, or the best usable one when `account_id` is None.

    An unpinned selector moves on to the next account when the best one cannot name
    a served model; a pin never does. The concrete account is returned in
    `ResolvedSelector.account_id`, which the selection event and attempt record keep.
    """
    rejected: set[str] = set()
    last: Unavailable | None = None
    while True:
        try:
            async with tx.savepoint():
                return await _reserve_account(
                    tx,
                    agent_id,
                    selector,
                    account_id,
                    role,
                    store,
                    served,
                    reuse,
                    rejected,
                )
        except AccountRejected as exc:
            if account_id is not None:
                raise
            last = exc
            rejected.add(exc.account_id)
        except Unavailable as exc:
            raise (last or exc) from None


async def _reserve_account(
    tx: Tx,
    agent_id: str,
    selector: Selector,
    account_id: str | None,
    role: LeaseRole,
    store: SecretStore,
    served: Mapping[str, tuple[str, ...] | None] | None,
    reuse: bool,
    rejected: set[str],
) -> tuple[AccountLease, ResolvedSelector]:
    from argus_api.accounts import service as accounts

    # A pin narrows to exactly that account; never fall back to the priority winner.
    lease = await accounts._try_lease(
        tx,
        agent_id,
        store,
        role,
        [selector.provider],
        only_account=account_id,
        exclude=rejected,
        reuse_held=reuse,
        local_served_models=served,
    )
    if lease is None:
        raise Unavailable(f"{role}_account_unavailable")
    known = (served or {}).get(lease.account_id)
    if selector.model.source == "explicit":
        model = selector.model.id
    else:
        model = (
            lease.settings.model if lease.settings else None
        ) or lease.credential.get("model")
        if not model and selector.provider == "local" and known and len(known) == 1:
            model = known[0]
    if not isinstance(model, str) or not model:
        raise AccountRejected(f"{role}_model_unknown", lease.account_id)
    if selector.provider == "local" and known is not None and model not in known:
        raise AccountRejected("model_not_served", lease.account_id)
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
                harness_pin, executor_pin = pins(alternative)
                engine, harness = await _reserve_selector(
                    tx,
                    agent_id,
                    alternative.harness,
                    harness_pin,
                    "engine",
                    store,
                    served,
                )
                executor_lease = None
                executor = None
                if isinstance(alternative, PairedAlternative):
                    executor_lease, executor = await _reserve_selector(
                        tx,
                        agent_id,
                        alternative.executor,
                        harness.account_id
                        if shared_native(alternative)
                        else executor_pin,
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
    tuple_owned = True
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
                tuple_owned = False
                break
    if body.action == "applied":
        if not tuple_owned:
            raise ConflictError(
                "execution tuple no longer owned", code="execution_lease_missing"
            )
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
    if not tuple_owned:
        # Lost-ownership race: release any still-active lease of this exact tuple
        # only, bump generation so duplicate reports stay fenced, and fail the
        # attempt cleanly so the task is never stranded claimed.
        for resolved in {
            s.lease_id: s
            for s in (selection.harness, selection.executor)
            if s is not None
        }.values():
            await tx.conn.execute(
                "UPDATE account_leases SET released_at=?, release_reason='execution_lease_missing' "
                "WHERE id=? AND account_id=? AND agent_id=? AND released_at IS NULL",
                (ts(tx.now), resolved.lease_id, resolved.account_id, agent_id),
            )
        await tx.conn.execute(
            "UPDATE task_execution_routes SET generation=generation+1, applied=0 WHERE task_id=?",
            (task_id,),
        )
        await tasks.fail(
            tx,
            agent_id,
            task_id,
            body.attempt,
            f"execution tuple no longer owned before advance ({body.reason})",
            "execution_lease_missing",
        )
        return
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
    if body.reason == "auth_expired":
        from argus_api.accounts import service as accounts

        await accounts._login_required(
            tx,
            agent_id,
            failed.account_id,
            task_id,
            body.attempt,
            "execution_route",
            role="engine" if failed is selection.harness else "executor",
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
    if body.reason in {"quota", "provider_unavailable", "auth_expired"}:
        await tasks.pause_for_quota(
            tx,
            agent_id,
            task_id,
            body.attempt,
            body.reason,
            wait=False,
            reason="auth_expired"
            if body.reason == "auth_expired"
            else "quota_exhausted",
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

    if locked():
        return False
    restriction = (
        json.loads(row["account_providers"]) if row["account_providers"] else None
    )
    for alternative in policy.alternatives[policy.cursor :]:
        if await alternative_eligible(
            conn, now, alternative, restriction, capabilities, providers
        ):
            return True
    return False


async def alternative_eligible(
    conn: aiosqlite.Connection,
    now: Any,
    alternative: Alternative,
    restriction: list[str] | None,
    capabilities: list[str],
    providers: list[str],
) -> bool:
    if not required_capabilities(alternative) <= set(capabilities):
        return False
    if alternative.harness.provider not in providers or (
        restriction is not None and alternative.harness.provider not in restriction
    ):
        return False
    harness_pin, executor_pin = pins(alternative)
    harness_rows = await _usable_accounts(
        conn, now, alternative.harness, harness_pin, "engine"
    )
    if isinstance(alternative, PairedAlternative):
        executor_rows = await _usable_accounts(
            conn, now, alternative.executor, executor_pin, "executor"
        )
    else:
        executor_rows = [None]
    # Any assignment of concrete accounts that fits capacity makes it eligible.
    for harness in harness_rows:
        for executor in executor_rows:
            if (
                executor is not None
                and shared_native(alternative)
                and executor["id"] != harness["id"]
            ):
                continue
            if _fits(alternative, harness, executor):
                return True
    return False


async def _usable_accounts(
    conn: aiosqlite.Connection,
    now: Any,
    selector: Selector,
    pin: str | None,
    role: LeaseRole,
) -> list[aiosqlite.Row]:
    """Accounts of the selector's provider (just the pin, when pinned) that are
    available and serve `role`, ordered by id for deterministic evidence."""
    from argus_api.accounts import service as accounts
    from argus_api.accounts.eligibility import LEASE_BLOCK_SQL

    async with conn.execute(
        "SELECT a.*, (SELECT count(*) FROM account_leases l WHERE l.account_id=a.id AND l.released_at IS NULL) AS active "  # noqa: S608
        "FROM accounts a WHERE provider=? AND (? IS NULL OR id=?) "
        "AND (state='available' OR (state='exhausted' AND exhausted_until<=?)) "
        f"AND {LEASE_BLOCK_SQL} IS NULL ORDER BY id",
        (selector.provider, pin, pin, ts(now)),
    ) as cur:
        rows = await cur.fetchall()
    return [a for a in rows if accounts._serves(a, role)]


def _fits(
    alternative: Alternative, harness: aiosqlite.Row, executor: aiosqlite.Row | None
) -> bool:
    """Slot demand of one concrete harness/executor assignment. A shared native
    executor reuses the harness lease; any other same-account pair needs two slots."""
    demand = {harness["id"]: 1}
    if executor is not None and not shared_native(alternative):
        demand[executor["id"]] = demand.get(executor["id"], 0) + 1
    return all(
        row["max_leases"] is None
        or row["active"] + demand[row["id"]] <= row["max_leases"]
        for row in (harness, executor)
        if row is not None
    )
