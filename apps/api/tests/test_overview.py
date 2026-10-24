"""GET /api/overview: complete totals, zeros, scopes, no secrets (home.spec.md)."""

from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api import overview
from argus_api.db import Database, ts
from argus_api.main import app
from argus_core.accounts import AccountState
from argus_core.agents import AgentState, Role
from argus_core.decisions import Audience, DecisionKind, DecisionState
from argus_core.fleet import InstanceState, PoolStatus
from argus_core.knowledge import PageState, ProposalState
from argus_core.tasks import TaskState
from fastapi.testclient import TestClient

ADMIN_TOKEN = "overview-admin-token-" + "v" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
SECRET_REF = "SECRET-REF-SENTINEL"
PAGE_SIZE_LIMIT = 500  # GET /api/tasks never returns more than this


async def add_task(db: Database, n: int, state: str, agent: str | None = None) -> None:
    async with db.write() as tx:
        stamp = ts(tx.now)
        await tx.conn.execute(
            "INSERT INTO tasks (id, kind, title, spec, acceptance, input,"
            " required_capabilities, priority, state, requires_approval, review,"
            " assignee_agent_id, lease_expires_at, lease_ttl_s, max_attempts,"
            " created_by, created_at, updated_at, project)"
            " VALUES (?, 'generic', 't', '', '[]', '{}', '[]', 0, ?, 0, 'none',"
            " ?, ?, 60, 3, 'test', ?, ?, 'global')",
            (f"t{n}", state, agent, stamp if agent else None, stamp, stamp),
        )


async def add_agent(
    db: Database,
    agent_id: str,
    state: str = "active",
    role: str = "worker",
    instance: str | None = None,
) -> None:
    async with db.write() as tx:
        stamp = ts(tx.now)
        ended = stamp if state in ("gone", "lost") else None
        await tx.conn.execute(
            "INSERT INTO agents (id, name, role, instance_id, capabilities, state,"
            " last_heartbeat_at, created_at, ended_at)"
            " VALUES (?, ?, ?, ?, '[]', ?, ?, ?, ?)",
            (agent_id, agent_id, role, instance, state, stamp, stamp, ended),
        )


async def add_pool(db: Database, name: str, desired: int, status: str = "ok") -> None:
    async with db.write() as tx:
        stamp = ts(tx.now)
        await tx.conn.execute(
            "INSERT INTO pools (name, target, runtime, image, capabilities,"
            " desired_replicas, max_replicas, config, status, created_at, updated_at)"
            " VALUES (?, 'docker', 'plain', 'img', '[]', ?, 5, '{}', ?, ?, ?)",
            (name, desired, status, stamp, stamp),
        )


async def add_instance(db: Database, iid: str, pool: str, state: str) -> None:
    async with db.write() as tx:
        stamp = ts(tx.now)
        await tx.conn.execute(
            "INSERT INTO instances (id, pool, provider_ref, state, created_at,"
            " terminated_at) VALUES (?, ?, 'ref', ?, ?, ?)",
            (iid, pool, state, stamp, stamp if state == "terminated" else None),
        )


async def add_account(
    db: Database,
    name: str,
    provider: str,
    state: str,
    until: str = "2099-01-01T00:00:00.000000Z",
) -> None:
    async with db.write() as tx:
        stamp = ts(tx.now)
        await tx.conn.execute(
            "INSERT INTO accounts (id, name, provider, kind, secret_ref, state,"
            " exhausted_until, disabled_reason, created_at, updated_at)"
            " VALUES (?, ?, ?, 'api_key', ?, ?, ?, ?, ?, ?)",
            (
                name,
                name,
                provider,
                SECRET_REF,
                state,
                until if state == "exhausted" else None,
                "test" if state == "disabled" else None,
                stamp,
                stamp,
            ),
        )


async def add_decision(db: Database, n: int, kind: str, state: str, audience: str):
    async with db.write() as tx:
        stamp = ts(tx.now)
        await tx.conn.execute(
            "INSERT INTO decisions (id, subject_type, subject_id, kind, question,"
            " audience, state, requested_by, created_at, decided_at)"
            " VALUES (?, 'task', 'x', ?, 'q', ?, ?, 'test', ?, ?)",
            (f"d{n}", kind, audience, state, stamp, None if state == "open" else stamp),
        )


# -- zeros: every state of every enum is present -----------------------------------------


async def test_empty_database_reports_every_state_as_zero(db: Database) -> None:
    o = await overview.overview(db)
    assert o.tasks == {s.value: 0 for s in TaskState}
    assert o.agents.by_state == {s.value: 0 for s in AgentState}
    assert set(o.agents.by_role) == {r.value for r in Role}
    assert all(c == {s.value: 0 for s in AgentState} for c in o.agents.by_role.values())
    assert o.agents.holding_claims == 0
    assert o.agents.lost_on_unfinished_instance == 0
    assert o.agents.lost_holding_claims == 0
    assert o.agents.lost_spike.triggered is False
    assert o.agents.hygiene_by_pool == {}
    assert o.instances.by_state == {s.value: 0 for s in InstanceState}
    assert o.pools.by_status == {s.value: 0 for s in PoolStatus}
    assert (o.pools.total, o.pools.desired, o.pools.live, o.pools.items) == (
        0,
        0,
        0,
        [],
    )
    assert o.accounts.by_state == {s.value: 0 for s in AccountState}
    assert o.accounts.next_reset_at is None
    assert o.accounts.quota_measured is False
    assert o.decisions.by_state == {s.value: 0 for s in DecisionState}
    assert o.decisions.open_by_kind == {s.value: 0 for s in DecisionKind}
    assert o.decisions.open_by_audience == {s.value: 0 for s in Audience}
    assert o.knowledge.pages_by_state == {s.value: 0 for s in PageState}
    assert o.knowledge.proposals_by_state == {s.value: 0 for s in ProposalState}


# -- totals are not capped by a page ------------------------------------------------------


async def test_totals_exceed_the_task_list_page_size(db: Database) -> None:
    n = PAGE_SIZE_LIMIT + 7
    async with db.write() as tx:
        stamp = ts(tx.now)
        await tx.conn.executemany(
            "INSERT INTO tasks (id, kind, title, spec, acceptance, input,"
            " required_capabilities, priority, state, requires_approval, review,"
            " lease_ttl_s, max_attempts, created_by, created_at, updated_at, project)"
            " VALUES (?, 'generic', 't', '', '[]', '{}', '[]', 0, 'succeeded', 0,"
            " 'none', 60, 3, 'test', ?, ?, 'global')",
            [(f"bulk{i}", stamp, stamp) for i in range(n)],
        )
    await add_task(db, 1, "failed")
    o = await overview.overview(db)
    assert o.tasks["succeeded"] == n > PAGE_SIZE_LIMIT
    assert o.tasks["failed"] == 1
    assert sum(o.tasks.values()) == n + 1


# -- every dimension, with mixed data ----------------------------------------------------


async def test_counts_by_state_role_pool_and_provider(db: Database) -> None:
    for i, state in enumerate(TaskState):
        if state is not TaskState.CLAIMED:
            await add_task(db, i, state.value)

    await add_pool(db, "alpha", desired=2)
    await add_pool(db, "beta", desired=1, status="failing")
    await add_instance(db, "i1", "alpha", "running")
    await add_instance(db, "i2", "alpha", "terminating")
    await add_instance(db, "i3", "alpha", "terminated")
    await add_instance(db, "i4", "beta", "terminated")
    await add_instance(db, "i5", "beta", "running")

    await add_agent(db, "w1", "active", "worker", "i1")
    await add_agent(db, "w2", "draining", "worker")
    await add_agent(db, "o1", "active", "orchestrator")
    await add_agent(db, "old", "lost", "worker", "i3")  # replaced rollout: history
    await add_agent(db, "stuck", "lost", "worker", "i2")  # rollout still ending
    await add_agent(db, "nofleet", "lost", "worker")  # no instance: history
    await add_agent(db, "left", "gone", "worker")
    await add_task(db, 900, "claimed", agent="w1")
    await add_task(db, 901, "claimed", agent="w1")  # same agent: counts once
    await add_task(db, 902, "claimed", agent="old")  # lost agent holds nothing

    await add_account(db, "a1", "anthropic", "available")
    await add_account(db, "a2", "anthropic", "exhausted")
    await add_account(db, "a3", "openai", "disabled")

    await add_decision(db, 1, "question", "open", "human")
    await add_decision(db, 2, "knowledge", "open", "any")
    await add_decision(db, 3, "approval", "answered", "human")

    async with db.write() as tx:
        stamp = ts(tx.now)
        for i, state in enumerate(("active", "archived", "archived")):
            await tx.conn.execute(
                "INSERT INTO knowledge_pages (id, project, slug, title, summary, tags,"
                " current_revision_id, current_number, state, reviewed_at,"
                " created_at, updated_at) VALUES (?, 'global', ?, 't', 's', '[]',"
                " 'r', 1, ?, ?, ?, ?)",
                (f"p{i}", f"p{i}", state, stamp, stamp, stamp),
            )
        await tx.conn.execute(
            "INSERT INTO knowledge_proposals (id, project, slug, title, summary,"
            " body, tags, author, reason, sources, state, created_at)"
            " VALUES ('pr1', 'global', 's', 't', 's', 'b', '[]', 'a', 'r', '[]',"
            " 'open', ?)",
            (stamp,),
        )

    o = await overview.overview(db)

    assert o.tasks["claimed"] == 3 and o.tasks["ready"] == 1
    assert o.agents.by_state == {
        "active": 2,
        "draining": 1,
        "gone": 1,
        "lost": 3,
    }
    assert o.agents.by_role["orchestrator"]["active"] == 1
    assert o.agents.by_role["worker"]["lost"] == 3
    assert o.agents.holding_claims == 1
    assert o.agents.lost_on_unfinished_instance == 1
    # Task 902 is claimed by lost agent "old" (inserted to prove holding_claims
    # ignores it); lost_holding_claims surfaces that inconsistency.
    assert o.agents.lost_holding_claims == 1
    assert isinstance(o.agents.hygiene_by_pool, dict)

    assert o.instances.by_state == {
        "reserved": 0,
        "running": 2,
        "terminating": 1,
        "terminated": 2,
    }
    assert o.pools.by_status == {"ok": 1, "unreachable": 0, "failing": 1}
    by_name = {p.name: p for p in o.pools.items}
    assert (by_name["alpha"].live, by_name["alpha"].running) == (2, 1)  # + terminating
    assert (by_name["beta"].live, by_name["beta"].running) == (1, 1)
    assert (o.pools.desired, o.pools.live, o.pools.running) == (3, 3, 2)

    assert o.accounts.by_state == {"available": 1, "exhausted": 1, "disabled": 1}
    assert o.accounts.by_provider["anthropic"] == {
        "available": 1,
        "exhausted": 1,
        "disabled": 0,
    }
    assert o.accounts.next_reset_at is not None

    assert o.decisions.by_state == {"open": 2, "answered": 1, "cancelled": 0}
    assert o.decisions.open_by_kind["knowledge"] == 1
    assert o.decisions.open_by_kind["approval"] == 0  # answered, not open
    assert o.decisions.open_by_audience == {"human": 1, "orchestrator": 0, "any": 1}
    assert o.knowledge.pages_by_state == {"active": 1, "archived": 2}
    assert o.knowledge.proposals_by_state["open"] == 1


async def test_next_reset_ignores_an_account_already_past_its_reset(
    db: Database,
) -> None:
    # Past its reset but not yet recovered: usable now, so no pending reset (the
    # claim gate judges it the same way).
    await add_account(
        db, "spent", "anthropic", "exhausted", until="2020-01-01T00:00:00.000000Z"
    )
    o = await overview.overview(db)
    assert (o.accounts.next_reset_at, o.accounts.awaiting_quota) == (None, False)


async def test_overview_only_reads(db: Database) -> None:
    before = await db_events(db)
    await overview.overview(db)
    assert await db_events(db) == before


async def db_events(db: Database) -> int:
    async with db.read() as conn, conn.execute("SELECT count(*) FROM events") as cur:
        row = await cur.fetchone()
    return row[0]


# -- HTTP: scopes and secrets ---------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        yield c


def agent_headers(client: TestClient, role: str) -> dict[str, str]:
    boot = client.post(
        "/api/agents/bootstrap-tokens", json={"role": role}, headers=ADMIN
    ).json()["token"]
    token = client.post("/api/agents/register", json={"bootstrap_token": boot}).json()[
        "token"
    ]
    return {"Authorization": f"Bearer {token}"}


def test_overview_scopes(client: TestClient) -> None:
    assert client.get("/api/overview").status_code == 401
    assert (
        client.get(
            "/api/overview", headers={"Authorization": "Bearer nope"}
        ).status_code
        == 401
    )
    assert (
        client.get("/api/overview", headers=agent_headers(client, "worker")).status_code
        == 403
    )
    for who in (ADMIN, agent_headers(client, "orchestrator")):
        res = client.get("/api/overview", headers=who)
        assert res.status_code == 200
        assert set(res.json()) == {
            "as_of",
            "tasks",
            "agents",
            "pools",
            "instances",
            "accounts",
            "decisions",
            "knowledge",
        }


def test_overview_over_http_reports_measured_zeros(client: TestClient) -> None:
    res = client.get("/api/overview", headers=ADMIN)
    assert res.status_code == 200
    body = res.json()
    assert body["tasks"]["succeeded"] == 0
    assert body["accounts"]["quota_measured"] is False


async def test_no_secret_reference_in_the_payload(db: Database) -> None:
    await add_account(db, "a1", "anthropic", "available")
    payload = (await overview.overview(db)).model_dump_json()
    assert "anthropic" in payload  # the provider name is shown…
    assert SECRET_REF not in payload  # …the secret reference never is
    assert "secret" not in payload.lower()
