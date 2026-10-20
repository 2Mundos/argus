"""Cost view: aggregation, windows, pricing, honesty about missing figures
(docs/specs/cost-view.spec.md)."""

import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from argus_api.accounts import cost, pricing, usage
from argus_api.accounts import service as accounts
from argus_api.accounts.pricing import PriceTable, PricingInvalidError
from argus_api.config import Settings
from argus_api.db import Database, ts
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from argus_core.accounts import AccountCreate, QuotaReport
from argus_core.cost import CostReport
from fastapi.testclient import TestClient

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
UTC_ZONE = ZoneInfo("UTC")
TABLE = pricing.load(None)


async def add_account(db: Database, name: str, provider: str = "openai") -> str:
    store = MemorySecretStore()
    await store.put(name, {"api_key": "x"})
    async with db.write() as tx:
        account = await accounts.create_account(
            tx,
            name,
            AccountCreate(
                name=name,
                provider=provider,
                kind="api_key",
                secret_ref=f"sops:{name}",
            ),
            f"sops:{name}",
            "admin:test",
            store,
        )
    return account.id


async def add_task(db: Database, task_id: str, project: str) -> None:
    async with db.write() as tx:
        now = ts(tx.now)
        await tx.conn.execute(
            "INSERT OR IGNORE INTO projects (slug, name, description, created_at,"
            " updated_at) VALUES (?, ?, '', ?, ?)",
            (project, project, now, now),
        )
        await tx.conn.execute(
            "INSERT INTO tasks (id, kind, title, spec, acceptance, input,"
            " required_capabilities, priority, state, requires_approval, review,"
            " lease_ttl_s, max_attempts, created_by, created_at, updated_at, project)"
            " VALUES (?, 'generic', 't', '', '[]', '{}', '[]', 0, 'succeeded', 0,"
            " 'none', 60, 3, 'test', ?, ?, ?)",
            (task_id, now, now, project),
        )


async def usage_row(
    db: Database,
    account_id: str,
    at: datetime,
    *,
    i: int | None = None,
    o: int | None = None,
    c: int | None = None,
    model: str | None = "claude-sonnet-5-5",
    task: str | None = None,
    reported: float | None = None,
    measurement: str = "measured",
) -> None:
    async with db.write() as tx:
        await tx.conn.execute(
            "INSERT INTO account_usage (account_id, task_id, input_tokens,"
            " output_tokens, cached_input_tokens, cost_usd, model, at, measurement)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, task, i, o, c, reported, model, ts(at), measurement),
        )


async def run(
    db: Database,
    window: str = "7d",
    tz: str = "UTC",
    table: PriceTable = TABLE,
) -> CostReport:
    async with db.read() as conn:
        return await cost.report(conn, NOW, window, tz, ZoneInfo(tz), table)  # ty: ignore[invalid-argument-type]


async def test_aggregates_per_account_project_and_model(db: Database) -> None:
    a = await add_account(db, "a")
    b = await add_account(db, "b")
    await add_task(db, "t1", "alpha")
    await add_task(db, "t2", "beta")
    h = NOW - timedelta(hours=2)
    # sonnet: 1M in x3 + 1M out x15 + 1M cached x0.3 = 18.3
    await usage_row(db, a, h, i=1_000_000, o=1_000_000, c=1_000_000, task="t1")
    # opus: 100k in x5 = 0.5, 100k out x25 = 2.5
    await usage_row(db, a, h, i=100_000, o=100_000, model="claude-opus-5-5", task="t2")
    await usage_row(db, b, h, i=10, o=20, task="t1", reported=0.25)
    await usage_row(db, b, h, i=1, o=2)  # no task: "no project"

    r = await run(db)
    assert r.totals.runs == 4
    assert r.totals.input_tokens == 1_100_011
    assert r.totals.output_tokens == 1_100_022
    assert r.totals.cached_input_tokens == 1_000_000
    assert r.totals.reported_cost_usd == pytest.approx(0.25)
    expected_a = 18.3 + 3.0
    tiny = (11 * 3.0 + 22 * 15.0) / 1_000_000
    assert r.totals.estimated_cost_usd == pytest.approx(expected_a + tiny)

    accs = {x.name: x for x in r.by_account}
    assert accs["a"].estimated_cost_usd == pytest.approx(expected_a)
    assert accs["a"].reported_cost_usd is None  # never mixed with the estimate
    assert accs["b"].reported_cost_usd == pytest.approx(0.25)
    assert [x.name for x in r.by_account] == ["a", "b"]  # sorted by cost

    projects = {x.project: x for x in r.by_project}
    assert set(projects) == {"alpha", "beta", None}
    assert projects["alpha"].input_tokens == 1_000_010
    assert projects["beta"].estimated_cost_usd == pytest.approx(3.0)
    assert projects[None].input_tokens == 1  # the explicit "no project" bucket
    assert r.by_project[0].project == "alpha"  # most expensive first


async def test_window_boundaries(db: Database) -> None:
    a = await add_account(db, "a")
    await usage_row(db, a, NOW - timedelta(hours=23, minutes=59), i=1, o=1)
    await usage_row(db, a, NOW - timedelta(hours=24, seconds=1), i=10, o=10)
    await usage_row(db, a, NOW - timedelta(days=6, hours=23), i=100, o=100)
    await usage_row(db, a, NOW - timedelta(days=7, minutes=1), i=1000, o=1000)
    await usage_row(db, a, NOW - timedelta(days=29, hours=23), i=10_000, o=10_000)
    await usage_row(db, a, NOW - timedelta(days=30, minutes=1), i=100_000, o=100_000)
    await usage_row(db, a, NOW + timedelta(minutes=1), i=7, o=7)  # the future

    assert (await run(db, "24h")).totals.input_tokens == 1
    assert (await run(db, "7d")).totals.input_tokens == 111
    assert (await run(db, "30d")).totals.input_tokens == 11_111


async def test_account_without_usage_is_not_a_measured_zero(db: Database) -> None:
    a = await add_account(db, "used")
    idle = await add_account(db, "idle")
    zero = await add_account(db, "zero")
    await usage_row(db, a, NOW - timedelta(hours=1), i=5, o=5)
    await usage_row(db, zero, NOW - timedelta(hours=1), i=0, o=0, c=0)

    r = await run(db)
    by = {x.account_id: x for x in r.by_account}
    assert by[idle].runs == 0 and by[idle].measured_runs == 0
    assert by[idle].input_tokens is None and by[idle].estimated_cost_usd is None
    assert by[zero].measured_runs == 1 and by[zero].input_tokens == 0
    assert by[zero].estimated_cost_usd == 0.0  # priced and measured: a real 0
    assert by[idle].quota_status.state == "unknown"
    assert by[idle].quota_status.reason == "no_usage_records"
    assert r.by_account[-1].account_id == idle  # no usage sorts last


async def test_quota_status_is_the_accounts_screen_one(db: Database) -> None:
    a = await add_account(db, "a")
    await usage_row(db, a, NOW - timedelta(hours=1), i=1, o=1)
    r = await run(db)
    async with db.read() as conn:
        listed = {x.id: x for x in await accounts.list_accounts(conn, NOW)}
    assert r.by_account[0].quota_status == listed[a].quota_status


async def test_quota_windows_are_the_accounts_screen_ones(db: Database) -> None:
    snap = await add_account(db, "snap")
    bare = await add_account(db, "bare")
    async with db.write() as tx:
        await usage.upsert_quota(
            tx,
            snap,
            [QuotaReport(window_minutes=300, used_percent=42)],
            "reported",
            "t",
        )
    r = await run(db)
    async with db.read() as conn:
        listed = {x.id: x for x in await accounts.list_accounts(conn, NOW)}
    by = {x.account_id: x for x in r.by_account}
    assert [q.window for q in by[snap].quota] == ["300m"]
    assert by[bare].quota == []
    for aid in (snap, bare):
        assert by[aid].quota == listed[aid].quota
        assert by[aid].quota_status == listed[aid].quota_status


async def test_not_reported_runs_are_counted_not_summed(db: Database) -> None:
    a = await add_account(db, "mixed")
    only = await add_account(db, "only-nr")
    t = NOW - timedelta(hours=1)
    await usage_row(db, a, t, i=10, o=10)
    await usage_row(db, a, t, measurement="not_reported")
    await usage_row(db, only, t, measurement="not_reported", model=None)
    await usage_row(db, only, t, measurement="not_reported")

    r = await run(db)
    by = {x.name: x for x in r.by_account}
    assert (by["mixed"].runs, by["mixed"].measured_runs) == (2, 1)
    assert by["mixed"].not_reported_runs == 1
    assert by["mixed"].input_tokens == 10
    assert by["only-nr"].not_reported_runs == 2
    assert by["only-nr"].input_tokens is None  # null, not 0
    assert by["only-nr"].output_tokens is None
    assert by["only-nr"].estimated_cost_usd is None
    assert r.totals.not_reported_runs == 3
    assert r.totals.measured_runs == 1
    assert r.pricing.unpriced_models == []  # nothing measured, nothing unpriced
    day = next(d for d in r.daily if d.runs)
    assert day.not_reported_runs == 3


async def test_unpriced_models_are_surfaced_never_zero(db: Database) -> None:
    a = await add_account(db, "a")
    t = NOW - timedelta(hours=1)
    await usage_row(db, a, t, i=1_000_000, o=0, model="mystery-1")
    await usage_row(db, a, t, i=5, o=5, model=None)
    r = await run(db)
    assert r.totals.input_tokens == 1_000_005  # tokens still counted
    assert r.totals.estimated_cost_usd is None
    assert r.totals.unpriced_runs == 2
    assert r.pricing.unpriced_models == ["(no model)", "mystery-1"]

    await usage_row(db, a, t, i=1_000_000, o=0)  # one priced run joins
    r = await run(db)
    assert r.totals.estimated_cost_usd == pytest.approx(3.0)  # priced part only
    assert r.totals.unpriced_runs == 2


async def test_cost_only_run_is_not_unpriced(db: Database) -> None:
    a = await add_account(db, "a")
    await usage_row(db, a, NOW - timedelta(hours=1), reported=0.5, model="mystery-1")
    r = await run(db)
    assert r.totals.reported_cost_usd == 0.5
    assert r.totals.input_tokens is None
    assert r.totals.unpriced_runs == 0 and r.pricing.unpriced_models == []


async def test_dated_model_suffix_uses_the_family_price(db: Database) -> None:
    a = await add_account(db, "a")
    await usage_row(
        db, a, NOW - timedelta(hours=1), i=1_000_000, model="Claude-Sonnet-5-5-20261001"
    )
    assert (await run(db)).totals.estimated_cost_usd == pytest.approx(3.0)


async def test_daily_series_is_zero_filled(db: Database) -> None:
    a = await add_account(db, "a")
    await usage_row(db, a, datetime(2026, 10, 15, 10, 0, tzinfo=UTC), i=1, o=1)
    await usage_row(db, a, datetime(2026, 10, 18, 23, 50, tzinfo=UTC), i=2, o=2)
    r = await run(db, "7d")
    days = [d.start.date().isoformat() for d in r.daily]
    assert days == [f"2026-10-{d}" for d in range(13, 21)]  # 13th..20th, no gaps
    by = {d.start.day: d for d in r.daily}
    assert by[15].input_tokens == 1 and by[18].input_tokens == 2
    assert by[14].runs == 0 and by[14].input_tokens is None  # empty, not missing
    assert sum(d.runs for d in r.daily) == r.totals.runs


async def test_daily_series_follows_the_timezone(db: Database) -> None:
    a = await add_account(db, "a")
    # 23:50 UTC on the 18th is already the 19th in Kolkata (UTC+5:30).
    await usage_row(db, a, datetime(2026, 10, 18, 23, 50, tzinfo=UTC), i=2, o=2)
    r = await run(db, "7d", tz="Asia/Kolkata")
    by = {d.start.day: d for d in r.daily}
    assert by[19].input_tokens == 2 and by[18].runs == 0
    assert r.daily[0].start.tzinfo is not None


async def test_gitea_accounts_are_not_listed(db: Database) -> None:
    await add_account(db, "repo", provider="gitea")
    assert (await run(db)).by_account == []


async def test_aggregation_is_read_only_and_indexed(db: Database) -> None:
    a = await add_account(db, "a")
    await usage_row(db, a, NOW - timedelta(hours=1), i=1, o=1)
    async with db.read() as conn:
        before = await (await conn.execute("PRAGMA data_version")).fetchone()
        count = await (
            await conn.execute("SELECT count(*) FROM account_usage")
        ).fetchone()
    await run(db, "30d")
    async with db.read() as conn:
        after = await (
            await conn.execute("SELECT count(*) FROM account_usage")
        ).fetchone()
        plan = await (
            await conn.execute(
                "EXPLAIN QUERY PLAN SELECT account_id, count(*) FROM account_usage u"
                " WHERE u.at > ? AND u.at <= ? GROUP BY account_id",
                ("a", "b"),
            )
        ).fetchall()
    assert before is not None and count is not None and after is not None
    assert count[0] == after[0] == 1
    assert any("account_usage_at" in row[3] for row in plan), plan


# -- price table ----------------------------------------------------------------


def test_shipped_table_covers_the_repo_models() -> None:
    for model in (
        "claude-sonnet-5-5",
        "claude-opus-5-5",
        "claude-haiku-5-5",
        "gpt-6-luna",
    ):
        assert TABLE.find(model) is not None
    assert TABLE.source == pricing.DEFAULT_SOURCE
    assert TABLE.find("claude-sonnet-5") is None  # a prefix is not a family
    assert TABLE.find(None) is None


def test_price_file_overrides_and_bad_files_fail_whole(tmp_path: Path) -> None:
    good = tmp_path / "p.json"
    good.write_text(
        json.dumps(
            {
                "currency": "EUR",
                "as_of": "2026-01-01",
                "version": "v1",
                "models": {"m": {"input": 1, "output": 2, "cached_input": 0.5}},
            }
        )
    )
    table = pricing.load(good)
    assert (table.currency, table.source) == ("EUR", str(good))
    assert table.cost("m", 1_000_000, 1_000_000, 2_000_000) == pytest.approx(4.0)

    for doc in (
        "{",
        json.dumps({"currency": "EUR"}),
        json.dumps(
            {
                "currency": "EUR",
                "as_of": "x",
                "version": "v",
                "models": {"m": {"input": -1, "output": 2, "cached_input": 0}},
            }
        ),
    ):
        good.write_text(doc)
        with pytest.raises(PricingInvalidError):
            pricing.load(good)
    with pytest.raises(PricingInvalidError):
        pricing.load(tmp_path / "missing.json")


def test_settings_read_the_pricing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGUS_PRICING_FILE", "/etc/argus/prices.json")
    assert Settings.from_env().pricing_file == Path("/etc/argus/prices.json")
    monkeypatch.delenv("ARGUS_PRICING_FILE")
    assert Settings.from_env().pricing_file is None


# -- HTTP -----------------------------------------------------------------------

ADMIN = {"Authorization": "Bearer cost-admin-token-" + "c" * 32}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", "cost-admin-token-" + "c" * 32)
    with TestClient(app) as c:
        yield c


def test_endpoint_is_manager_only_and_validates_input(client: TestClient) -> None:
    boot = client.post(
        "/api/agents/bootstrap-tokens", json={"role": "worker"}, headers=ADMIN
    ).json()
    worker = client.post(
        "/api/agents/register", json={"bootstrap_token": boot["token"]}
    ).json()["token"]
    assert client.get("/api/accounts/cost").status_code == 401
    denied = client.get(
        "/api/accounts/cost", headers={"Authorization": f"Bearer {worker}"}
    )
    assert denied.status_code == 403
    assert client.get("/api/accounts/cost?window=1y", headers=ADMIN).status_code == 422
    assert (
        client.get("/api/accounts/cost?tz=Nope/Zone", headers=ADMIN).status_code == 422
    )


def test_endpoint_shape_for_an_empty_install(client: TestClient) -> None:
    res = client.get("/api/accounts/cost?window=7d&tz=UTC", headers=ADMIN)
    assert res.status_code == 200
    body = res.json()
    assert body["window"] == "7d"
    assert body["totals"]["runs"] == 0 and body["totals"]["input_tokens"] is None
    assert body["pricing"]["estimate"] is True
    assert body["pricing"]["currency"] == "USD"
    assert body["pricing"]["source"] == pricing.DEFAULT_SOURCE
    assert body["by_account"] == [] and body["by_project"] == []
    assert len(body["daily"]) in (7, 8)
    assert all(d["runs"] == 0 for d in body["daily"])


def test_endpoint_reports_a_broken_price_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", "cost-admin-token-" + "c" * 32)
    monkeypatch.setenv("ARGUS_PRICING_FILE", str(bad))
    with TestClient(app) as c:
        res = c.get("/api/accounts/cost", headers=ADMIN)
    assert res.status_code == 500
    assert "pricing_invalid" in res.text
