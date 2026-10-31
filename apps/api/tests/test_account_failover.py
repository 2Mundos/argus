"""Automatic account failover on quota exhaustion (account selection 3.6)."""

import asyncio
import json
import logging
from collections.abc import Generator
from datetime import timedelta
from pathlib import Path

import pytest
from api_support import FakeClock, insert_agent
from argus_api.accounts import routes as account_routes
from argus_api.accounts import service as accounts
from argus_api.accounts.service import NoAccountError
from argus_api.db import Database
from argus_api.errors import ConflictError, LeaseConflictError
from argus_api.main import app
from argus_api.reaper import reap
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import service as tasks
from argus_core.accounts import AccountCreate, ExhaustedReport
from argus_core.tasks import TaskCreate
from argus_core.tasks import TaskState as S
from fastapi.testclient import TestClient

A, B = "agent-a", "agent-b"
QUOTA = "Rate limit [five_hour] (resets 2026-01-01T05:00:00Z)"
GENERIC = "The agent run failed. Check the logs for details."


@pytest.fixture
def store() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture(autouse=True)
async def agents(db: Database) -> None:
    await insert_agent(db, A)
    await insert_agent(db, B)


async def add(db: Database, store: MemorySecretStore, name: str, **kw) -> str:
    await store.put(name, {"api_key": f"key-{name}"})
    async with db.write() as tx:
        account = await accounts.create_account(
            tx,
            name,
            AccountCreate(
                name=name,
                provider=kw.pop("provider", "anthropic"),
                secret_ref=f"sops:{name}",
                **kw,
            ),
            f"sops:{name}",
            "admin:test",
            store,
        )
    return account.id


async def lease(db: Database, store, agent=A, *, task=None):
    """Lease for `agent`. Pass `task` to reuse one claim; otherwise create one."""
    if task is None:
        task = await new_task(db, agent)
    async with db.write() as tx:
        return await accounts.lease(
            tx,
            agent,
            ["anthropic", "openai", "cursor"],
            store,
            task_id=task.id,
            attempt=task.attempt,
        )


async def new_task(db: Database, agent=A, **kw):
    """Create a task and claim it as `agent`."""
    async with db.write() as tx:
        await tasks.create_task(
            tx, TaskCreate(project="global", title="t", **kw), "admin:test"
        )
        task = await tasks.claim(tx, agent, [])
    assert task is not None
    return task


async def fail(
    db: Database, store, task, error=QUOTA, agent=A, reason="provider_failure"
):
    async with db.write() as tx:
        return await accounts.fail_task(
            tx, agent, task.id, task.attempt, error, reason, store, timedelta(hours=1)
        )


async def get(db: Database, task_id: str):
    async with db.read() as conn:
        return await tasks.get_task(conn, task_id)


async def events(db: Database, kind: str) -> list[dict]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT subject_id, actor, data FROM events WHERE kind = ? ORDER BY id",
            (kind,),
        ) as cur,
    ):
        return [
            {"subject": r[0], "actor": r[1], **json.loads(r[2])}
            for r in await cur.fetchall()
        ]


# -- ranking ----------------------------------------------------------------------------


async def test_exhausted_top_account_is_skipped_and_costs_no_attempt(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    a = await add(db, store, "acct-a", priority=100)
    await add(db, store, "acct-b", priority=90)
    held = await lease(db, store)
    task = await new_task(db, A)
    async with db.write() as tx:
        await accounts.exhausted(
            tx,
            A,
            a,
            ExhaustedReport(
                until=clock.now + timedelta(hours=4),
                runtime_providers=["anthropic"],
                task_id=task.id,
                attempt=task.attempt,
            ),
            store,
        )
    assert (await lease(db, store, B)).name == "acct-b"  # A is out of rotation
    task = await new_task(db, B)
    assert held.name == "acct-a"
    assert (task.attempt, task.failures) == (1, 0)


async def test_stale_exhausted_mark_past_reset_does_not_demote(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    a = await add(db, store, "acct-a", priority=100)
    await add(db, store, "acct-b", priority=90)
    await lease(db, store)
    task = await new_task(db, A)
    async with db.write() as tx:
        await accounts.exhausted(
            tx,
            A,
            a,
            ExhaustedReport(
                until=clock.now + timedelta(hours=1),
                runtime_providers=["anthropic"],
                task_id=task.id,
                attempt=task.attempt,
            ),
            store,
        )
    clock.advance(3601)  # reaper has not run: the mark is stale
    assert (await lease(db, store, B)).name == "acct-a"  # recovered on lease
    async with db.read() as conn:
        acct = await accounts.get_account(conn, a, clock.now)
    assert (acct.state, acct.exhausted_until) == ("available", None)


# -- the server-authoritative path -------------------------------------------------------


async def test_quota_fail_fails_over_without_consuming_an_attempt(
    db: Database,
    store: MemorySecretStore,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    a = await add(db, store, "acct-a", priority=100)
    await add(db, store, "acct-b", priority=90)
    assert (await lease(db, store)).name == "acct-a"
    task = await new_task(db)
    assert (await get(db, task.id)).serving_account.name == "acct-a"  # type: ignore[union-attr]

    with caplog.at_level(logging.INFO, logger="argus_api.accounts.service"):
        after = await fail(db, store, task)

    assert (after.state, after.attempt, after.failures) == (S.READY, 1, 0)
    assert after.error is None
    assert (after.failover.from_account, after.failover.to_account) == (  # type: ignore[union-attr]
        "acct-a",
        "acct-b",
    )
    assert "five_hour" in after.failover.reason  # type: ignore[union-attr]
    async with db.read() as conn:
        acct = await accounts.get_account(conn, a, clock.now)
    assert acct.state == "exhausted"
    assert acct.exhausted_until.isoformat() == "2026-01-01T05:00:00+00:00"  # type: ignore[union-attr]

    # The agent now serves from B; the next claim is a new attempt, still no failure.
    async with db.write() as tx:
        again = await tasks.claim(tx, A, [])
    assert again is not None and (again.attempt, again.failures) == (2, 0)
    assert again.serving_account is None  # nothing is leased before the new claim does
    assert (await lease(db, store, task=again)).name == "acct-b"
    assert (await get(db, task.id)).serving_account.name == "acct-b"  # type: ignore[union-attr]

    [event] = await events(db, "account.failover")
    assert event["actor"] == f"agent:{A}" and event["subject"] == task.id
    assert (event["from"], event["to"], event["attempt"]) == ("acct-a", "acct-b", 1)
    [record] = [
        r for r in caplog.records if r.getMessage().startswith("account_failover")
    ]
    logged = json.loads(record.getMessage().split(" ", 1)[1])
    assert (
        logged.items()
        >= {
            "event": "account_failover",
            "from": "acct-a",
            "to": "acct-b",
            "reason": QUOTA,
            "task_id": task.id,
            "attempt": 1,
            "outcome": "switched",
            "apply": "pending",
            "reason_code": "quota_exhausted",
        }.items()
    )
    assert "key-acct" not in json.dumps(event) + caplog.text


async def test_quota_never_reaches_failed_however_often_it_repeats(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    await add(db, store, "acct-a", priority=100)
    await add(db, store, "acct-b", priority=90)
    await add(db, store, "acct-c", priority=80)
    await lease(db, store)
    task = await new_task(db, max_attempts=2)
    for _ in range(2):
        task = await fail(db, store, task)
        assert (task.state, task.failures) == (S.READY, 0)
        async with db.write() as tx:
            task = await tasks.claim(tx, A, [])  # type: ignore[assignment]
        await lease(db, store, task=task)
    assert (await get(db, task.id)).state is S.CLAIMED


async def test_generic_provider_failure_still_counts(
    db: Database, store: MemorySecretStore
) -> None:
    a = await add(db, store, "acct-a")
    await lease(db, store)
    task = await new_task(db)
    after = await fail(db, store, task, GENERIC)
    assert (after.state, after.failures) == (S.READY, 1)
    async with (
        db.read() as conn,
        conn.execute("SELECT state FROM accounts WHERE id = ?", (a,)) as cur,
    ):
        assert (await cur.fetchone())[0] == "available"  # type: ignore[index]
    assert await events(db, "account.failover") == []


async def test_claude_session_limit_line_resets_at_the_next_utc_clock_time(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    a = await add(db, store, "acct-a", priority=100)
    await add(db, store, "acct-b", priority=90)
    await lease(db, store)
    clock.advance(22 * 3600 + 6 * 60)  # 22:06Z, the incident's hour
    task = await new_task(db)
    line = "You\u2019ve hit your session limit \u00b7 resets 2am (UTC)"
    after = await fail(db, store, task, line)
    assert (after.state, after.failures) == (S.READY, 0)
    async with db.read() as conn:
        acct = await accounts.get_account(conn, a, clock.now)
    assert acct.exhausted_until.isoformat() == "2026-01-02T02:00:00+00:00"  # type: ignore[union-attr]


async def test_model_text_about_limits_still_counts_as_a_failure(
    db: Database, store: MemorySecretStore
) -> None:
    await add(db, store, "acct-a")
    await lease(db, store)
    task = await new_task(db)
    text = "I think you've hit your session limit \u00b7 resets 2am (UTC)"
    assert (await fail(db, store, task, text)).failures == 1


async def test_other_providers_quota_text_does_not_exhaust_the_engine_account(
    db: Database, store: MemorySecretStore
) -> None:
    await add(db, store, "acct-a")  # anthropic
    await lease(db, store)
    task = await new_task(db)
    after = await fail(db, store, task, "usage_limit_reached")  # an openai notice
    assert after.failures == 1


async def test_repeated_report_and_stale_attempt_change_nothing(
    db: Database, store: MemorySecretStore
) -> None:
    await add(db, store, "acct-a", priority=100)
    await add(db, store, "acct-b", priority=90)
    await lease(db, store)
    task = await new_task(db)

    stale = task.model_copy(update={"attempt": task.attempt + 1})
    with pytest.raises(LeaseConflictError) as err:
        await fail(db, store, stale)
    assert err.value.code == "stale_attempt"
    assert await events(db, "account.failover") == []
    assert (await events(db, "account.exhausted")) == []

    await fail(db, store, task)
    with pytest.raises(LeaseConflictError):  # the repeat: task is no longer claimed
        await fail(db, store, task)
    assert len(await events(db, "account.failover")) == 1
    assert len(await events(db, "account.exhausted")) == 1
    assert (await get(db, task.id)).failures == 0


async def test_exhausted_report_is_fenced_and_repeat_is_refused(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    a = await add(db, store, "acct-a", priority=100)
    await add(db, store, "acct-b", priority=90)
    await lease(db, store)
    task = await new_task(db)
    report = ExhaustedReport(
        task_id=task.id,
        attempt=task.attempt + 1,
        reason=QUOTA,
        runtime_providers=["anthropic"],
    )
    async with db.write() as tx:
        with pytest.raises(LeaseConflictError):
            await accounts.exhausted(tx, A, a, report, store)
    async with db.read() as conn:
        assert (await accounts.get_account(conn, a, clock.now)).state == "available"

    report = ExhaustedReport(
        task_id=task.id,
        attempt=task.attempt,
        reason=QUOTA,
        runtime_providers=["anthropic"],
    )
    async with db.write() as tx:
        result = await accounts.exhausted(tx, A, a, report, store)
    assert result.lease is None and result.next_account_name == "acct-b"
    async with db.write() as tx:
        with pytest.raises(ConflictError) as repeat:
            await accounts.exhausted(tx, A, a, report, store)
    assert repeat.value.code == "not_leased"
    [event] = await events(db, "account.failover")
    assert (event["from"], event["to"]) == ("acct-a", "acct-b")
    got = await get(db, task.id)
    assert (got.state, got.attempt, got.failures) == (S.CLAIMED, 1, 0)


# -- awaiting quota ----------------------------------------------------------------------


async def test_all_exhausted_awaits_quota_then_returns_to_ready(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    b = await add(db, store, "acct-b", priority=90)
    soon = clock.now + timedelta(hours=2)
    task_b = await new_task(db, B)
    await lease(db, store, B, task=task_b)
    async with db.write() as tx:
        await accounts.exhausted(
            tx,
            B,
            b,
            ExhaustedReport(
                until=soon,
                runtime_providers=["anthropic"],
                task_id=task_b.id,
                attempt=task_b.attempt,
            ),
            store,
        )
    # Adding acct-a would unblock task_b into ready; cancel the fence so the
    # subsequent claim is a fresh attempt-1 task, not a reclaim of task_b.
    async with db.write() as tx:
        await tasks.cancel(tx, task_b.id, "admin:test")
    await add(db, store, "acct-a", priority=100)
    task = await new_task(db)
    await lease(db, store, task=task)

    after = await fail(db, store, task)  # A's reset is 05:00 (QUOTA)
    assert (after.state, after.block_reason) == (S.BLOCKED, "awaiting_quota")
    assert (after.attempt, after.failures) == (1, 0)
    assert after.refill_at == soon  # min(exhausted_until): B resets first
    assert after.failover.to_account is None  # type: ignore[union-attr]

    clock.advance(2 * 3600 + 1)
    async with db.write() as tx:
        result = await reap(tx)
    assert result.recovered_accounts == [b]
    ready = await get(db, task.id)
    assert (ready.state, ready.failures, ready.refill_at) == (S.READY, 0, None)


async def test_exhausted_endpoint_path_also_awaits_quota_with_refill(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    a = await add(db, store, "acct-a")
    await lease(db, store)
    task = await new_task(db)
    reset = clock.now + timedelta(minutes=30)
    async with db.write() as tx:
        result = await accounts.exhausted(
            tx,
            A,
            a,
            ExhaustedReport(
                until=reset,
                reason=QUOTA,
                task_id=task.id,
                attempt=task.attempt,
                runtime_providers=["anthropic"],
            ),
            store,
        )
    assert result.lease is None and result.task_blocked
    got = await get(db, task.id)
    assert (got.block_reason, got.refill_at, got.attempt) == (
        "awaiting_quota",
        reset,
        1,
    )
    assert got.failover.to_account is None  # type: ignore[union-attr]


async def test_legacy_no_account_rows_are_still_read_and_released(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    a = await add(db, store, "acct-a")
    await lease(db, store)
    task = await new_task(db)
    async with db.write() as tx:
        await accounts.exhausted(
            tx,
            A,
            a,
            ExhaustedReport(
                until=clock.now + timedelta(minutes=5),
                runtime_providers=["anthropic"],
                task_id=task.id,
                attempt=task.attempt,
            ),
            store,
        )
        await tx.conn.execute(
            "UPDATE tasks SET block_reason = 'no_account' WHERE id = ?", (task.id,)
        )
    old = await get(db, task.id)
    assert old.refill_at == clock.now + timedelta(minutes=5)
    clock.advance(301)
    async with db.write() as tx:
        await reap(tx)
    assert (await get(db, task.id)).state is S.READY


async def test_unclaimed_ready_task_is_parked_when_no_account_is_leasable(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    a = await add(db, store, "acct-a")
    await lease(db, store, B)
    reset = clock.now + timedelta(hours=1)
    task_b = await new_task(db, B)
    async with db.write() as tx:
        await accounts.exhausted(
            tx,
            B,
            a,
            ExhaustedReport(
                until=reset,
                runtime_providers=["anthropic"],
                task_id=task_b.id,
                attempt=task_b.attempt,
            ),
            store,
        )
    async with db.write() as tx:
        await tasks.create_task(
            tx, TaskCreate(project="global", title="t"), "admin:test"
        )
        other = await tasks.create_task(
            tx,
            TaskCreate(project="global", title="u", required_capabilities=["gpu"]),
            "admin:test",
        )
    async with db.write() as tx:
        ids = await accounts.mark_awaiting_quota(
            tx, A, [], ["anthropic", "openai", "cursor"]
        )
    assert len(ids) == 1 and ids[0] != other.id  # only what the agent could claim
    parked = await get(db, ids[0])
    assert (parked.state, parked.block_reason, parked.attempt, parked.failures) == (
        S.BLOCKED,
        "awaiting_quota",
        0,
        0,
    )
    assert parked.refill_at == reset
    clock.advance(3601)
    async with db.write() as tx:
        await reap(tx)
    assert (await get(db, ids[0])).state is S.READY


async def test_parking_is_refused_while_an_account_is_usable(
    db: Database, store: MemorySecretStore
) -> None:
    await add(db, store, "acct-a")
    async with db.write() as tx:
        await tasks.create_task(
            tx, TaskCreate(project="global", title="t"), "admin:test"
        )
    async with db.write() as tx:
        with pytest.raises(ConflictError) as err:
            await accounts.mark_awaiting_quota(
                tx, A, [], ["anthropic", "openai", "cursor"]
            )
    assert err.value.code == "account_available"


async def test_new_account_releases_waiting_tasks(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    await add(db, store, "acct-a")
    await lease(db, store)
    task = await new_task(db)
    await fail(db, store, task)
    assert (await get(db, task.id)).state is S.BLOCKED
    await add(db, store, "acct-new")
    assert (await get(db, task.id)).state is S.READY


# -- exclusivity -------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 2])
async def test_concurrent_leases_never_exceed_max_leases(
    db: Database, store: MemorySecretStore, limit: int
) -> None:
    solo = await add(db, store, "solo", max_leases=limit)
    ids = [f"w{i}" for i in range(8)]
    for i in ids:
        await insert_agent(db, i)
    results = await asyncio.gather(
        *(lease(db, store, i) for i in ids), return_exceptions=True
    )
    won = [r for r in results if not isinstance(r, BaseException)]
    assert len(won) == limit
    assert all(isinstance(r, NoAccountError) for r in results if r not in won)
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT count(*) FROM account_leases WHERE account_id = ? AND released_at IS NULL",
            (solo,),
        ) as cur,
    ):
        assert (await cur.fetchone())[0] == limit  # type: ignore[index]


async def test_two_agents_never_hold_the_same_task(
    db: Database, store: MemorySecretStore
) -> None:
    async with db.write() as tx:
        await tasks.create_task(
            tx, TaskCreate(project="global", title="t"), "admin:test"
        )

    async def claim(agent: str):
        async with db.write() as tx:
            return await tasks.claim(tx, agent, [])

    got = await asyncio.gather(claim(A), claim(B))
    assert sum(t is not None for t in got) == 1


# -- over HTTP ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", "test-admin-token-" + "x" * 32)
    with TestClient(
        app, headers={"Authorization": "Bearer test-admin-token-" + "x" * 32}
    ) as c:
        app.state.secrets = MemorySecretStore()
        yield c


def register(client: TestClient) -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
    res = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    return {"Authorization": f"Bearer {res.json()['token']}"}


def test_failover_over_http(client: TestClient) -> None:
    worker = register(client)
    for name, priority in (("acct-a", 100), ("acct-b", 90)):
        res = client.post(
            "/api/accounts",
            json={
                "name": name,
                "provider": "anthropic",
                "priority": priority,
                "credential": {"api_key": f"sk-ant-api03-{name}"},
            },
        )
        assert res.status_code == 201
    client.post("/api/tasks", json={"project": "global", "title": "x"})
    task = client.post("/api/tasks/claim", json={}, headers=worker).json()
    assert client.post(
        "/api/accounts/lease",
        json={
            "runtime_providers": ["anthropic"],
            "task_id": task["id"],
            "attempt": task["attempt"],
        },
        headers=worker,
    ).json()["name"] == ("acct-a")

    res = client.post(
        f"/api/tasks/{task['id']}/fail",
        json={"attempt": task["attempt"], "error": QUOTA, "reason": "provider_failure"},
        headers=worker,
    )
    assert res.status_code == 200
    body = res.json()
    assert (body["state"], body["failures"], body["attempt"]) == ("ready", 0, 1)
    assert body["failover"]["from_account"] == "acct-a"
    assert body["failover"]["to_account"] == "acct-b"
    assert "sk-ant" not in res.text
    # The repeat is refused without effect.
    again = client.post(
        f"/api/tasks/{task['id']}/fail",
        json={"attempt": task["attempt"], "error": QUOTA, "reason": "provider_failure"},
        headers=worker,
    )
    assert again.status_code == 409


def test_awaiting_quota_endpoint_over_http(client: TestClient) -> None:
    worker = register(client)
    account = client.post(
        "/api/accounts",
        json={
            "name": "solo",
            "provider": "anthropic",
            "credential": {"api_key": "sk-ant-api03-solo"},
        },
    ).json()
    task = client.post("/api/tasks", json={"project": "global", "title": "x"}).json()
    parked = client.post(
        "/api/accounts/awaiting-quota",
        json={"runtime_providers": ["anthropic"]},
        headers=worker,
    )
    assert parked.status_code == 409 and parked.json()["detail"]["code"] == (
        "account_available"
    )
    task = client.post("/api/tasks/claim", json={}, headers=worker).json()
    client.post(
        "/api/accounts/lease",
        json={
            "runtime_providers": ["anthropic"],
            "task_id": task["id"],
            "attempt": task["attempt"],
        },
        headers=worker,
    )
    waiting = client.post(
        "/api/tasks", json={"project": "global", "title": "waiting"}
    ).json()
    reset = "2099-01-01T00:00:00Z"
    client.post(
        f"/api/accounts/{account['id']}/exhausted",
        json={
            "until": reset,
            "runtime_providers": ["anthropic"],
            "task_id": task["id"],
            "attempt": task["attempt"],
        },
        headers=worker,
    )
    parked = client.post(
        "/api/accounts/awaiting-quota",
        json={"runtime_providers": ["anthropic"]},
        headers=worker,
    )
    assert parked.status_code == 200
    assert parked.json()["task_ids"] == [waiting["id"]]
    assert parked.json()["refill_at"].startswith("2099-01-01T00:00:00")
    got = client.get(f"/api/tasks/{task['id']}").json()
    assert (got["state"], got["block_reason"]) == ("blocked", "awaiting_quota")
    assert got["refill_at"].startswith("2099-01-01T00:00:00")


def test_exhausted_returns_metadata_only_and_honors_runtime_provider_filter(
    client: TestClient,
) -> None:
    worker = register(client)
    first = client.post(
        "/api/accounts",
        json={
            "name": "first",
            "provider": "anthropic",
            "priority": 10,
            "credential": {"api_key": "sk-ant-api03-first"},
        },
    ).json()
    client.post(
        "/api/accounts",
        json={
            "name": "other-provider",
            "provider": "openai",
            "credential": {"api_key": "opaque-openai"},
        },
    )
    client.post(
        "/api/accounts",
        json={
            "name": "anthropic-fallback",
            "provider": "anthropic",
            "credential": {"api_key": "sk-ant-api03-next"},
        },
    )
    client.post("/api/tasks", json={"project": "global", "title": "quota"})
    task = client.post("/api/tasks/claim", json={}, headers=worker).json()
    leased = client.post(
        "/api/accounts/lease",
        json={
            "runtime_providers": ["anthropic"],
            "task_id": task["id"],
            "attempt": task["attempt"],
        },
        headers=worker,
    ).json()
    assert leased["account_id"] == first["id"]
    no_claim = client.post(
        f"/api/accounts/{first['id']}/exhausted",
        json={"runtime_providers": ["anthropic"]},
        headers=worker,
    )
    assert no_claim.status_code == 422 and "credential" not in no_claim.text
    undeclared = client.post(
        f"/api/accounts/{first['id']}/exhausted",
        json={
            "runtime_providers": ["openai"],
            "task_id": task["id"],
            "attempt": task["attempt"],
        },
        headers=worker,
    )
    assert undeclared.status_code == 409
    admin = {"Authorization": "Bearer test-admin-token-" + "x" * 32}
    assert (
        client.get(f"/api/accounts/{first['id']}", headers=admin).json()["state"]
        == "available"
    )
    result = client.post(
        f"/api/accounts/{first['id']}/exhausted",
        json={
            "runtime_providers": ["anthropic"],
            "task_id": task["id"],
            "attempt": task["attempt"],
            "reason": "limit",
        },
        headers=worker,
    )
    assert result.status_code == 200
    body = result.json()
    assert body["next_provider"] == "anthropic"
    assert body["next_account_name"] == "anthropic-fallback"
    assert "credential" not in body and body["lease"] is None
    assert "sk-ant-api03-next" not in result.text and "opaque-openai" not in result.text


def test_only_the_internal_orchestrator_leases_without_a_task(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """internal-orchestrator.spec.md 3.1: it leases like any agent, with no task."""
    agent = register(client)
    client.post(
        "/api/accounts",
        json={
            "name": "solo",
            "provider": "anthropic",
            "credential": {"api_key": "sk-ant-api03-solo"},
        },
    )
    bare = {"runtime_providers": ["anthropic"]}
    refused = client.post("/api/accounts/lease", json=bare, headers=agent)
    assert refused.status_code == 422, refused.text
    half = client.post(
        "/api/accounts/lease", json={**bare, "task_id": "t"}, headers=agent
    )
    assert half.status_code == 422

    async def system_pool(_conn: object, _agent_id: str | None) -> str:
        return "orchestrator"

    monkeypatch.setattr(account_routes, "agent_system_pool", system_pool)
    leased = client.post("/api/accounts/lease", json=bare, headers=agent)
    assert leased.status_code == 200, leased.text
    assert leased.json()["name"] == "solo"
    executor = client.post(
        "/api/accounts/lease", json={**bare, "role": "executor"}, headers=agent
    )
    assert executor.status_code == 422
