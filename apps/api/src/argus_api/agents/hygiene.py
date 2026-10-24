"""Lost/gone aggregates and the lost-spike rule (docs/specs/agent-hygiene.spec.md)."""

from datetime import date, datetime, timedelta
from typing import Any

import aiosqlite
from argus_core.agents import AgentState
from pydantic import BaseModel

from argus_api.config import Settings

AGE_BUCKETS = ("under_1d", "1d_to_7d", "7d_to_30d", "30d_and_over")


class HygieneGroup(BaseModel):
    pool: str
    state: AgentState
    day: date
    count: int
    oldest_heartbeat_at: datetime
    newest_heartbeat_at: datetime
    max_age_hours: float


class HygieneTotals(BaseModel):
    lost: int
    gone: int
    archived: int
    lost_holding_claims: int


class ClaimHoldingAgent(BaseModel):
    agent_id: str
    task_id: str


class LostSpike(BaseModel):
    window_hours: int
    current: int
    previous: int
    min_count: int
    factor: float
    triggered: bool


class AgentHygiene(BaseModel):
    as_of: datetime
    totals: HygieneTotals
    groups: list[HygieneGroup]
    claim_holders: list[ClaimHoldingAgent]
    lost_spike: LostSpike
    # state -> bucket -> count (lost and gone only; archived excluded)
    age_buckets: dict[str, dict[str, int]]


def lost_spike(
    ended_at_values: list[datetime],
    *,
    now: datetime,
    window_hours: int,
    min_count: int,
    factor: float,
) -> LostSpike:
    """Count newly lost agents in the current window vs the equal window before.

    Current: `(now - window, now]`. Previous: `(now - 2*window, now - window]`.
    Triggers when `current >= min_count` and `current > factor * previous`.
    """
    window = timedelta(hours=window_hours)
    cur_start = now - window
    prev_start = now - 2 * window
    current = sum(1 for t in ended_at_values if cur_start < t <= now)
    previous = sum(1 for t in ended_at_values if prev_start < t <= cur_start)
    return LostSpike(
        window_hours=window_hours,
        current=current,
        previous=previous,
        min_count=min_count,
        factor=factor,
        triggered=current >= min_count and current > factor * previous,
    )


def _age_bucket(age_days: float) -> str:
    if age_days < 1:
        return "under_1d"
    if age_days < 7:
        return "1d_to_7d"
    if age_days < 30:
        return "7d_to_30d"
    return "30d_and_over"


def _empty_buckets() -> dict[str, int]:
    return dict.fromkeys(AGE_BUCKETS, 0)


async def agent_hygiene(
    conn: aiosqlite.Connection, *, now: datetime, settings: Settings
) -> AgentHygiene:
    """One-snapshot aggregates over lost/gone identities (archived excluded)."""
    await conn.execute("BEGIN")
    try:

        async def grouped(sql: str, *params: Any) -> list[tuple[Any, ...]]:
            async with conn.execute(sql, params) as cur:
                return [tuple(r) for r in await cur.fetchall()]

        async def scalar(sql: str, *params: Any) -> Any:
            return (await grouped(sql, *params))[0][0]

        lost = await scalar(
            "SELECT count(*) FROM agents WHERE state = 'lost' AND archived_at IS NULL"
        )
        gone = await scalar(
            "SELECT count(*) FROM agents WHERE state = 'gone' AND archived_at IS NULL"
        )
        archived = await scalar(
            "SELECT count(*) FROM agents WHERE archived_at IS NOT NULL"
        )
        lost_holding = await scalar(
            "SELECT count(DISTINCT a.id) FROM agents a"
            " JOIN tasks t ON t.assignee_agent_id = a.id"
            " WHERE a.state = 'lost' AND t.state = 'claimed'"
        )

        holders = [
            ClaimHoldingAgent(agent_id=agent_id, task_id=task_id)
            for task_id, agent_id in await grouped(
                "SELECT t.id, t.assignee_agent_id FROM tasks t"
                " JOIN agents a ON a.id = t.assignee_agent_id"
                " WHERE t.state = 'claimed' AND a.state = 'lost'"
                " ORDER BY t.assignee_agent_id, t.id"
            )
        ]

        group_rows = await grouped(
            "SELECT COALESCE(i.pool, 'unknown') AS pool, a.state,"
            " date(a.ended_at) AS day, count(*), min(a.last_heartbeat_at),"
            " max(a.last_heartbeat_at)"
            " FROM agents a LEFT JOIN instances i ON i.id = a.instance_id"
            " WHERE a.state IN ('lost', 'gone') AND a.archived_at IS NULL"
            " GROUP BY pool, a.state, day"
            " ORDER BY pool, a.state, day DESC"
        )
        groups: list[HygieneGroup] = []
        for pool, state, day, count, oldest, newest in group_rows:
            oldest_dt = datetime.fromisoformat(oldest)
            groups.append(
                HygieneGroup(
                    pool=pool,
                    state=AgentState(state),
                    day=date.fromisoformat(day),
                    count=count,
                    oldest_heartbeat_at=oldest_dt,
                    newest_heartbeat_at=datetime.fromisoformat(newest),
                    max_age_hours=(now - oldest_dt).total_seconds() / 3600,
                )
            )

        age_buckets: dict[str, dict[str, int]] = {
            AgentState.LOST: _empty_buckets(),
            AgentState.GONE: _empty_buckets(),
        }
        for state, heartbeat in await grouped(
            "SELECT state, last_heartbeat_at FROM agents"
            " WHERE state IN ('lost', 'gone') AND archived_at IS NULL"
        ):
            age_days = (now - datetime.fromisoformat(heartbeat)).total_seconds() / 86400
            age_buckets[state][_age_bucket(age_days)] += 1

        ended = [
            datetime.fromisoformat(row[0])
            for row in await grouped(
                "SELECT ended_at FROM agents WHERE state = 'lost' AND ended_at IS NOT NULL"
            )
        ]
        spike = lost_spike(
            ended,
            now=now,
            window_hours=settings.lost_spike_window_hours,
            min_count=settings.lost_spike_min_count,
            factor=settings.lost_spike_factor,
        )
    finally:
        await conn.execute("COMMIT")

    return AgentHygiene(
        as_of=now,
        totals=HygieneTotals(
            lost=lost,
            gone=gone,
            archived=archived,
            lost_holding_claims=lost_holding,
        ),
        groups=groups,
        claim_holders=holders,
        lost_spike=spike,
        age_buckets=age_buckets,
    )


def hygiene_by_pool_buckets(
    rows: list[tuple[str, str, datetime]], *, now: datetime
) -> dict[str, dict[str, int]]:
    """`"<state>:<pool>"` → age-bucket counts for lost/gone (archived already excluded)."""
    out: dict[str, dict[str, int]] = {}
    for state, pool, heartbeat in rows:
        key = f"{state}:{pool}"
        buckets = out.setdefault(key, _empty_buckets())
        age_days = (now - heartbeat).total_seconds() / 86400
        buckets[_age_bucket(age_days)] += 1
    return out
