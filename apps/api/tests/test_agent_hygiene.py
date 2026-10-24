"""Agent hygiene: aggregates, spike rule, bounded archive (agent-hygiene.spec.md)."""

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from api_support import FakeClock
from argus_api.agents import hygiene as hygiene_mod
from argus_api.agents import service as agents
from argus_api.config import Settings
from argus_api.db import Database, ts
from argus_api.errors import ValidationFailedError
from argus_api.main import app
from argus_api.overview import overview
from argus_core.agents import AgentState
from fastapi.testclient import TestClient

ADMIN_TOKEN = "hygiene-admin-token-" + "h" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


async def add_pool(db: Database, name: str) -> None:
    async with db.write() as tx:
        stamp = ts(tx.now)
        await tx.conn.execute(
            "INSERT INTO pools (name, target, runtime, image, capabilities,"
            " desired_replicas, max_replicas, config, status, created_at, updated_at)"
            " VALUES (?, 'docker', 'plain', 'img', '[]', 1, 5, '{}', 'ok', ?, ?)",
            (name, stamp, stamp),
        )


async def add_instance(db: Database, iid: str, pool: str) -> None:
    async with db.write() as tx:
        stamp = ts(tx.now)
        await tx.conn.execute(
            "INSERT INTO instances (id, pool, provider_ref, state, created_at)"
            " VALUES (?, ?, 'ref', 'running', ?)",
            (iid, pool, stamp),
        )


async def add_agent(
    db: Database,
    agent_id: str,
    *,
    state: str = "lost",
    instance: str | None = None,
    heartbeat: datetime | None = None,
    ended: datetime | None = None,
    archived: datetime | None = None,
) -> None:
    async with db.write() as tx:
        hb = ts(heartbeat or tx.now)
        end = ts(ended or tx.now) if state in ("lost", "gone") else None
        await tx.conn.execute(
            "INSERT INTO agents (id, name, role, instance_id, capabilities, state,"
            " last_heartbeat_at, created_at, ended_at, archived_at)"
            " VALUES (?, ?, 'worker', ?, '[]', ?, ?, ?, ?, ?)",
            (
                agent_id,
                agent_id,
                instance,
                state,
                hb,
                ts(tx.now),
                end,
                ts(archived) if archived else None,
            ),
        )


async def add_task(db: Database, task_id: str, state: str, agent: str | None) -> None:
    async with db.write() as tx:
        stamp = ts(tx.now)
        await tx.conn.execute(
            "INSERT INTO tasks (id, kind, title, spec, acceptance, input,"
            " required_capabilities, priority, state, requires_approval, review,"
            " assignee_agent_id, lease_expires_at, lease_ttl_s, max_attempts,"
            " created_by, created_at, updated_at, project)"
            " VALUES (?, 'generic', 't', '', '[]', '{}', '[]', 0, ?, 0, 'none',"
            " ?, ?, 60, 3, 'test', ?, ?, 'global')",
            (task_id, state, agent, stamp if agent else None, stamp, stamp),
        )


def settings(**kw) -> Settings:
    return Settings(data_dir=Path("."), **kw)


# -- spike rule (pure) -------------------------------------------------------------------


def test_lost_spike_triggers_at_the_boundary() -> None:
    now = datetime(2026, 1, 10, tzinfo=UTC)
    window = timedelta(hours=24)
    # Five in the current window, two in the previous → 5 > 2*2 and >= min_count.
    ended = [
        now - timedelta(hours=1),
        now - timedelta(hours=2),
        now - timedelta(hours=3),
        now - timedelta(hours=4),
        now - timedelta(hours=5),
        now - window - timedelta(hours=1),
        now - window - timedelta(hours=2),
    ]
    spike = hygiene_mod.lost_spike(
        ended, now=now, window_hours=24, min_count=5, factor=2.0
    )
    assert (spike.current, spike.previous, spike.triggered) == (5, 2, True)


def test_lost_spike_does_not_trigger_below_min_count() -> None:
    now = datetime(2026, 1, 10, tzinfo=UTC)
    ended = [now - timedelta(hours=1)] * 4  # current=4 < min_count=5
    spike = hygiene_mod.lost_spike(
        ended, now=now, window_hours=24, min_count=5, factor=2.0
    )
    assert spike.triggered is False
    assert spike.current == 4


def test_lost_spike_does_not_trigger_when_not_above_factor() -> None:
    now = datetime(2026, 1, 10, tzinfo=UTC)
    window = timedelta(hours=24)
    # current=6, previous=3 → 6 == 2*3, not strictly greater.
    ended = [now - timedelta(hours=i) for i in range(1, 7)] + [
        now - window - timedelta(hours=i) for i in range(1, 4)
    ]
    spike = hygiene_mod.lost_spike(
        ended, now=now, window_hours=24, min_count=5, factor=2.0
    )
    assert (spike.current, spike.previous, spike.triggered) == (6, 3, False)


def test_lost_spike_excludes_the_previous_window_boundary() -> None:
    now = datetime(2026, 1, 10, tzinfo=UTC)
    window = timedelta(hours=24)
    # Exactly at now - window belongs to previous (half-open current).
    ended = [now - window]
    spike = hygiene_mod.lost_spike(
        ended, now=now, window_hours=24, min_count=1, factor=2.0
    )
    assert (spike.current, spike.previous) == (0, 1)


# -- aggregates --------------------------------------------------------------------------


async def test_groups_by_pool_state_day_and_unknown(
    db: Database, clock: FakeClock
) -> None:
    await add_pool(db, "alpha")
    await add_pool(db, "beta")
    await add_instance(db, "i1", "alpha")
    await add_instance(db, "i2", "beta")
    day = clock.now
    await add_agent(db, "a1", state="lost", instance="i1", ended=day, heartbeat=day)
    await add_agent(db, "a2", state="lost", instance="i1", ended=day, heartbeat=day)
    await add_agent(db, "a3", state="gone", instance="i2", ended=day, heartbeat=day)
    await add_agent(db, "orphan", state="lost", ended=day, heartbeat=day)  # no instance

    async with db.read() as conn:
        h = await hygiene_mod.agent_hygiene(conn, now=clock.now, settings=settings())
    by_key = {(g.pool, g.state.value, g.day.isoformat()): g.count for g in h.groups}
    assert by_key[("alpha", "lost", day.date().isoformat())] == 2
    assert by_key[("beta", "gone", day.date().isoformat())] == 1
    assert by_key[("unknown", "lost", day.date().isoformat())] == 1
    assert h.totals.lost == 3
    assert h.totals.gone == 1


async def test_age_buckets_at_day_boundaries(db: Database, clock: FakeClock) -> None:
    now = clock.now
    cases = [
        ("half", 0.5, "under_1d"),
        ("one", 1.0, "1d_to_7d"),
        ("almost7", 6.9, "1d_to_7d"),
        ("seven", 7.0, "7d_to_30d"),
        ("thirty", 30.0, "30d_and_over"),
    ]
    for name, days, _ in cases:
        hb = now - timedelta(days=days)
        await add_agent(db, name, state="lost", heartbeat=hb, ended=hb)

    async with db.read() as conn:
        h = await hygiene_mod.agent_hygiene(conn, now=now, settings=settings())
    buckets = h.age_buckets["lost"]
    for name, days, expected in cases:
        assert buckets[expected] >= 1, (name, days, buckets)
    assert buckets == {
        "under_1d": 1,
        "1d_to_7d": 2,
        "7d_to_30d": 1,
        "30d_and_over": 1,
    }


async def test_archived_excluded_from_groups_and_counted_in_totals(
    db: Database, clock: FakeClock
) -> None:
    await add_agent(db, "live-lost", state="lost")
    await add_agent(db, "old", state="lost", archived=clock.now)
    o = await overview(db)
    assert o.agents.by_state["lost"] == 1
    async with db.read() as conn:
        h = await hygiene_mod.agent_hygiene(conn, now=clock.now, settings=settings())
    assert h.totals.lost == 1
    assert h.totals.archived == 1
    assert len(h.groups) == 1
    assert sum(h.age_buckets["lost"].values()) == 1


async def test_lost_holding_claims_and_claim_holders(
    db: Database, clock: FakeClock
) -> None:
    await add_agent(db, "lost1", state="lost")
    await add_agent(db, "active1", state="active")
    # Inconsistent row: a lost agent still assigned to a claimed task.
    await add_task(db, "t-held", "claimed", "lost1")
    await add_task(db, "t-ok", "claimed", "active1")

    async with db.read() as conn:
        h = await hygiene_mod.agent_hygiene(conn, now=clock.now, settings=settings())
    assert h.totals.lost_holding_claims == 1
    assert h.claim_holders == [
        hygiene_mod.ClaimHoldingAgent(agent_id="lost1", task_id="t-held")
    ]
    o = await overview(db)
    assert o.agents.lost_holding_claims == 1


# -- archive -----------------------------------------------------------------------------


async def test_archive_refuses_active_state(db: Database) -> None:
    await add_agent(db, "a1", state="lost")
    async with db.write() as tx:
        with pytest.raises(ValidationFailedError) as err:
            await agents.archive_matching(
                tx,
                "admin:test",
                states=[AgentState.ACTIVE],
                older_than_days=0,
                limit=10,
            )
    assert err.value.code == "state_not_archivable"


async def test_archive_respects_limit_oldest_first(
    db: Database, clock: FakeClock
) -> None:
    for i in range(5):
        ended = clock.now - timedelta(days=10 - i)
        await add_agent(db, f"a{i}", state="lost", ended=ended, heartbeat=ended)
    async with db.write() as tx:
        n = await agents.archive_matching(
            tx,
            "admin:test",
            states=[AgentState.LOST],
            older_than_days=0,
            limit=2,
        )
    assert n == 2
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT id FROM agents WHERE archived_at IS NOT NULL ORDER BY id"
        ) as cur,
    ):
        archived = [r["id"] for r in await cur.fetchall()]
    assert archived == ["a0", "a1"]  # oldest ended_at first


async def test_archive_dry_run_writes_nothing(db: Database, clock: FakeClock) -> None:
    await add_agent(db, "a1", state="gone", ended=clock.now - timedelta(days=5))
    async with db.write() as tx:
        n = await agents.archive_matching(
            tx,
            "admin:test",
            states=[AgentState.GONE],
            older_than_days=0,
            limit=10,
            dry_run=True,
        )
    assert n == 1
    async with (
        db.read() as conn,
        conn.execute("SELECT archived_at FROM agents WHERE id = 'a1'") as cur,
    ):
        assert (await cur.fetchone())["archived_at"] is None


async def test_archive_writes_no_event_and_leaves_tasks(
    db: Database, clock: FakeClock
) -> None:
    await add_agent(db, "a1", state="lost", ended=clock.now - timedelta(days=40))
    await add_task(db, "t1", "succeeded", None)
    async with db.read() as conn:
        async with conn.execute("SELECT count(*) AS n FROM events") as cur:
            before_events = (await cur.fetchone())["n"]
        async with conn.execute("SELECT * FROM tasks WHERE id = 't1'") as cur:
            before_task = dict(await cur.fetchone())

    async with db.write() as tx:
        n = await agents.archive_matching(
            tx,
            "admin:test",
            states=[AgentState.LOST],
            older_than_days=30,
            limit=10,
        )
    assert n == 1

    async with db.read() as conn:
        async with conn.execute("SELECT count(*) AS n FROM events") as cur:
            assert (await cur.fetchone())["n"] == before_events
        async with conn.execute("SELECT * FROM tasks WHERE id = 't1'") as cur:
            assert dict(await cur.fetchone()) == before_task
        async with conn.execute(
            "SELECT archived_at FROM agents WHERE id = 'a1'"
        ) as cur:
            assert (await cur.fetchone())["archived_at"] is not None

    async with db.write() as tx:
        assert (
            await agents.archive_matching(
                tx,
                "admin:test",
                states=[AgentState.LOST],
                older_than_days=30,
                limit=10,
            )
            == 0
        )


# -- HTTP --------------------------------------------------------------------------------


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


def test_hygiene_scopes(client: TestClient) -> None:
    assert client.get("/api/agents/hygiene").status_code == 401
    assert (
        client.get(
            "/api/agents/hygiene", headers=agent_headers(client, "worker")
        ).status_code
        == 403
    )
    for who in (ADMIN, agent_headers(client, "orchestrator")):
        assert client.get("/api/agents/hygiene", headers=who).status_code == 200


def test_archive_scopes_and_disabled(client: TestClient) -> None:
    body = {"states": ["lost"], "older_than_days": 0}
    assert client.post("/api/agents/archive", json=body).status_code == 401
    assert (
        client.post(
            "/api/agents/archive",
            json=body,
            headers=agent_headers(client, "orchestrator"),
        ).status_code
        == 403
    )
    # No older_than_days and no env default → 409 agent_archive_disabled.
    res = client.post("/api/agents/archive", json={"states": ["lost"]}, headers=ADMIN)
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "agent_archive_disabled"


def test_archive_over_http_refuses_active(client: TestClient) -> None:
    res = client.post(
        "/api/agents/archive",
        json={"states": ["active"], "older_than_days": 0},
        headers=ADMIN,
    )
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "state_not_archivable"


def test_overview_exposes_hygiene_fields(client: TestClient) -> None:
    res = client.get("/api/overview", headers=ADMIN)
    assert res.status_code == 200
    agents_body = res.json()["agents"]
    assert "lost_holding_claims" in agents_body
    assert agents_body["lost_holding_claims"] == 0
    spike = agents_body["lost_spike"]
    assert set(spike) == {
        "window_hours",
        "current",
        "previous",
        "min_count",
        "factor",
        "triggered",
    }
    assert agents_body["hygiene_by_pool"] == {}


async def test_list_agents_archived_filter(db: Database) -> None:
    await add_agent(db, "live", state="lost")
    await add_agent(
        db, "parked", state="lost", archived=datetime(2026, 1, 1, tzinfo=UTC)
    )
    async with db.read() as conn:
        default = await agents.list_agents(conn)
        archived = await agents.list_agents(conn, archived=True)
    assert {a.id for a in default} == {"live"}
    assert {a.id for a in archived} == {"parked"}


def test_mcp_get_agent_hygiene_matches_rest(client: TestClient) -> None:
    from test_mcp import rpc, tool

    def register(role: str = "worker") -> str:
        boot = client.post(
            "/api/agents/bootstrap-tokens", json={"role": role}, headers=ADMIN
        ).json()
        return client.post(
            "/api/agents/register", json={"bootstrap_token": boot["token"]}
        ).json()["token"]

    orch = register("orchestrator")
    tools = {t["name"] for t in rpc(client, orch, "tools/list")["tools"]}
    assert "get_agent_hygiene" in tools
    assert "archive_agents" not in tools  # admin only
    admin_tools = {t["name"] for t in rpc(client, ADMIN_TOKEN, "tools/list")["tools"]}
    assert "archive_agents" in admin_tools

    rest = client.get("/api/agents/hygiene", headers=ADMIN).json()
    mcp = tool(client, orch, "get_agent_hygiene")
    assert mcp["totals"] == rest["totals"]
