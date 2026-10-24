"""Background loop: expire overdue leases and mark silent agents lost (3.4)."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from argus_api.accounts import service as accounts
from argus_api.agents import service as agents
from argus_api.db import Database, Tx
from argus_api.ops import LoopStats
from argus_api.tasks import service as tasks

log = logging.getLogger(__name__)

REAP_INTERVAL_S = 10.0


@dataclass(frozen=True)
class ReapResult:
    expired_tasks: list[str]
    lost_agents: list[str]
    recovered_accounts: list[str]
    failed_tasks: list[str] = field(default_factory=list)
    failed_agents: list[str] = field(default_factory=list)
    failed_stages: list[str] = field(default_factory=list)
    failed_quiet: list[str] = field(default_factory=list)
    quieted_tasks: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (
            self.failed_tasks
            or self.failed_agents
            or self.failed_stages
            or self.failed_quiet
        )


async def reap(
    tx: Tx,
    local: tuple[bool | None, str] = (None, ""),
    *,
    quiet_claim_s: int = tasks.QUIET_CLAIM_S,
    stall_fail_s: int = 0,
) -> ReapResult:
    """One pass (3.4). The stages share the transaction but not their fate: each
    runs in a savepoint, and so does every task or agent inside lease/timeout
    expiry and loss, so one bad row or stage never keeps leases from expiring.

    `local` is the local model's probe, taken before the transaction opened:
    reachable (or not), with the detail. `(None, "")` means it was not probed.
    `quiet_claim_s` flags silent claims; `stall_fail_s` (0: off) then expires a flagged
    claim that stays silent that long (stall visibility 3.6)."""
    failed_tasks: list[str] = []
    failed_agents: list[str] = []
    failed_stages: list[str] = []
    failed_quiet: list[str] = []

    async def stage(name: str, call: Callable[[], Awaitable[list[str]]]) -> list[str]:
        try:
            async with tx.savepoint():
                return await call()
        except Exception:
            log.exception("reaper stage %s failed; continuing", name)
            failed_stages.append(name)
            return []

    # Lease expiry runs first and independently: a live agent's overdue lease
    # expires too. Then lost agents release whatever leases they still hold.
    expired = await stage(
        "expire_leases", lambda: tasks.expire_leases(tx, failed_tasks)
    )
    expired += await stage(
        "expire_timeouts", lambda: tasks.expire_timeouts(tx, failed_tasks)
    )
    expired += await stage(
        "expire_stalled", lambda: tasks.expire_stalled(tx, stall_fail_s, failed_tasks)
    )
    lost = await stage("mark_lost", lambda: agents.mark_lost(tx, failed_agents))
    quieted = await stage(
        "flag_quiet",
        lambda: tasks.flag_quiet(tx, quiet_claim_s, failed_quiet),
    )
    recovered = await stage("accounts.recover", lambda: accounts.recover(tx))
    recovered += await stage(
        "accounts.local_health", lambda: accounts.sync_local_health(tx, *local)
    )
    return ReapResult(
        expired,
        lost,
        recovered,
        failed_tasks,
        failed_agents,
        failed_stages,
        failed_quiet,
        quieted,
    )


async def run(
    db: Database,
    interval: float = REAP_INTERVAL_S,
    stats: LoopStats | None = None,
    local_probe: Callable[[], Awaitable[tuple[bool | None, str]]] | None = None,
    quiet_claim_s: int = tasks.QUIET_CLAIM_S,
    stall_fail_s: int = 0,
) -> None:
    while True:
        started = time.monotonic()
        try:
            local: tuple[bool | None, str] = (None, "")
            if local_probe is not None:
                # Outside the write transaction by design: no network call in one.
                try:
                    local = await local_probe()
                except Exception:
                    log.exception(
                        "local model probe failed; leaving accounts as they are"
                    )
            async with db.write() as tx:
                result = await reap(
                    tx, local, quiet_claim_s=quiet_claim_s, stall_fail_s=stall_fail_s
                )
            if stats:
                stats.record(time.monotonic() - started)
            if not result.clean:
                log.error(
                    "reaper: failed tasks=%s agents=%s stages=%s quiet=%s",
                    result.failed_tasks,
                    result.failed_agents,
                    result.failed_stages,
                    result.failed_quiet,
                )
            if result.expired_tasks or result.lost_agents or result.quieted_tasks:
                log.info(
                    "reaper: expired %d leases, lost %d agents, quieted %d",
                    len(result.expired_tasks),
                    len(result.lost_agents),
                    len(result.quieted_tasks),
                )
        except Exception:
            log.exception("reaper pass failed")
            if stats:
                stats.record(time.monotonic() - started, ok=False)
        await asyncio.sleep(interval)
