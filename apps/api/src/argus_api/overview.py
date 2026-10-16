"""Fleet-wide totals for the dashboard Home (docs/specs/home.spec.md).

Every number is a `GROUP BY` over the whole table, read in one snapshot
transaction, so totals are never limited by a list's page size and never drift
between sections. Every enum state is present, with an explicit 0 when none.
Nothing here contains a credential, a secret reference, or free text.
"""

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Any

from argus_core.accounts import AccountState
from argus_core.agents import AgentState, Role
from argus_core.decisions import Audience, DecisionKind, DecisionState
from argus_core.fleet import InstanceState, PoolStatus
from argus_core.knowledge import PageState, ProposalState
from argus_core.tasks import TaskState
from pydantic import BaseModel

from argus_api.db import Database, utcnow

Counts = dict[str, int]


class PoolItem(BaseModel):
    name: str
    status: str
    desired: int
    max: int
    live: int  # instances not terminated (the same definition as /api/metrics)
    running: int


class PoolsOverview(BaseModel):
    by_status: Counts
    total: int
    desired: int
    max: int
    live: int
    running: int
    items: list[PoolItem]


class AgentsOverview(BaseModel):
    by_state: Counts
    # role -> state -> count; the role is what the agent registered as.
    by_role: dict[str, Counts]
    # Live agents that hold at least one claimed task. It says nothing about
    # whether the agent's model is reachable or working.
    holding_claims: int
    # Lost agents whose instance has not been terminated yet: a rollout still
    # in progress. The rest of `by_state.lost` is history.
    lost_on_unfinished_instance: int


class InstancesOverview(BaseModel):
    by_state: Counts
    draining: int  # not terminated, and being drained


class AccountsOverview(BaseModel):
    by_state: Counts
    total: int
    by_provider: dict[str, Counts]
    next_reset_at: datetime | None  # earliest `exhausted_until`, if any
    # Argus stores no quota or limit for an account, so none is reported.
    quota_measured: bool


class DecisionsOverview(BaseModel):
    by_state: Counts
    open_by_kind: Counts
    open_by_audience: Counts


class KnowledgeOverview(BaseModel):
    pages_by_state: Counts
    proposals_by_state: Counts


class Overview(BaseModel):
    as_of: datetime
    tasks: Counts
    agents: AgentsOverview
    pools: PoolsOverview
    instances: InstancesOverview
    accounts: AccountsOverview
    decisions: DecisionsOverview
    knowledge: KnowledgeOverview


def _zeros(states: Iterable[StrEnum]) -> Counts:
    return {s.value: 0 for s in states}


def _filled(states: type[StrEnum], rows: Iterable[tuple[Any, ...]]) -> Counts:
    """One entry per enum member (0 when absent), plus any state the table holds."""
    counts = _zeros(states)
    for state, n in rows:
        counts[state] = n
    return counts


async def overview(db: Database) -> Overview:
    now = utcnow()
    async with db.read() as conn:
        await conn.execute("BEGIN")  # one snapshot for every query below
        try:

            async def grouped(sql: str, *params: Any) -> list[tuple[Any, ...]]:
                async with conn.execute(sql, params) as cur:
                    return [tuple(r) for r in await cur.fetchall()]

            async def scalar(sql: str) -> Any:
                return (await grouped(sql))[0][0]

            tasks = _filled(
                TaskState,
                await grouped("SELECT state, count(*) FROM tasks GROUP BY state"),
            )

            agents_by_state = _zeros(AgentState)
            agents_by_role = {r.value: _zeros(AgentState) for r in Role}
            for role, state, n in await grouped(
                "SELECT role, state, count(*) FROM agents GROUP BY role, state"
            ):
                agents_by_state[state] = agents_by_state.get(state, 0) + n
                agents_by_role.setdefault(role, _zeros(AgentState))[state] = n
            holding = await scalar(
                "SELECT count(DISTINCT t.assignee_agent_id) FROM tasks t"
                " JOIN agents a ON a.id = t.assignee_agent_id"
                " WHERE t.state = 'claimed' AND a.state IN ('active', 'draining')"
            )
            lost_unfinished = await scalar(
                "SELECT count(*) FROM agents a JOIN instances i ON i.id = a.instance_id"
                " WHERE a.state = 'lost' AND i.state != 'terminated'"
            )

            instances = _filled(
                InstanceState,
                await grouped("SELECT state, count(*) FROM instances GROUP BY state"),
            )
            draining = await scalar(
                "SELECT count(*) FROM instances"
                " WHERE state != 'terminated' AND draining = 1"
            )
            per_pool = {
                (pool, state): n
                for pool, state, n in await grouped(
                    "SELECT pool, state, count(*) FROM instances GROUP BY pool, state"
                )
            }
            pool_items = [
                PoolItem(
                    name=name,
                    status=status,
                    desired=desired,
                    max=maximum,
                    live=sum(
                        n
                        for (p, s), n in per_pool.items()
                        if p == name and s != InstanceState.TERMINATED
                    ),
                    running=per_pool.get((name, InstanceState.RUNNING), 0),
                )
                for name, desired, maximum, status in await grouped(
                    "SELECT name, desired_replicas, max_replicas, status"
                    " FROM pools ORDER BY name"
                )
            ]

            accounts_by_provider: dict[str, Counts] = {}
            accounts = _zeros(AccountState)
            for provider, state, n in await grouped(
                "SELECT provider, state, count(*) FROM accounts GROUP BY provider, state"
            ):
                accounts[state] = accounts.get(state, 0) + n
                accounts_by_provider.setdefault(provider, _zeros(AccountState))[
                    state
                ] = n
            next_reset = await scalar(
                "SELECT min(exhausted_until) FROM accounts WHERE state = 'exhausted'"
            )

            decisions = _filled(
                DecisionState,
                await grouped("SELECT state, count(*) FROM decisions GROUP BY state"),
            )
            open_by_kind = _filled(
                DecisionKind,
                await grouped(
                    "SELECT kind, count(*) FROM decisions WHERE state = 'open'"
                    " GROUP BY kind"
                ),
            )
            open_by_audience = _filled(
                Audience,
                await grouped(
                    "SELECT audience, count(*) FROM decisions WHERE state = 'open'"
                    " GROUP BY audience"
                ),
            )
            pages = _filled(
                PageState,
                await grouped(
                    "SELECT state, count(*) FROM knowledge_pages GROUP BY state"
                ),
            )
            proposals = _filled(
                ProposalState,
                await grouped(
                    "SELECT state, count(*) FROM knowledge_proposals GROUP BY state"
                ),
            )
        finally:
            await conn.execute("COMMIT")

    return Overview(
        as_of=now,
        tasks=tasks,
        agents=AgentsOverview(
            by_state=agents_by_state,
            by_role=agents_by_role,
            holding_claims=holding,
            lost_on_unfinished_instance=lost_unfinished,
        ),
        pools=PoolsOverview(
            by_status=_pool_status_counts(pool_items),
            total=len(pool_items),
            desired=sum(p.desired for p in pool_items),
            max=sum(p.max for p in pool_items),
            live=sum(p.live for p in pool_items),
            running=sum(p.running for p in pool_items),
            items=pool_items,
        ),
        instances=InstancesOverview(by_state=instances, draining=draining),
        accounts=AccountsOverview(
            by_state=accounts,
            total=sum(accounts.values()),
            by_provider=accounts_by_provider,
            next_reset_at=datetime.fromisoformat(next_reset) if next_reset else None,
            quota_measured=False,
        ),
        decisions=DecisionsOverview(
            by_state=decisions,
            open_by_kind=open_by_kind,
            open_by_audience=open_by_audience,
        ),
        knowledge=KnowledgeOverview(pages_by_state=pages, proposals_by_state=proposals),
    )


def _pool_status_counts(items: list[PoolItem]) -> Counts:
    counts = _zeros(PoolStatus)
    for p in items:
        counts[p.status] = counts.get(p.status, 0) + 1
    return counts
