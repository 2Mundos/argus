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

    @property
    def clean(self) -> bool:
        return not (self.failed_tasks or self.failed_agents or self.failed_stages)


async def reap(tx: Tx) -> ReapResult:
    """One pass (3.4). The stages share the transaction but not their fate: each
    runs in a savepoint, and so does every task or agent inside lease/timeout
    expiry and loss, so one bad row or stage never keeps leases from expiring."""
    failed_tasks: list[str] = []
    failed_agents: list[str] = []
    failed_stages: list[str] = []

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
    lost = await stage("mark_lost", lambda: agents.mark_lost(tx, failed_agents))
    recovered = await stage("accounts.recover", lambda: accounts.recover(tx))
    return ReapResult(
        expired, lost, recovered, failed_tasks, failed_agents, failed_stages
    )


async def run(
    db: Database, interval: float = REAP_INTERVAL_S, stats: LoopStats | None = None
) -> None:
    while True:
        started = time.monotonic()
        try:
            async with db.write() as tx:
                result = await reap(tx)
            if stats:
                stats.record(time.monotonic() - started)
            if not result.clean:
                log.error(
                    "reaper: failed tasks=%s agents=%s stages=%s",
                    result.failed_tasks,
                    result.failed_agents,
                    result.failed_stages,
                )
            if result.expired_tasks or result.lost_agents:
                log.info(
                    "reaper: expired %d leases, lost %d agents",
                    len(result.expired_tasks),
                    len(result.lost_agents),
                )
        except Exception:
            log.exception("reaper pass failed")
            if stats:
                stats.record(time.monotonic() - started, ok=False)
        await asyncio.sleep(interval)
