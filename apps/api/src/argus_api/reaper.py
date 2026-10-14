"""Background loop: expire overdue leases and mark silent agents lost (3.4)."""

import asyncio
import logging
from dataclasses import dataclass

from argus_api.agents import service as agents
from argus_api.db import Database, Tx
from argus_api.tasks import service as tasks

log = logging.getLogger(__name__)

REAP_INTERVAL_S = 10.0


@dataclass(frozen=True)
class ReapResult:
    expired_tasks: list[str]
    lost_agents: list[str]


async def reap(tx: Tx) -> ReapResult:
    # Lease expiry runs first and independently: a live agent's overdue lease
    # expires too. Then lost agents release whatever leases they still hold.
    expired = await tasks.expire_leases(tx)
    lost = await agents.mark_lost(tx)
    return ReapResult(expired, lost)


async def run(db: Database, interval: float = REAP_INTERVAL_S) -> None:
    while True:
        try:
            async with db.write() as tx:
                result = await reap(tx)
            if result.expired_tasks or result.lost_agents:
                log.info(
                    "reaper: expired %d leases, lost %d agents",
                    len(result.expired_tasks),
                    len(result.lost_agents),
                )
        except Exception:
            log.exception("reaper pass failed")
        await asyncio.sleep(interval)
