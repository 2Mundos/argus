"""Converge each pool's instances to its desired replicas (worker lifecycle 3.5).

Each pass, per pool:

1. **Observe**: list the pool's containers on its target. If the target cannot be
   queried, nothing about that pool changes this pass (status `unreachable`):
   absence of evidence is never confirmation, and capacity stays reserved.
2. **Decide** in one write transaction: confirm what the observation proves,
   start terminations, reserve slots for new instances, drain idle ones.
3. **Act** after commit: start and stop containers. A failed or ambiguous call
   changes nothing; the next pass observes the outcome and acts again.

Invariants:
- An instance holds a capacity slot until the provider confirms it stopped or no
  longer exists. Timeouts and stop acknowledgements are not confirmation.
- Ending an instance and ending its agents (tasks released, tokens revoked)
  happen in the same transaction.
- Scale-down and recycling never stop a busy agent: a draining instance is
  stopped only once none of its agents holds a claimed task (or all have left).
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import aiosqlite
from argus_core.agents import BootstrapTokenCreate, Role
from argus_core.fleet import Pool

from argus_api import model_policy
from argus_api.agents import service as agents
from argus_api.db import Database, Tx, ts
from argus_api.errors import ValidationFailedError
from argus_api.events import record
from argus_api.fleet import runtimes
from argus_api.fleet import service as fleet
from argus_api.fleet.targets import (
    POOL_LABEL,
    LabeledContainer,
    ProviderInstance,
    Target,
    TargetError,
    TargetRegistry,
    container_name,
)
from argus_api.ops import LoopStats

log = logging.getLogger(__name__)


def default_start_cap() -> int:
    """Starts in flight at once: half the logical CPUs of this host, at least 1."""
    return max(1, (os.cpu_count() or 1) // 2)


@dataclass(frozen=True)
class FleetSettings:
    argus_url: str | None  # how containers reach Argus; required to start anything
    registration_timeout: timedelta = timedelta(minutes=5)
    stop_grace: timedelta = timedelta(seconds=30)
    start_backoff_max: timedelta = timedelta(minutes=5)
    # The manager's model policy file; set, it is passed to new NanoClaw instances.
    model_policy_path: Path | None = None
    # Reconciler-wide cap on `reserved` instances no listing has confirmed yet.
    max_starts_in_flight: int = field(default_factory=default_start_cap)


@dataclass(frozen=True)
class _Start:
    instance_id: str


@dataclass(frozen=True)
class _Stop:
    ref: str


async def reconcile(
    db: Database, targets: TargetRegistry, settings: FleetSettings
) -> None:
    async with db.read() as conn:
        pools = await fleet.list_pools(conn)
    for pool in pools:
        try:
            await reconcile_pool(db, pool.name, targets, settings)
        except Exception:
            log.exception("reconciling pool %s failed", pool.name)


async def reconcile_pool(
    db: Database, name: str, targets: TargetRegistry, settings: FleetSettings
) -> None:
    async with db.read() as conn:
        pool = await fleet.get_pool(conn, name)
    try:
        target = targets.get(pool.target, *runtimes.target_location(pool))
        observed = {
            p.instance_id: p for p in await target.list({POOL_LABEL: pool.name})
        }
        descendants = await _observe_descendants(target, pool)
    except TargetError as exc:
        async with db.write() as tx:
            await _set_status(tx, name, "unreachable", str(exc))
        return

    async with db.write() as tx:
        actions = await _decide(tx, name, observed, descendants, settings)
    for action in actions:
        await _act(db, target, name, action, settings)


async def _observe_descendants(
    target: Target, pool: Pool
) -> dict[str, list[LabeledContainer]]:
    """The runtime's own containers (e.g. NanoClaw agents), keyed by the id
    prefix of the instance that owns them."""
    key = runtimes.descendant_label(pool)
    if key is None:
        return {}
    owned: dict[str, list[LabeledContainer]] = {}
    for c in await target.list_by_label(key):
        if (owner := runtimes.descendant_owner(pool, c.value)) is not None:
            owned.setdefault(owner, []).append(c)
    return owned


async def _decide(
    tx: Tx,
    name: str,
    observed: dict[str, ProviderInstance],
    descendants: dict[str, list[LabeledContainer]],
    settings: FleetSettings,
) -> list[_Start | _Stop]:
    pool = await fleet.get_pool(tx.conn, name)
    if pool.status == "unreachable":
        await _set_status(tx, name, "ok", None)
    actions: list[_Start | _Stop] = []
    async with tx.conn.execute(
        "SELECT * FROM instances WHERE pool = ? AND state != 'terminated'", (name,)
    ) as cur:
        instances = list(await cur.fetchall())

    # Existing reservations hold their slots; only the oldest `cap` of them
    # (across all pools) may retry their start, so a stuck one never starves.
    async with tx.conn.execute(
        "SELECT id FROM instances WHERE state = 'reserved'"
        " ORDER BY created_at, id LIMIT ?",
        (settings.max_starts_in_flight,),
    ) as cur:
        may_start = {r["id"] for r in await cur.fetchall()}

    known = set()
    for inst in instances:
        known.add(inst["id"])
        obs = observed.get(inst["id"])
        state = inst["state"]
        if (
            obs is not None
            and obs.status == "running"
            and obs.image_digest
            and obs.image_digest != inst["image_digest"]
        ):  # observed before this transaction: no target call in here
            await tx.conn.execute(
                "UPDATE instances SET image_digest = ? WHERE id = ?",
                (obs.image_digest, inst["id"]),
            )
        if state == "reserved":
            if obs is not None and obs.status == "running":
                await _mark_running(tx, inst["id"], obs.ref)
            elif obs is not None and obs.status == "exited":
                await confirm_terminated(
                    tx, inst["id"], f"exited while starting ({obs.exit_code})"
                )
                actions.append(_Stop(obs.ref))  # remove the exited container
            elif inst["id"] in may_start and _start_due(inst, tx, settings):
                actions.append(_Start(inst["id"]))
        elif state == "running":
            if obs is None or obs.status == "exited":
                why = (
                    "container vanished" if obs is None else f"exited ({obs.exit_code})"
                )
                children = descendants.get(inst["id"][:8], [])
                if children:  # the host is gone but its agents are not
                    await begin_terminating(tx, inst["id"], why)
                    actions.extend(_Stop(c.ref) for c in children)
                else:
                    await confirm_terminated(tx, inst["id"], why)
                if obs is not None:
                    actions.append(_Stop(obs.ref))  # remove the exited container
            elif reason := await _why_terminate(tx.conn, inst, tx, settings):
                await begin_terminating(tx, inst["id"], reason)
                actions.append(_Stop(inst["provider_ref"]))
        elif state == "terminating":
            children = descendants.get(inst["id"][:8], [])
            actions.extend(_Stop(c.ref) for c in children)
            if obs is None or obs.status == "exited":
                if not children:  # confirmed only once every owned container is gone
                    await confirm_terminated(
                        tx, inst["id"], inst["exit_reason"] or "stopped"
                    )
                if obs is not None:
                    actions.append(_Stop(obs.ref))
            else:
                actions.append(_Stop(inst["provider_ref"]))

    # Containers carrying this pool's label that Argus does not track as live:
    # leftovers of terminated instances, or orphans. Stop them.
    actions.extend(_Stop(o.ref) for i, o in observed.items() if i not in known)
    # Descendants whose instance is not live (terminated, or never ours): orphans.
    live_prefixes = {i[:8] for i in known}
    for owner, children in descendants.items():
        if owner not in live_prefixes:
            actions.extend(_Stop(c.ref) for c in children)

    actions.extend(await _scale(tx, pool, settings))
    return actions


async def _why_terminate(
    conn: aiosqlite.Connection, inst: aiosqlite.Row, tx: Tx, settings: FleetSettings
) -> str | None:
    async with conn.execute(
        "SELECT state, count(*) AS n FROM agents WHERE instance_id = ? GROUP BY state",
        (inst["id"],),
    ) as cur:
        by_state = {r["state"]: r["n"] for r in await cur.fetchall()}
    if by_state.get("lost"):
        return "an agent of this instance was lost"
    live = by_state.get("active", 0) + by_state.get("draining", 0)
    if by_state and not live:
        return "all agents left"
    if inst["draining"] and not await _busy(conn, inst["id"]):
        return "drained"  # recycled or scaled down, and nothing is in progress
    running_at = fleet.parse_ts(inst["running_at"])
    if (
        not by_state
        and running_at
        and tx.now - running_at > settings.registration_timeout
    ):
        return "no agent registered in time"
    return None


async def _scale(tx: Tx, pool: Pool, settings: FleetSettings) -> list[_Start | _Stop]:
    async with tx.conn.execute(
        "SELECT * FROM instances WHERE pool = ? AND state != 'terminated'"
        " ORDER BY created_at DESC, id",
        (pool.name,),
    ) as cur:
        live = list(await cur.fetchall())
    desired = min(pool.desired_replicas, pool.max_replicas)
    actions: list[_Start | _Stop] = []
    if len(live) < desired:
        if hold := await _crash_loop_hold(tx, pool.name, settings):
            await _set_status(tx, pool.name, "failing", hold)
            return []
        if settings.argus_url is None:
            await _set_status(
                tx,
                pool.name,
                "failing",
                "ARGUS_PUBLIC_URL is not set: containers cannot reach Argus",
            )
            return []
        # Reservations anywhere (this pass included) are starts in flight.
        async with tx.conn.execute(
            "SELECT count(*) AS n FROM instances WHERE state = 'reserved'"
        ) as cur:
            row = await cur.fetchone()
        in_flight = row["n"] if row else 0
        room = max(0, settings.max_starts_in_flight - in_flight)
        for _ in range(min(desired - len(live), room)):
            instance_id = str(uuid4())
            await tx.conn.execute(
                "INSERT INTO instances (id, pool, provider_ref, state, created_at)"
                " VALUES (?, ?, ?, 'reserved', ?)",
                (instance_id, pool.name, container_name(instance_id), ts(tx.now)),
            )
            await record(
                tx,
                "system",
                "instance",
                instance_id,
                "instance.reserved",
                {"pool": pool.name},
            )
            actions.append(_Start(instance_id))
        return actions

    going = sum(1 for i in live if i["draining"] or i["state"] == "terminating")
    excess = len(live) - desired - going
    if excess <= 0:
        return actions
    # Cheapest first: instances with no container yet, then idle running ones.
    for inst in [i for i in live if i["state"] == "reserved"]:
        if excess == 0:
            break
        await begin_terminating(tx, inst["id"], "scaled down")
        actions.append(_Stop(inst["provider_ref"]))
        excess -= 1
    for inst in [i for i in live if i["state"] == "running" and not i["draining"]]:
        if excess == 0:
            break
        if await _busy(tx.conn, inst["id"]):
            continue  # never interrupt work to scale down
        await tx.conn.execute(
            "UPDATE instances SET draining = 1 WHERE id = ?", (inst["id"],)
        )
        async with tx.conn.execute(
            "SELECT id FROM agents WHERE instance_id = ? AND state = 'active'",
            (inst["id"],),
        ) as cur:
            agent_ids = [r["id"] for r in await cur.fetchall()]
        for agent_id in agent_ids:
            await agents.drain(tx, agent_id, "system")
        if not agent_ids:  # nothing registered yet: nothing to drain, just stop it
            await begin_terminating(tx, inst["id"], "scaled down")
            actions.append(_Stop(inst["provider_ref"]))
        await record(tx, "system", "instance", inst["id"], "instance.draining")
        excess -= 1
    return actions


CRASH_WINDOW = timedelta(minutes=10)
CRASH_LIFETIME = timedelta(seconds=60)
CRASH_FREE_STARTS = 3
CRASH_BACKOFF_BASE = timedelta(seconds=10)


async def _crash_loop_hold(
    tx: Tx, pool_name: str, settings: FleetSettings
) -> str | None:
    """Back off when instances keep exiting soon after they started.

    After CRASH_FREE_STARTS such exits in CRASH_WINDOW, new starts wait
    10 s, 20 s, 40 s, … (max `start_backoff_max`) from the last exit. Returns the
    message to show on the pool while holding, or None to go ahead.
    """
    async with tx.conn.execute(
        "SELECT created_at, terminated_at, exit_reason FROM instances"
        " WHERE pool = ? AND state = 'terminated' AND terminated_at > ?"
        "   AND exit_reason LIKE 'exited%' AND draining = 0"  # a drain is not a crash
        " ORDER BY terminated_at DESC",
        (pool_name, ts(tx.now - CRASH_WINDOW)),
    ) as cur:
        rows = [
            r
            for r in await cur.fetchall()
            if datetime.fromisoformat(r["terminated_at"])
            - datetime.fromisoformat(r["created_at"])
            < CRASH_LIFETIME
        ]
    if len(rows) < CRASH_FREE_STARTS:
        return None
    wait = min(
        CRASH_BACKOFF_BASE * 2 ** (len(rows) - CRASH_FREE_STARTS),
        settings.start_backoff_max,
    )
    last = datetime.fromisoformat(rows[0]["terminated_at"])
    if tx.now - last >= wait:
        return None
    return (
        f"{len(rows)} instances exited within a minute of starting in the last"
        f" {int(CRASH_WINDOW.total_seconds() // 60)} min (last: {rows[0]['exit_reason']});"
        f" holding new starts for {int(wait.total_seconds())} s. See the instance logs."
    )


async def _act(
    db: Database,
    target: Target,
    pool_name: str,
    action: _Start | _Stop,
    settings: FleetSettings,
) -> None:
    if isinstance(action, _Stop):
        try:
            await target.stop(action.ref, grace=settings.stop_grace)
        except TargetError as exc:
            log.warning("stopping %s failed (will retry): %s", action.ref, exc)
        return

    # A fresh bootstrap token per attempt: Argus keeps only hashes, so a token
    # cannot be re-sent; older unused ones for this instance are revoked.
    async with db.write() as tx:
        pool = await fleet.get_pool(tx.conn, pool_name)
        await tx.conn.execute(
            "UPDATE tokens SET revoked_at = ? WHERE revoked_at IS NULL AND kind = 'bootstrap'"
            " AND used_at IS NULL AND instance_id = ?",
            (ts(tx.now), action.instance_id),
        )
        tokens = [
            (
                await agents.create_bootstrap_token(
                    tx,
                    BootstrapTokenCreate(
                        role=Role.WORKER, instance_id=action.instance_id
                    ),
                    "system",
                )
            ).token
            for _ in range(pool.agents_per_instance)
        ]
        await tx.conn.execute(
            "UPDATE instances SET start_attempts = start_attempts + 1,"
            " last_start_at = ?, image = ? WHERE id = ?",
            (ts(tx.now), pool.image, action.instance_id),
        )
    assert settings.argus_url is not None
    try:
        spec = runtimes.container_spec(
            pool,
            action.instance_id,
            tokens,
            settings.argus_url,
            model_policy=_model_policy(pool, settings),
        )
        await target.start(spec)
    except TargetError as exc:
        # Possibly ambiguous: the next pass looks the instance up by name before
        # starting again, so this can never create a second container.
        async with db.write() as tx:
            await tx.conn.execute(
                "UPDATE instances SET last_error = ? WHERE id = ?",
                (str(exc), action.instance_id),
            )
            await _set_status(tx, pool_name, "failing", str(exc))
        return
    # The instance stays `reserved` (an in-flight start) until a provider
    # listing confirms it.
    if pool.status == "failing":
        async with db.write() as tx:
            await _set_status(tx, pool_name, "ok", None)


def _model_policy(pool: Pool, settings: FleetSettings) -> str | None:
    """The canonical policy JSON for a new instance; only its revision is logged."""
    if pool.runtime != "nanoclaw" or settings.model_policy_path is None:
        return None
    try:
        policy, rev = model_policy.effective_env(settings.model_policy_path)
    except ValidationFailedError as exc:
        raise TargetError(f"model policy is invalid, not starting: {exc}") from exc
    log.info("pool %s: new instance gets model policy %s", pool.name, rev[:8])
    return policy


async def begin_terminating(
    tx: Tx,
    instance_id: str,
    reason: str,
    *,
    release: str | None = None,
    actor: str = "system",
) -> None:
    """Request the stop; its agents end now, its slot only when confirmed. Their
    tasks are released with `release` (see `agents.end_instance`)."""
    await tx.conn.execute(
        "UPDATE instances SET state = 'terminating', terminating_at = ?, exit_reason = ?"
        " WHERE id = ? AND state IN ('reserved', 'running')",
        (ts(tx.now), reason, instance_id),
    )
    await agents.end_instance(
        tx,
        instance_id,
        f"instance terminating: {reason}",
        release=release,
        actor=actor,
    )
    await record(
        tx,
        "system",
        "instance",
        instance_id,
        "instance.terminating",
        {"reason": reason},
    )


async def confirm_terminated(tx: Tx, instance_id: str, reason: str) -> None:
    """The provider confirmed the container is gone: free the slot."""
    await tx.conn.execute(
        "UPDATE instances SET state = 'terminated', terminated_at = ?,"
        " exit_reason = coalesce(exit_reason, ?) WHERE id = ?",
        (ts(tx.now), reason, instance_id),
    )
    await agents.end_instance(tx, instance_id, f"instance terminated: {reason}")
    await record(
        tx, "system", "instance", instance_id, "instance.terminated", {"reason": reason}
    )


async def _mark_running(tx: Tx, instance_id: str, ref: str) -> None:
    await tx.conn.execute(
        "UPDATE instances SET state = 'running', provider_ref = ?, running_at = ?,"
        " last_error = NULL WHERE id = ?",
        (ref, ts(tx.now), instance_id),
    )
    await record(tx, "system", "instance", instance_id, "instance.running")


def _start_due(inst: aiosqlite.Row, tx: Tx, settings: FleetSettings) -> bool:
    """Retry starts with exponential backoff (5 s, 10 s, … up to the max)."""
    last = fleet.parse_ts(inst["last_start_at"])
    if last is None:
        return True
    delay = min(
        timedelta(seconds=5 * 2 ** max(inst["start_attempts"] - 1, 0)),
        settings.start_backoff_max,
    )
    return tx.now - last >= delay


async def _busy(conn: aiosqlite.Connection, instance_id: str) -> bool:
    async with conn.execute(
        "SELECT 1 FROM tasks t JOIN agents a ON a.id = t.assignee_agent_id"
        " WHERE a.instance_id = ? AND t.state = 'claimed' LIMIT 1",
        (instance_id,),
    ) as cur:
        return await cur.fetchone() is not None


async def _set_status(tx: Tx, name: str, status: str, error: str | None) -> None:
    await tx.conn.execute(
        "UPDATE pools SET status = ?, last_error = ? WHERE name = ?",
        (status, error, name),
    )


async def run(
    db: Database,
    targets: TargetRegistry,
    settings: FleetSettings,
    interval: float = 5.0,
    stats: LoopStats | None = None,
) -> None:
    while True:
        started = time.monotonic()
        ok = True
        try:
            await reconcile(db, targets, settings)
        except Exception:  # a whole pass failing (not one pool) must not end the loop
            log.exception("reconcile pass failed")
            ok = False
        if stats:
            stats.record(time.monotonic() - started, ok=ok)
        await asyncio.sleep(interval)
