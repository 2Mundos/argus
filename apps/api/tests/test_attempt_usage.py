"""One usage outcome per attempt (account usage spec 3.10)."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from api_support import DONE, FakeClock, insert_agent
from argus_api.accounts import cost, pricing, usage
from argus_api.accounts import service as accounts
from argus_api.db import Database, ts
from argus_api.db.migrate import load_migrations
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import service as tasks
from argus_core.accounts import PLACEHOLDER_PREFIX, AccountCreate, UsageReport
from argus_core.tasks import TaskCreate, TaskResult
from pydantic import ValidationError

A = "agent-a"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def store() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture(autouse=True)
async def agent(db: Database) -> None:
    await insert_agent(db, A)


async def add_account(db: Database, store: MemorySecretStore, name: str = "acc") -> str:
    await store.put(name, {"api_key": "x"})
    async with db.write() as tx:
        account = await accounts.create_account(
            tx,
            name,
            AccountCreate(
                name=name,
                provider="openai",
                kind="api_key",
                secret_ref=f"sops:{name}",
            ),
            f"sops:{name}",
            "admin:test",
            store,
        )
    return account.id


async def claim_with_lease(
    db: Database, store: MemorySecretStore, account_id: str | None = None
):
    async with db.write() as tx:
        await tasks.create_task(
            tx, TaskCreate(project="global", title="attempt usage"), "admin:test"
        )
        task = await tasks.claim(tx, A, [])
    assert task is not None
    if account_id is not None:
        async with db.write() as tx:
            await accounts.lease(
                tx,
                A,
                ["openai"],
                store,
                "engine",
                task_id=task.id,
                attempt=task.attempt,
            )
    return task


async def usage_rows(db: Database, task_id: str) -> list[dict]:
    async with (
        db.read() as c,
        c.execute(
            "SELECT account_id, attempt, measurement, unmeasured_reason, run_key,"
            " input_tokens, role FROM account_usage WHERE task_id = ? ORDER BY id",
            (task_id,),
        ) as cur,
    ):
        return [dict(r) for r in await cur.fetchall()]


async def test_client_cannot_use_placeholder_run_key_prefix() -> None:
    with pytest.raises(ValidationError, match="reserved"):
        UsageReport(input_tokens=1, run_key=f"{PLACEHOLDER_PREFIX}x")


async def test_complete_without_report_writes_placeholder(
    db: Database, store: MemorySecretStore
) -> None:
    acc = await add_account(db, store)
    task = await claim_with_lease(db, store, acc)
    async with db.write() as tx:
        await tasks.complete(tx, A, task.id, task.attempt, DONE)
    rows = await usage_rows(db, task.id)
    assert len(rows) == 1
    r = rows[0]
    assert r["measurement"] == "not_reported"
    assert r["unmeasured_reason"] == "no_report"
    assert r["attempt"] == task.attempt
    assert r["account_id"] == acc
    assert r["role"] == "engine"
    assert r["run_key"].startswith(PLACEHOLDER_PREFIX)
    assert r["input_tokens"] is None


async def test_fail_writes_no_report_reason(
    db: Database, store: MemorySecretStore
) -> None:
    acc = await add_account(db, store)
    failed = await claim_with_lease(db, store, acc)
    async with db.write() as tx:
        await tasks.fail(tx, A, failed.id, failed.attempt, "boom")
    rows = await usage_rows(db, failed.id)
    assert [(r["attempt"], r["unmeasured_reason"]) for r in rows] == [
        (failed.attempt, "no_report")
    ]


async def test_release_writes_released_reason(
    db: Database, store: MemorySecretStore
) -> None:
    acc = await add_account(db, store)
    released = await claim_with_lease(db, store, acc)
    async with db.write() as tx:
        await tasks.release(
            tx, released.id, "admin:test", "recycle", attempt=released.attempt
        )
    rows = await usage_rows(db, released.id)
    assert [(r["attempt"], r["unmeasured_reason"]) for r in rows] == [
        (released.attempt, "released")
    ]


async def test_cancel_writes_cancelled_reason(
    db: Database, store: MemorySecretStore
) -> None:
    acc = await add_account(db, store)
    cancelled = await claim_with_lease(db, store, acc)
    async with db.write() as tx:
        await tasks.cancel(tx, cancelled.id, "admin:test")
    rows = await usage_rows(db, cancelled.id)
    assert [(r["attempt"], r["unmeasured_reason"]) for r in rows] == [
        (cancelled.attempt, "cancelled")
    ]


async def test_expire_maps_lost_timeout_stalled(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    acc = await add_account(db, store)
    task = await claim_with_lease(db, store, acc)
    clock.advance(10_000)
    async with db.write() as tx:
        assert await tasks.expire_leases(tx) == [task.id]
    assert (await usage_rows(db, task.id))[0]["unmeasured_reason"] == "lost"


async def test_expire_timeouts_writes_timeout_reason(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    acc = await add_account(db, store)
    async with db.write() as tx:
        await tasks.create_task(
            tx,
            TaskCreate(project="global", title="timeout usage", timeout_s=60),
            "admin:test",
        )
        task = await tasks.claim(tx, A, [])
        assert task is not None
        await accounts.lease(
            tx,
            A,
            ["openai"],
            store,
            "engine",
            task_id=task.id,
            attempt=task.attempt,
        )
    clock.advance(61)
    async with db.write() as tx:
        assert await tasks.expire_timeouts(tx) == [task.id]
    rows = await usage_rows(db, task.id)
    assert rows[0]["unmeasured_reason"] == "timeout"
    assert rows[0]["account_id"] == acc


async def test_expire_stalled_writes_stalled_reason(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    acc = await add_account(db, store)
    task = await claim_with_lease(db, store, acc)
    async with db.write() as tx:
        # Flag quiet so expire_stalled will consider it; last signal is claim time.
        await tx.conn.execute(
            "UPDATE tasks SET quiet_at = ? WHERE id = ?", (ts(tx.now), task.id)
        )
    clock.advance(100)
    async with db.write() as tx:
        assert await tasks.expire_stalled(tx, stall_after_s=60) == [task.id]
    assert (await usage_rows(db, task.id))[0]["unmeasured_reason"] == "stalled"


async def test_no_engine_lease_leaves_null_account(
    db: Database, store: MemorySecretStore
) -> None:
    task = await claim_with_lease(db, store, account_id=None)
    async with db.write() as tx:
        await tasks.complete(tx, A, task.id, task.attempt, DONE)
    r = (await usage_rows(db, task.id))[0]
    assert r["account_id"] is None and r["role"] is None
    assert r["measurement"] == "not_reported"


async def test_measured_report_before_finish_skips_placeholder(
    db: Database, store: MemorySecretStore
) -> None:
    acc = await add_account(db, store)
    task = await claim_with_lease(db, store, acc)
    async with db.write() as tx:
        await accounts.record_usage(
            tx,
            acc,
            A,
            UsageReport(
                task_id=task.id,
                attempt=task.attempt,
                input_tokens=10,
                output_tokens=1,
                run_key="r1:early",
            ),
        )
        await tasks.complete(tx, A, task.id, task.attempt, DONE)
    rows = await usage_rows(db, task.id)
    assert len(rows) == 1
    assert rows[0]["measurement"] == "measured"
    assert rows[0]["run_key"] == "r1:early"


async def test_late_measured_upgrades_placeholder_in_place(
    db: Database, store: MemorySecretStore
) -> None:
    acc = await add_account(db, store)
    task = await claim_with_lease(db, store, acc)
    async with db.write() as tx:
        await tasks.complete(tx, A, task.id, task.attempt, DONE)
    before = (await usage_rows(db, task.id))[0]
    assert before["measurement"] == "not_reported"
    # Re-lease so a late report is still allowed (§3.2).
    async with db.write() as tx:
        await tasks.create_task(
            tx, TaskCreate(project="global", title="hold lease"), "admin:test"
        )
        hold = await tasks.claim(tx, A, [])
        await accounts.lease(
            tx, A, ["openai"], store, "engine", task_id=hold.id, attempt=hold.attempt
        )
        await accounts.record_usage(
            tx,
            acc,
            A,
            UsageReport(
                task_id=task.id,
                attempt=task.attempt,
                input_tokens=42,
                output_tokens=7,
                cost_usd=0.01,
                model="m",
                engine="claude",
                role="engine",
                run_key="r1:late",
            ),
        )
    rows = await usage_rows(db, task.id)
    assert len(rows) == 1
    r = rows[0]
    assert r["measurement"] == "measured"
    assert r["input_tokens"] == 42
    assert r["run_key"] == "r1:late"
    assert r["unmeasured_reason"] is None


async def test_not_reported_after_placeholder_adds_nothing(
    db: Database, store: MemorySecretStore
) -> None:
    acc = await add_account(db, store)
    task = await claim_with_lease(db, store, acc)
    async with db.write() as tx:
        await tasks.complete(tx, A, task.id, task.attempt, DONE)
    async with db.write() as tx:
        await tasks.create_task(
            tx, TaskCreate(project="global", title="hold"), "admin:test"
        )
        hold = await tasks.claim(tx, A, [])
        await accounts.lease(
            tx, A, ["openai"], store, "engine", task_id=hold.id, attempt=hold.attempt
        )
        await accounts.record_usage(
            tx,
            acc,
            A,
            UsageReport(
                task_id=task.id,
                attempt=task.attempt,
                measurement="not_reported",
                reason="no_usage_in_output",
                run_key="r1:nr",
            ),
        )
    assert len(await usage_rows(db, task.id)) == 1


async def test_not_reported_after_measured_adds_nothing(
    db: Database, store: MemorySecretStore
) -> None:
    acc = await add_account(db, store)
    task = await claim_with_lease(db, store, acc)
    async with db.write() as tx:
        await accounts.record_usage(
            tx,
            acc,
            A,
            UsageReport(
                task_id=task.id,
                attempt=task.attempt,
                input_tokens=3,
                run_key="r1:measured",
            ),
        )
        await accounts.record_usage(
            tx,
            acc,
            A,
            UsageReport(
                task_id=task.id,
                attempt=task.attempt,
                measurement="not_reported",
                reason="no_usage_in_output",
                run_key="r1:late-nr",
            ),
        )
        await tasks.complete(tx, A, task.id, task.attempt, DONE)
    rows = await usage_rows(db, task.id)
    assert len(rows) == 1
    assert rows[0]["measurement"] == "measured"
    assert rows[0]["run_key"] == "r1:measured"


async def test_duplicate_finish_and_duplicate_run_key_are_noop(
    db: Database, store: MemorySecretStore
) -> None:
    acc = await add_account(db, store)
    task = await claim_with_lease(db, store, acc)
    async with db.write() as tx:
        await accounts.record_usage(
            tx,
            acc,
            A,
            UsageReport(
                task_id=task.id,
                attempt=task.attempt,
                input_tokens=1,
                run_key="r1:dup",
            ),
        )
        await accounts.record_usage(
            tx,
            acc,
            A,
            UsageReport(
                task_id=task.id,
                attempt=task.attempt,
                input_tokens=99,
                run_key="r1:dup",
            ),
        )
        await tasks.complete(tx, A, task.id, task.attempt, DONE)
    rows = await usage_rows(db, task.id)
    assert len(rows) == 1 and rows[0]["input_tokens"] == 1


async def test_retries_get_one_outcome_each(
    db: Database, store: MemorySecretStore
) -> None:
    acc = await add_account(db, store)
    first = await claim_with_lease(db, store, acc)
    async with db.write() as tx:
        await tasks.fail(tx, A, first.id, first.attempt, "temp", "provider_failure")
    async with db.write() as tx:
        # Transient fail parks behind retry_at; clear it so the next claim can run.
        await tx.conn.execute(
            "UPDATE tasks SET retry_at = NULL WHERE id = ?", (first.id,)
        )
        second = await tasks.claim(tx, A, [])
        assert second is not None and second.attempt == 2
        await accounts.lease(
            tx,
            A,
            ["openai"],
            store,
            "engine",
            task_id=second.id,
            attempt=second.attempt,
        )
        await tasks.complete(tx, A, second.id, second.attempt, DONE)
    rows = await usage_rows(db, first.id)
    assert {r["attempt"] for r in rows} == {1, 2}
    assert all(r["measurement"] == "not_reported" for r in rows)


async def test_result_usage_attributes_completing_attempt(
    db: Database, store: MemorySecretStore
) -> None:
    acc = await add_account(db, store)
    task = await claim_with_lease(db, store, acc)
    result = TaskResult(
        summary="done",
        usage={"account_id": acc, "input_tokens": 5, "output_tokens": 1},
    )
    async with db.write() as tx:
        await tasks.complete(tx, A, task.id, task.attempt, result)
    rows = await usage_rows(db, task.id)
    assert len(rows) == 1
    assert rows[0]["measurement"] == "measured"
    assert rows[0]["attempt"] == task.attempt
    assert rows[0]["input_tokens"] == 5


async def test_concurrent_placeholder_writers(
    db: Database, store: MemorySecretStore
) -> None:
    acc = await add_account(db, store)
    task = await claim_with_lease(db, store, acc)
    async with (
        db.read() as c,
        c.execute(
            "SELECT id, claimed_at FROM task_attempts WHERE task_id = ? AND attempt = ?",
            (task.id, task.attempt),
        ) as cur,
    ):
        claim = await cur.fetchone()
    assert claim is not None

    async def write_placeholder() -> None:
        async with db.write() as tx:
            await usage.ensure_attempt_outcome(
                tx,
                task_id=task.id,
                attempt=task.attempt,
                agent_id=A,
                claim_id=claim["id"],
                claimed_at=claim["claimed_at"],
                reason="no_report",
            )

    await asyncio.gather(*(write_placeholder() for _ in range(8)))
    rows = await usage_rows(db, task.id)
    assert len(rows) == 1
    assert (
        rows[0]["run_key"]
        == f"{PLACEHOLDER_PREFIX}{task.id}:{task.attempt}:{claim['id']}"
    )
    assert rows[0]["account_id"] == acc


async def test_ensure_outcome_without_claim_row_uses_claim_id_zero(
    db: Database, store: MemorySecretStore
) -> None:
    """Leave-claimed with no task_attempts row still writes a1:…:0."""
    async with db.write() as tx:
        await tasks.create_task(
            tx, TaskCreate(project="global", title="no claim row"), "admin:test"
        )
        task = await tasks.claim(tx, A, [])
        assert task is not None
        await tx.conn.execute(
            "DELETE FROM task_attempts WHERE task_id = ? AND attempt = ?",
            (task.id, task.attempt),
        )
        await tasks.complete(tx, A, task.id, task.attempt, DONE)
    rows = await usage_rows(db, task.id)
    assert len(rows) == 1
    assert rows[0]["run_key"] == f"{PLACEHOLDER_PREFIX}{task.id}:{task.attempt}:0"
    assert rows[0]["measurement"] == "not_reported"


async def test_delete_account_detaches_usage_keeps_attempt_outcome(
    db: Database, store: MemorySecretStore
) -> None:
    acc = await add_account(db, store, name="doomed")
    task = await claim_with_lease(db, store, acc)
    async with db.write() as tx:
        await tasks.complete(tx, A, task.id, task.attempt, DONE)
        await accounts.release(tx, A, "test")
    before = (await usage_rows(db, task.id))[0]
    assert before["account_id"] == acc
    async with db.write() as tx:
        await accounts.delete_account(tx, acc, "doomed", "admin:test")
    after = (await usage_rows(db, task.id))[0]
    assert after["account_id"] is None
    assert after["role"] is None
    assert after["measurement"] == before["measurement"]
    assert after["attempt"] == before["attempt"]
    assert after["run_key"] == before["run_key"]
    assert after["unmeasured_reason"] == before["unmeasured_reason"]


async def test_cost_unattributed_and_attempt_coverage(
    db: Database, store: MemorySecretStore
) -> None:
    acc = await add_account(db, store)
    task = await claim_with_lease(db, store, account_id=None)
    async with db.write() as tx:
        await tasks.complete(tx, A, task.id, task.attempt, DONE)
    # Measured row on the account for a different attempt-attributed task.
    async with db.write() as tx:
        await tx.conn.execute(
            "INSERT INTO account_usage (account_id, task_id, attempt, input_tokens,"
            " output_tokens, at, measurement, run_key)"
            " VALUES (?, 'other', 1, 100, 10, ?, 'measured', 'r1:other')",
            (acc, ts(NOW)),
        )
        await tx.conn.execute(
            "UPDATE account_usage SET at = ? WHERE task_id = ?",
            (ts(NOW), task.id),
        )
    async with db.read() as conn:
        report = await cost.report(
            conn, NOW, "7d", "UTC", ZoneInfo("UTC"), pricing.load(None)
        )
    assert report.unattributed.runs == 1
    assert report.unattributed.not_reported_attempts == 1
    assert report.unattributed.measured_attempts == 0
    assert report.unattributed.input_tokens is None
    by = {a.account_id: a for a in report.by_account}
    assert by[acc].measured_attempts == 1
    assert by[acc].input_tokens == 100
    assert report.totals.runs == report.unattributed.runs + by[acc].runs
    assert report.totals.attempts == (report.unattributed.attempts + by[acc].attempts)


async def test_migration_0053_backfills_attempt_and_is_idempotent(
    tmp_path: Path,
) -> None:
    migrations = load_migrations()
    assert migrations[52].version == 53
    path = tmp_path / "pre53.db"
    old = await Database.open(path, migrations=migrations[:52])
    async with old.write() as tx:
        await tx.conn.execute(
            "INSERT INTO agents (id, name, role, capabilities, state,"
            " last_heartbeat_at, created_at) VALUES ('ag', 'ag', 'worker', '[]',"
            " 'active', 't', 't')"
        )
        await tx.conn.execute(
            "INSERT INTO accounts (id, name, provider, kind, secret_ref, state,"
            " created_at, updated_at) VALUES ('a', 'a', 'p', 'api_key', 'sops:a',"
            " 'available', 't', 't')"
        )
        await tx.conn.execute(
            "INSERT INTO tasks (id, kind, title, spec, acceptance, input,"
            " required_capabilities, priority, state, requires_approval, review,"
            " lease_ttl_s, max_attempts, created_by, created_at, updated_at, project,"
            " attempt)"
            " VALUES ('task1', 'generic', 't', '', '[]', '{}', '[]', 0, 'succeeded',"
            " 0, 'none', 60, 3, 'test', 't', 't', 'global', 2)"
        )
        await tx.conn.execute(
            "INSERT INTO task_attempts (task_id, attempt, agent_id, claimed_at,"
            " definition) VALUES ('task1', 1, 'ag', '2026-01-01T00:00:00+00:00', '{}')"
        )
        await tx.conn.execute(
            "INSERT INTO task_attempts (task_id, attempt, agent_id, claimed_at,"
            " definition) VALUES ('task1', 2, 'ag', '2026-01-02T00:00:00+00:00', '{}')"
        )
        await tx.conn.execute(
            "INSERT INTO account_usage (account_id, agent_id, task_id, input_tokens,"
            " at, measurement) VALUES ('a', 'ag', 'task1', 9,"
            " '2026-01-02T01:00:00+00:00', 'measured')"
        )
    await old.close()

    db = await Database.open(path)
    try:
        async with (
            db.read() as c,
            c.execute(
                "SELECT attempt, account_id FROM account_usage WHERE task_id = 'task1'"
            ) as cur,
        ):
            row = await cur.fetchone()
            assert row is not None
            assert row[0] == 2  # backfilled from the claim at/before the row
            assert row[1] == "a"
        async with (
            db.read() as c,
            c.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'account_usage_attempt_outcome'"
            ) as cur,
        ):
            assert await cur.fetchone() is not None
        # Re-open applies nothing (user_version already at latest).
        again = await Database.open(path)
        try:
            async with again.read() as c, c.execute("PRAGMA user_version") as cur:
                assert (await cur.fetchone())[0] == 57
            async with (
                again.read() as c,
                c.execute("SELECT count(*) FROM account_usage") as cur,
            ):
                assert (await cur.fetchone())[0] == 1
        finally:
            await again.close()
    finally:
        await db.close()
