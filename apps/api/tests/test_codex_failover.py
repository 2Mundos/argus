"""Codex usage limit and expired login as quota (account selection 3.6)."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from api_support import FakeClock, insert_agent
from argus_api.accounts import service as accounts
from argus_api.accounts.eligibility import engine_pool
from argus_api.db import Database
from argus_api.errors import ConflictError
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import service as tasks
from argus_core.accounts import AccountCreate, ExhaustedReport
from argus_core.quota import sanitize_notice
from argus_core.tasks import TaskCreate
from argus_core.tasks import TaskState as S

A = "agent-a"
LIMIT = (
    "You\u2019ve hit your usage limit. Upgrade to Plus to continue using Codex "
    "(https://chatgpt.com/explore/plus?utm=x), or try again at Nov 19th, 2026 2:08 PM."
)
LIMIT_JSON = json.dumps(
    {"error": {"message": "plan spent", "codexErrorInfo": "usageLimitExceeded"}}
)
EXPIRED = "401 Unauthorized: token_expired"
RUNTIME = ["anthropic", "openai", "custom", "local"]


@pytest.fixture
def store() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture(autouse=True)
async def agent(db: Database) -> None:
    await insert_agent(db, A)


async def add(db: Database, store, name: str, provider="openai", **kw) -> str:
    await store.put(name, {"api_key": f"key-{name}"})
    async with db.write() as tx:
        account = await accounts.create_account(
            tx,
            name,
            AccountCreate(
                name=name, provider=provider, secret_ref=f"sops:{name}", **kw
            ),
            f"sops:{name}",
            "admin:test",
            store,
        )
    return account.id


async def claim(db: Database):
    async with db.write() as tx:
        task = await tasks.claim(tx, A, [])
    assert task is not None
    return task


async def lease(db: Database, store, task):
    async with db.write() as tx:
        return await accounts.lease(
            tx, A, RUNTIME, store, task_id=task.id, attempt=task.attempt
        )


async def started(db: Database, store, **kw):
    """Create a task, claim it and lease the engine account for the claim."""
    async with db.write() as tx:
        await tasks.create_task(tx, TaskCreate(project="global", title="t", **kw), "u")
    task = await claim(db)
    assert (await lease(db, store, task)) is not None
    return task


async def fail(db: Database, store, task, error, reason="provider_failure"):
    async with db.write() as tx:
        return await accounts.fail_task(
            tx, A, task.id, task.attempt, error, reason, store, timedelta(hours=1)
        )


async def events(db: Database, kind: str) -> list[dict]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT data FROM events WHERE kind = ? ORDER BY id", (kind,)
        ) as c,
    ):
        return [json.loads(r[0]) for r in await c.fetchall()]


@pytest.mark.parametrize("error", [LIMIT, LIMIT_JSON])
async def test_codex_limit_fails_over_at_the_next_claim_without_an_attempt(
    db: Database, store: MemorySecretStore, error: str
) -> None:
    await add(db, store, "codex-a", priority=100)
    await add(db, store, "codex-b", priority=90)
    task = await started(db, store)
    after = await fail(db, store, task, error)
    assert (after.state, after.attempt, after.failures) == (S.READY, 1, 0)
    assert after.failover.reason_code == "quota_exhausted"  # type: ignore[union-attr]
    assert after.failover.outcome == "switched"  # type: ignore[union-attr]
    assert after.failover.apply == "pending"  # type: ignore[union-attr]
    # No lease yet: the next claim takes a new one, bound to it.
    nxt = await claim(db)
    assert (nxt.attempt, nxt.failures) == (2, 0)
    assert (await lease(db, store, nxt)).name == "codex-b"


async def test_codex_limit_without_a_zone_uses_the_cooldown(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    await add(db, store, "codex-a")
    task = await started(db, store)
    after = await fail(db, store, task, LIMIT)
    assert (after.state, after.block_reason) == (S.BLOCKED, "awaiting_quota")
    assert after.refill_at == clock.now + timedelta(hours=1)
    f = after.failover
    assert (f.reset_status, f.reset_text) == ("ambiguous", "Nov 19th, 2026 2:08 PM")  # type: ignore[union-attr]
    assert "utm" not in f.reason  # type: ignore[union-attr]
    [event] = await events(db, "account.exhausted")
    assert event["until_kind"] == "cooldown"


async def test_expired_login_rotates_with_its_own_code(
    db: Database, store: MemorySecretStore
) -> None:
    await add(db, store, "codex-a", priority=100)
    await add(db, store, "codex-b", priority=90)
    task = await started(db, store)
    after = await fail(db, store, task, EXPIRED)
    assert (after.state, after.failures) == (S.READY, 0)
    assert after.failover.reason_code == "auth_expired"  # type: ignore[union-attr]


async def test_model_talk_about_limits_still_counts(
    db: Database, store: MemorySecretStore
) -> None:
    await add(db, store, "codex-a")
    task = await started(db, store)
    after = await fail(db, store, task, "I noted that you've hit your usage limit")
    assert (after.state, after.failures) == (S.READY, 1)
    assert await events(db, "account.failover") == []


async def test_runtime_switch_failed_blocks_without_spending_an_attempt(
    db: Database, store: MemorySecretStore
) -> None:
    await add(db, store, "codex-a")
    task = await started(db, store)
    after = await fail(
        db, store, task, "runtime still up Bearer abcdefghijkl", "runtime_switch_failed"
    )
    assert (after.state, after.block_reason) == (S.BLOCKED, "runtime_switch_failed")
    assert (after.attempt, after.failures) == (1, 0)
    assert after.failure_class == "runtime_switch_failed"
    [event] = await events(db, "account.switch_failed")
    assert event["account"] == "codex-a" and "abcdefghijkl" not in json.dumps(event)
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT count(*) FROM account_leases WHERE agent_id = ? AND released_at IS NULL",
            (A,),
        ) as cur,
    ):
        assert (await cur.fetchone())[0] == 0  # type: ignore[index]


@pytest.mark.parametrize("how", ["exhausted", "disabled"])
async def test_claim_on_an_unusable_account_is_refused(
    db: Database, store: MemorySecretStore, clock: FakeClock, how: str
) -> None:
    held = await add(db, store, "codex-a", priority=100)
    await add(db, store, "codex-b", priority=90)
    first = await started(db, store)
    async with db.write() as tx:
        await tasks.cancel(tx, first.id, "admin:test")
    until = (clock.now + timedelta(hours=2)).isoformat()
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE accounts SET state = ?, exhausted_until = ?, disabled_reason = ?"
            " WHERE id = ?",
            (
                how,
                until if how == "exhausted" else None,
                "test" if how == "disabled" else None,
                held,
            ),
        )
        await tasks.create_task(tx, TaskCreate(project="global", title="u"), "u")
    with pytest.raises(ConflictError) as err:
        await claim(db)
    assert err.value.code == "lease_account_unusable"


async def test_engine_pool_counts_engines_only(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    async with db.read() as conn:
        assert (await engine_pool(conn, clock.now)).usable == 0
    await add(db, store, "cursor-1", provider="cursor")
    await add(db, store, "repo", provider="gitea")
    async with db.read() as conn:
        assert (await engine_pool(conn, clock.now)).usable == 0
    await store.put("local-1", {"base_url": "http://localhost:8080", "auth_token": "t"})
    async with db.write() as tx:
        await accounts.create_account(
            tx,
            "local-1",
            AccountCreate(
                name="local-1",
                provider="local",
                credential={"base_url": "http://localhost:8080", "auth_token": "t"},
                priority=-10,
            ),
            "sops:local-1",
            "admin:test",
            store,
        )
    async with db.read() as conn:
        pool = await engine_pool(conn, clock.now)
    assert (pool.usable, pool.awaiting_quota) == (1, False)


def test_sanitize_notice_drops_tokens_and_query_strings() -> None:
    out = sanitize_notice(
        "x\x00y Bearer abcdefghijkl sk-abcdefghijkl https://a.io/p?token=zzz\nend"
    )
    assert "\n" not in out and "\x00" not in out
    assert not any(s in out for s in ("abcdefghijkl", "zzz", "?"))
    assert len(sanitize_notice("a" * 900)) == 500


async def test_exhausted_report_needs_a_claim_and_custom_is_an_engine(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    # No claim at all is the internal orchestrator's form, refused for anyone else
    # by the route (test_the_internal_orchestrator_fails_over_without_a_task).
    with pytest.raises(ValueError):
        ExhaustedReport(runtime_providers=["openai"], task_id="t")
    with pytest.raises(ValueError):
        ExhaustedReport(runtime_providers=["openai"], task_id="t", attempt=0)
    await store.put("c", {"api_key": "k"})
    async with db.write() as tx:
        await accounts.create_account(
            tx,
            "c",
            AccountCreate(
                name="c",
                provider="custom",
                secret_ref="sops:c",
                config={
                    "base_url": "https://openrouter.ai/api/v1",
                    "label": "or",
                    "engine": {"model": "m"},
                },  # type: ignore[arg-type]
            ),
            "sops:c",
            "admin:test",
            store,
        )
    async with db.read() as conn:
        assert (await engine_pool(conn, clock.now)).usable == 1


@pytest.mark.parametrize(
    ("error", "source"), [(LIMIT, "notice"), (LIMIT_JSON, "structured")]
)
async def test_failover_labels_the_source_of_the_hit(
    db: Database, store: MemorySecretStore, error: str, source: str
) -> None:
    await add(db, store, "codex-a")
    task = await started(db, store)
    after = await fail(db, store, task, error)
    assert after.failover.source == source  # type: ignore[union-attr]


def test_exhausted_report_reset_status_must_match_until() -> None:
    base = {"runtime_providers": ["openai"], "task_id": "t", "attempt": 1}
    zoned = datetime(2026, 1, 2, tzinfo=UTC)
    ExhaustedReport(**base, until=zoned, reset_status="stated")
    ExhaustedReport(**base, reset_status="absent")
    ExhaustedReport(**base, reset_status="ambiguous", reset_text="3pm")
    for bad in (
        {"reset_status": "stated"},  # no time
        {"reset_status": "stated", "until": datetime(2026, 1, 2)},  # no zone
        {"reset_status": "absent", "until": zoned},
        {"reset_status": "ambiguous", "until": zoned},
    ):
        with pytest.raises(ValueError):
            ExhaustedReport(**base, **bad)  # type: ignore[arg-type]
