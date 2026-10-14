"""Background loop: expire overdue leases and mark silent agents lost (3.4)."""

import asyncio
import logging
import time
from dataclasses import dataclass

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


async def reap(tx: Tx) -> ReapResult:
    # Lease expiry runs first and independently: a live agent's overdue lease
    # expires too. Then lost agents release whatever leases they still hold.
    expired = await tasks.expire_leases(tx)
    expired += await tasks.expire_timeouts(tx)
    lost = await agents.mark_lost(tx)
    recovered = await accounts.recover(tx)
    return ReapResult(expired, lost, recovered)


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
