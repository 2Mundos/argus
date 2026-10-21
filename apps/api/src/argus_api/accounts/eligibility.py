"""The one rule for "can an engine account serve a claim now" (account selection
3.6). Claim gate, overview, refill time, task release and the lease ranking all
read the registry through it, so none can disagree.

An account counts toward the engine pool only if it is an engine provider
(anthropic, openai, custom, local: never an executor-only one such as Cursor,
never the repository account). The local model has no quota, so it is never
held back; a disabled or unreachable account is not in the pool at all. It is *usable* when available, or exhausted past its reset;
*held back* while exhausted with a reset in the future or none stated."""

from dataclasses import dataclass
from datetime import datetime

import aiosqlite
from argus_core.accounts import ENGINE_PROVIDERS, LOCAL_PROVIDER


@dataclass(frozen=True)
class EnginePool:
    usable: int
    exhausted: int
    next_reset: datetime | None  # earliest stated reset of a held-back account

    @property
    def awaiting_quota(self) -> bool:
        """Capacity is spent and none is left: a claim would only burn an attempt."""
        return self.exhausted > 0 and self.usable == 0


async def engine_pool(conn: aiosqlite.Connection, now: datetime | None) -> EnginePool:
    """`now` None: judge by state alone (an exhausted account stays held back until
    the recovery loop frees it), for readers that have no clock."""
    async with conn.execute(
        "SELECT provider, state, exhausted_until FROM accounts"
        " WHERE state IN ('available', 'exhausted')"
    ) as cur:
        rows = await cur.fetchall()
    usable = exhausted = 0
    marks: list[datetime] = []
    for r in rows:
        if r["provider"] not in ENGINE_PROVIDERS:
            continue
        if r["provider"] == LOCAL_PROVIDER:
            usable += 1
            continue
        until = (
            datetime.fromisoformat(r["exhausted_until"])
            if r["exhausted_until"]
            else None
        )
        if r["state"] == "exhausted" and (until is None or now is None or until > now):
            exhausted += 1
            if until is not None:
                marks.append(until)
        else:
            usable += 1
    return EnginePool(usable, exhausted, min(marks, default=None))
