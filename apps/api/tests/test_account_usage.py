"""Account usage: reports, quota snapshots, aggregation, and the Codex quota probe
(docs/specs/account-usage.spec.md)."""

import asyncio
import base64
import json
import sys
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from api_support import FakeClock, insert_agent
from argus_api import ops
from argus_api.accounts import quota_probe, usage
from argus_api.accounts import service as accounts
from argus_api.accounts.quota_probe import QuotaProber
from argus_api.config import Settings
from argus_api.db import Database, ts
from argus_api.db.migrate import load_migrations
from argus_api.errors import ValidationFailedError
from argus_api.events import Event
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import service as tasks
from argus_core.accounts import AccountCreate, AccountUpdate, QuotaReport, UsageReport
from argus_core.tasks import TaskCreate
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from pydantic import ValidationError

A = "agent-a"


async def lease(
    db: Database, store: MemorySecretStore, providers: list[str], role="engine"
):
    async with db.write() as tx:
        await tasks.create_task(
            tx, TaskCreate(project="global", title="usage lease test"), "admin:test"
        )
        task = await tasks.claim(tx, A, [])
    assert task is not None
    async with db.write() as tx:
        return await accounts.lease(
            tx,
            A,
            providers,
            store,
            role,
            task_id=task.id,
            attempt=task.attempt,
        )


@pytest.fixture
def store() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture(autouse=True)
async def agent(db: Database) -> None:
    await insert_agent(db, A)


async def add(
    db: Database,
    store: MemorySecretStore,
    name: str,
    provider: str,
    credential: dict,
    kind: str = "api_key",
) -> str:
    await store.put(name, credential)
    async with db.write() as tx:
        account = await accounts.create_account(
            tx,
            name,
            AccountCreate(
                name=name,
                provider=provider,
                kind=kind,  # ty: ignore[invalid-argument-type]
                secret_ref=f"sops:{name}",
            ),
            f"sops:{name}",
            "admin:test",
            store,
        )
    return account.id


async def report(db: Database, account_id: str, **kw) -> None:
    figures = ("input_tokens", "output_tokens", "cached_input_tokens", "cost_usd")
    if not any(k in kw for k in figures):
        kw.setdefault("measurement", "not_reported")  # e.g. a quota-only report
    async with db.write() as tx:
        await accounts.record_usage(tx, account_id, A, UsageReport(**kw))


async def quota_events(db: Database, account_id: str) -> list[dict]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT data FROM events WHERE subject_id = ? AND kind = 'account.quota'",
            (account_id,),
        ) as cur,
    ):
        return [json.loads(r[0]) for r in await cur.fetchall()]


def jwt(expires: datetime) -> str:
    def part(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': int(expires.timestamp())})}.sig"


def chatgpt_login(expires_in: timedelta = timedelta(hours=1), refresh="r1") -> dict:
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": "id",
            "access_token": jwt(datetime.now(UTC) + expires_in),
            "refresh_token": refresh,
        },
    }


# -- reports and snapshots ---------------------------------------------------------


def test_a_quota_report_names_exactly_one_window() -> None:
    assert QuotaReport(window_minutes=300, used_percent=12).window == "300m"
    assert QuotaReport(pool="third_party").window == "third_party"
    for bad in ({}, {"window_minutes": 300, "pool": "cursor"}):
        with pytest.raises(ValidationError):
            QuotaReport(**bad)


async def test_reported_rate_limits_become_snapshots(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    c = await add(db, store, "codex-1", "openai", chatgpt_login())
    await lease(db, store, ["openai"])
    resets = datetime(2026, 10, 20, tzinfo=UTC)
    limits = [
        {"window_minutes": 10080, "used_percent": 40.0, "resets_at": resets},
        {"window_minutes": 300, "used_percent": 12.5},
    ]
    await report(db, c, input_tokens=10, rate_limits=limits, role="engine")
    async with db.read() as conn:
        account = await accounts.get_account(conn, c, clock.now)
    assert [(q.window, q.used_percent, q.source) for q in account.quota] == [
        ("300m", 12.5, "reported"),
        ("10080m", 40.0, "reported"),
    ]
    assert account.quota[1].resets_at == resets
    assert await quota_events(db, c) == [{"windows": ["10080m", "300m"]}]

    clock.advance(60)  # same figures again: observed later, no new event
    await report(db, c, rate_limits=limits)
    assert len(await quota_events(db, c)) == 1
    limits[1]["used_percent"] = 30.0
    await report(db, c, rate_limits=limits)
    assert (await quota_events(db, c))[-1] == {"windows": ["300m"]}


async def test_a_pool_snapshot_has_no_percentage(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    c = await add(db, store, "cursor-1", "cursor", {"api_key": "k"})
    await lease(db, store, ["cursor"], "executor")
    until = datetime(2026, 11, 3, tzinfo=UTC)
    await report(db, c, rate_limits=[{"pool": "third_party", "exhausted_until": until}])
    async with db.read() as conn:
        (q,) = (await accounts.get_account(conn, c, clock.now)).quota
    assert (q.window, q.window_minutes, q.used_percent, q.exhausted_until) == (
        "third_party",
        None,
        None,
        until,
    )


# -- aggregation ----------------------------------------------------------------------


def test_hour_buckets_follow_local_clocks() -> None:
    berlin = ZoneInfo("Europe/Berlin")  # spring forward on 2026-03-29
    day = datetime(2026, 3, 29, tzinfo=berlin)
    next_day = datetime(2026, 3, 30, tzinfo=berlin)
    hours = usage.bucket_starts(day, next_day, "hour", berlin)
    assert len(hours) == 23  # the 02:00 hour does not exist that day
    assert "02:00" not in [h.strftime("%H:%M") for h in hours]
    kolkata = ZoneInfo("Asia/Kolkata")  # +05:30
    start = datetime(2026, 1, 1, 0, 0, tzinfo=kolkata)
    hours = usage.bucket_starts(start, start + timedelta(hours=3), "hour", kolkata)
    assert [h.strftime("%H:%M") for h in hours] == ["00:00", "01:00", "02:00"]


async def test_series_buckets_in_the_viewers_timezone(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    c = await add(db, store, "codex-1", "openai", chatgpt_login())
    idle = await add(db, store, "codex-2", "openai", chatgpt_login(refresh="r2"))
    await lease(db, store, ["openai"])
    clock.now = datetime(2026, 10, 2, 2, 30, tzinfo=UTC)  # Oct 1, 23:30 in São Paulo
    await report(db, c, input_tokens=100, output_tokens=10, cached_input_tokens=60)
    clock.now = datetime(2026, 10, 2, 15, 0, tzinfo=UTC)
    await report(db, c, input_tokens=5, cost_usd=0.5)
    sp = ZoneInfo("America/Sao_Paulo")
    start, end = datetime(2026, 10, 1, tzinfo=sp), datetime(2026, 10, 4, tzinfo=sp)
    async with db.read() as conn:
        busy, quiet = await usage.series(conn, start, end, "day", sp, [c, idle])
    assert [(b.start.day, b.input_tokens, b.runs) for b in busy.buckets] == [
        (1, 100, 1),
        (2, 5, 1),
        (3, 0, 0),  # a measured zero
    ]
    assert busy.totals.cached_input_tokens == 60
    assert busy.totals.cost_usd == pytest.approx(0.5)
    assert busy.reported and not quiet.reported
    assert all(b.runs == 0 for b in quiet.buckets)


async def test_too_many_buckets_are_refused(db: Database) -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    async with db.read() as conn:
        with pytest.raises(ValidationFailedError):
            await usage.series(
                conn, start, start + timedelta(days=60), "hour", ZoneInfo("UTC"), []
            )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", "usage-admin-token-" + "u" * 32)
    with TestClient(app) as c:
        yield c


def test_usage_endpoint_is_for_managers(client: TestClient) -> None:
    admin = {"Authorization": "Bearer usage-admin-token-" + "u" * 32}
    boot = client.post(
        "/api/agents/bootstrap-tokens", json={"role": "worker"}, headers=admin
    ).json()
    worker = client.post(
        "/api/agents/register", json={"bootstrap_token": boot["token"]}
    ).json()["token"]
    params = {"from": "2026-10-01T00:00:00Z", "to": "2026-10-02T00:00:00Z"}
    res = client.get("/api/accounts/usage", params=params, headers=admin)
    assert res.status_code == 200
    assert res.json()["accounts"] == []
    res = client.get(
        "/api/accounts/usage",
        params=params,
        headers={"Authorization": f"Bearer {worker}"},
    )
    assert res.status_code == 403
    bad = client.get(
        "/api/accounts/usage", params={**params, "tz": "Mars/Base"}, headers=admin
    )
    assert bad.status_code == 422


# -- the Codex quota probe ------------------------------------------------------------

RATE_LIMITS = {
    "rateLimits": {
        "primary": {
            "usedPercent": 37,
            "windowDurationMins": 300,
            "resetsAt": 1792000000,
        },
        "secondary": {"usedPercent": 81, "windowDurationMins": 10080, "resetsAt": None},
    }
}


def fake_codex(tmp_path: Path, *, rotate: bool = False) -> Path:
    """A stand-in for `codex app-server`: answers initialize and rateLimits/read;
    with `rotate`, rewrites auth.json with a new refresh token first."""
    script = tmp_path / "codex"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"ROTATE, LIMITS = {rotate!r}, {json.dumps(RATE_LIMITS)!r}\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n"
        "    if msg.get('id') == 1:\n"
        "        print(json.dumps({'id': 1, 'result': {}}), flush=True)\n"
        "    elif msg.get('method') == 'account/rateLimits/read':\n"
        "        if ROTATE:\n"
        "            path = os.path.join(os.environ['CODEX_HOME'], 'auth.json')\n"
        "            doc = json.load(open(path))\n"
        "            doc['tokens']['refresh_token'] = 'rotated'\n"
        "            json.dump(doc, open(path, 'w'))\n"
        "        print(json.dumps({'id': 2, 'result': json.loads(LIMITS)}), flush=True)\n"
    )
    script.chmod(0o755)
    return script


def prober(db, store, tmp_path: Path, codex: Path | None) -> QuotaProber:
    settings = Settings(data_dir=tmp_path, codex_bin=str(codex) if codex else None)
    return QuotaProber(db, store, settings)


async def test_probe_stores_polled_windows(
    db: Database, store: MemorySecretStore, tmp_path: Path
) -> None:
    c = await add(db, store, "codex-1", "openai", chatgpt_login())
    p = prober(db, store, tmp_path, fake_codex(tmp_path))
    result = await p.refresh(c)
    assert result.status == "updated"
    assert [(q.window, q.used_percent, q.source) for q in result.quota] == [
        ("300m", 37.0, "polled"),
        ("10080m", 81.0, "polled"),
    ]
    assert result.quota[0].resets_at == datetime.fromtimestamp(1792000000, UTC)
    assert (await p.refresh(c)).status == "rate_limited"
    assert store.get("codex-1") == chatgpt_login() | {
        "tokens": store.get("codex-1")["tokens"]
    }  # untouched


@pytest.mark.parametrize(
    ("credential", "reason"),
    [
        ({"auth_mode": "apikey", "OPENAI_API_KEY": "sk"}, "API-key"),
        (chatgpt_login(expires_in=timedelta(minutes=5)), "expires soon"),
    ],
)
async def test_probe_skips_what_it_must_not_touch(
    db: Database, store: MemorySecretStore, tmp_path: Path, credential, reason
) -> None:
    c = await add(db, store, "codex-1", "openai", credential)
    result = await prober(db, store, tmp_path, fake_codex(tmp_path)).refresh(c)
    assert result.status == "skipped"
    assert reason in (result.reason or "")
    assert result.quota == []


async def test_probe_without_a_binary_is_skipped(
    db: Database, store: MemorySecretStore, tmp_path: Path
) -> None:
    c = await add(db, store, "codex-1", "openai", chatgpt_login())
    result = await prober(db, store, tmp_path, None).refresh(c)
    assert (result.status, result.reason) == (
        "skipped",
        "codex binary not configured (ARGUS_CODEX_BIN)",
    )


async def test_a_rotated_login_is_written_back(
    db: Database, store: MemorySecretStore, tmp_path: Path
) -> None:
    c = await add(db, store, "codex-1", "openai", chatgpt_login())
    p = prober(db, store, tmp_path, fake_codex(tmp_path, rotate=True))
    assert (await p.refresh(c)).status == "updated"
    assert store.get("codex-1")["tokens"]["refresh_token"] == "rotated"
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT count(*) FROM events WHERE kind = 'account.credential_rotated'"
        ) as cur,
    ):
        assert (await cur.fetchone())[0] == 1


async def test_a_finished_run_queues_its_codex_accounts(
    db: Database, store: MemorySecretStore, tmp_path: Path, clock: FakeClock
) -> None:
    c = await add(db, store, "codex-1", "openai", chatgpt_login())
    await add(db, store, "claude-1", "anthropic", {"api_key": "k"})
    await lease(db, store, ["openai"])
    p = prober(db, store, tmp_path, None)
    claimed_at = clock.now
    async with db.write() as tx:
        await tx.conn.execute(
            "INSERT INTO events (at, actor, subject_type, subject_id, kind, data)"
            " VALUES (?, 'agent:agent-a', 'task', 't1', 'task.claim', '{}')",
            (ts(claimed_at),),
        )
    ended = Event(
        99, clock.now, f"agent:{A}", "task", "t1", "task.complete", {"from": "claimed"}
    )
    assert await p._stale_accounts(ended) == [c]
    async with db.write() as tx:  # a snapshot seen during the run: nothing to do
        await usage.upsert_quota(
            tx, c, [QuotaReport(window_minutes=300, used_percent=1)], "reported", "t"
        )
    assert await p._stale_accounts(ended) == []


async def test_metrics_report_tokens_and_quota(
    db: Database, store: MemorySecretStore, tmp_path: Path
) -> None:
    c = await add(db, store, "codex-1", "openai", chatgpt_login())
    await lease(db, store, ["openai"])
    await report(
        db,
        c,
        input_tokens=7,
        cached_input_tokens=3,
        rate_limits=[{"window_minutes": 300, "used_percent": 25}],
    )
    snap = await ops.snapshot(db, tmp_path / "argus.db", tmp_path)
    text = ops.render_metrics(snap, {}, uptime_s=1, secrets_ok=True)
    samples = {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for f in text_string_to_metric_families(text)
        for s in f.samples
    }
    who = (("account", "codex-1"),)
    assert (
        samples[
            (
                "argus_account_tokens_total",
                (*who, ("kind", "input"), ("provider", "openai")),
            )
        ]
        == 7
    )
    assert (
        samples[
            (
                "argus_account_tokens_total",
                (*who, ("kind", "cached"), ("provider", "openai")),
            )
        ]
        == 3
    )
    assert (
        samples[
            (
                "argus_account_quota_used_percent",
                (*who, ("provider", "openai"), ("window", "300m")),
            )
        ]
        == 25
    )


# -- run keys, measurement state, quota status ---------------------------------------


async def held(
    db: Database,
    store: MemorySecretStore,
    provider: str = "openai",
    kind: str = "subscription",
) -> str:
    """An account of `provider` leased by A."""
    c = await add(db, store, f"{provider}-x", provider, chatgpt_login(), kind)
    await lease(db, store, [provider])
    return c


async def usage_of(db: Database, clock: FakeClock, account_id: str):
    async with db.read() as conn:
        return await accounts.get_account(conn, account_id, clock.now)


async def test_a_repeated_run_key_adds_no_row(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    c = await held(db, store)
    for _ in range(2):
        await report(db, c, input_tokens=10, output_tokens=5, run_key="t1:a:codex:0")
    account = await usage_of(db, clock, c)
    assert (account.usage_total.runs, account.usage_total.input_tokens) == (1, 10)


async def test_distinct_run_keys_are_separate_rows(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    c = await held(db, store)
    for i in range(3):  # multi-turn / repeated runs of one task
        await report(
            db, c, task_id="t1", input_tokens=10, output_tokens=i, run_key=f"t1:a:e:{i}"
        )
    await report(db, c, input_tokens=1, output_tokens=1)  # no key: never deduped
    await report(db, c, input_tokens=1, output_tokens=1)
    total = (await usage_of(db, clock, c)).usage_total
    assert (total.runs, total.input_tokens, total.output_tokens) == (5, 32, 5)


async def test_not_reported_runs_count_but_add_no_tokens(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    c = await held(db, store)
    await report(db, c, input_tokens=10, output_tokens=5)
    await report(db, c, measurement="not_reported", run_key="k")
    account = await usage_of(db, clock, c)
    for u in (account.usage_24h, account.usage_total):
        assert (u.runs, u.unmeasured_runs, u.input_tokens, u.output_tokens) == (
            2,
            1,
            10,
            5,
        )
    async with db.read() as conn:
        s = await usage.series(
            conn,
            clock.now - timedelta(hours=2),
            clock.now + timedelta(hours=1),
            "hour",
            ZoneInfo("UTC"),
            [c],
        )
    t = s[0].totals
    assert (t.runs, t.unmeasured_runs, t.input_tokens) == (2, 1, 10)
    assert sum(b.unmeasured_runs for b in s[0].buckets) == 1


def test_measurement_validation() -> None:
    with pytest.raises(ValidationError):  # nothing measured, nothing admitted
        UsageReport()
    with pytest.raises(ValidationError):
        UsageReport(measurement="not_reported", input_tokens=0)
    with pytest.raises(ValidationError):
        UsageReport(measurement="not_reported", cost_usd=0.1)
    with pytest.raises(ValidationError):
        UsageReport(input_tokens=1, run_key="x" * 201)
    assert UsageReport(measurement="not_reported").input_tokens is None
    assert UsageReport(cost_usd=0.0).measurement == "measured"
    for bad in ({"6h": 5}, {"5h": 0}):
        with pytest.raises(ValidationError):
            AccountCreate(name="n", provider="p", secret_ref="sops:n", token_limits=bad)


async def test_quota_status_reasons(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    codex = await add(db, store, "codex", "openai", chatgpt_login(), "subscription")
    claude = await add(db, store, "claude", "anthropic", {"x": 1}, "subscription")
    cursor = await add(db, store, "cursor", "cursor", {"api_key": "k"}, "subscription")
    apikey = await add(db, store, "oai-key", "openai", {"x": 1}, "api_key")
    expected = {
        codex: "not_yet_probed",
        claude: "unsupported_provider",
        cursor: "unsupported_provider",
        apikey: "api_key_account",
    }

    async def status(account_id: str):
        return (await usage_of(db, clock, account_id)).quota_status

    # Usage rows say nothing about quota: the reason is the same with or without.
    for with_usage in (False, True):
        if with_usage:
            async with db.write() as tx:
                for a in expected:
                    await usage.insert_usage(tx, a, A, None, input_tokens=1)
        for a, reason in expected.items():
            s = await status(a)
            assert (s.state, s.reason, s.windows) == ("unknown", reason, [])
            assert (s.observed_at, s.collector, s.last_probe) == (None, None, None)


async def test_quota_status_known_then_stale(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    c = await add(db, store, "codex", "openai", chatgpt_login(), "subscription")
    async with db.write() as tx:
        await usage.upsert_quota(
            tx,
            c,
            [
                QuotaReport(
                    window_minutes=300,
                    used_percent=5,
                    resets_at=clock.now + timedelta(hours=3),
                )
            ],
            "polled",
            "t",
        )
    observed = clock.now
    s = (await usage_of(db, clock, c)).quota_status
    assert (s.state, s.reason, s.collector, s.observed_at) == (
        "known",
        None,
        "codex_rate_limits",
        observed,
    )
    clock.advance(3601)  # observed over an hour ago
    account = await usage_of(db, clock, c)
    assert account.quota_status.state == "stale"
    assert account.quota_status.reason is None
    assert [q.window for q in account.quota] == ["300m"]  # still listed
    # a window that has already reset is stale even when freshly observed
    async with db.write() as tx:
        await usage.upsert_quota(
            tx,
            c,
            [
                QuotaReport(
                    window_minutes=300,
                    used_percent=5,
                    resets_at=clock.now - timedelta(minutes=1),
                )
            ],
            "reported",
            "t",
        )
    s = (await usage_of(db, clock, c)).quota_status
    assert (s.state, s.collector) == ("stale", "worker_report")


async def test_quota_status_follows_the_last_probe(
    db: Database, store: MemorySecretStore, tmp_path: Path, clock: FakeClock
) -> None:
    c = await add(db, store, "codex", "openai", chatgpt_login(), "subscription")
    # no binary: skipped, and the reason is explicit
    p = prober(db, store, tmp_path, None)
    assert (await p.probe(c)).status == "skipped"
    s = (await usage_of(db, clock, c)).quota_status
    assert (s.state, s.reason) == ("unknown", "probe_skipped")
    assert s.last_probe is not None
    assert (s.last_probe.status, s.last_probe.at) == (
        "skipped",
        clock.now,
    )
    assert "ARGUS_CODEX_BIN" in (s.last_probe.reason or "")
    # a client that dies is a failure, named by type only
    broken = tmp_path / "broken"
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    p = prober(db, store, tmp_path, broken)
    assert (await p.probe(c)).status == "failed"
    s = (await usage_of(db, clock, c)).quota_status
    assert (s.reason, s.last_probe.status) == ("probe_failed", "failed")
    async with (
        db.read() as conn,
        conn.execute("SELECT count(*) FROM account_quota_probe") as cur,
    ):
        assert (await cur.fetchone())[0] == 1  # one row per account
    # the floor does not overwrite what the earlier probe stored
    assert (await p.probe(c)).status == "rate_limited"
    s = (await usage_of(db, clock, c)).quota_status
    assert s.last_probe.status == "failed"
    # a good probe makes it known
    p = prober(db, store, tmp_path, fake_codex(tmp_path))
    assert (await p.probe(c)).status == "updated"
    s = (await usage_of(db, clock, c)).quota_status
    assert (s.state, s.last_probe.status) == ("known", "updated")


async def test_probe_of_other_providers_is_skipped_and_recorded(
    db: Database, store: MemorySecretStore, tmp_path: Path, clock: FakeClock
) -> None:
    c = await add(db, store, "claude", "anthropic", {"x": 1}, "subscription")
    p = prober(db, store, tmp_path, fake_codex(tmp_path))
    result = await p.refresh(c)
    assert (result.status, result.quota) == ("skipped", [])
    s = (await usage_of(db, clock, c)).quota_status
    assert (s.reason, s.last_probe.status) == ("unsupported_provider", "skipped")
    ghost = await p.probe("no-such-account")  # nothing to attach the outcome to
    assert ghost.status == "skipped"


# -- unmeasured reasons and roles ------------------------------------------------------


def test_reason_is_only_for_not_reported_runs() -> None:
    ok = UsageReport(measurement="not_reported", reason="no_usage_in_output")
    assert ok.reason == "no_usage_in_output"
    assert UsageReport(measurement="not_reported").reason is None
    with pytest.raises(ValidationError, match="only allowed with"):
        UsageReport(input_tokens=1, reason="no_usage_in_output")
    for bad in ("Has Caps", "", "x" * 61, "dash-ed"):
        with pytest.raises(ValidationError):
            UsageReport(measurement="not_reported", reason=bad)


async def test_unmeasured_runs_keep_their_reason_and_never_read_as_zero(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    c = await held(db, store)
    await report(db, c, measurement="not_reported", reason="no_usage_in_output")
    await report(db, c, measurement="not_reported", reason="no_usage_in_output")
    await report(db, c, measurement="not_reported", role="executor")
    account = await usage_of(db, clock, c)
    for u in (account.usage_24h, account.usage_total):
        assert (u.runs, u.unmeasured_runs, u.measured_runs) == (3, 3, 0)
        assert u.unmeasured_reasons == {"no_usage_in_output": 2, "unknown": 1}
        assert u.runs_by_role == {"unknown": 2, "executor": 1}
    await report(db, c, input_tokens=4, output_tokens=1, role="engine")
    await report(db, c, input_tokens=0, output_tokens=0, role="engine")  # measured 0
    total = (await usage_of(db, clock, c)).usage_total
    assert (total.runs, total.measured_runs, total.input_tokens) == (5, 2, 4)
    assert total.runs_by_role == {"unknown": 2, "executor": 1, "engine": 2}
    async with db.read() as conn:
        series = await usage.series(
            conn,
            clock.now - timedelta(hours=1),
            clock.now + timedelta(hours=1),
            "day",
            ZoneInfo("UTC"),
            [c],
        )
    t = series[0].totals
    assert (t.runs, t.measured_runs, t.unmeasured_runs) == (5, 2, 3)
    assert t.unmeasured_reasons == {"no_usage_in_output": 2, "unknown": 1}
    assert t.runs_by_role == {"unknown": 2, "executor": 1, "engine": 2}
    assert sum(b.measured_runs for b in series[0].buckets) == 2


async def test_metrics_skip_accounts_with_only_unmeasured_runs(
    db: Database, store: MemorySecretStore, tmp_path: Path
) -> None:
    c = await held(db, store)
    await report(db, c, measurement="not_reported")
    snap = await ops.snapshot(db, tmp_path / "argus.db", tmp_path)
    assert snap.account_tokens == {}
    await report(db, c, input_tokens=2)
    snap = await ops.snapshot(db, tmp_path / "argus.db", tmp_path)
    assert list(snap.account_tokens.values()) == [(2, 0, 0)]


# -- the prober's inventory --------------------------------------------------------


class Clocks:
    """Wall time for the prober, and a sleep that only records."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock, self.slept = clock, []
        self.mono = 0.0

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def timed_prober(db, store, tmp_path, codex, fake: Clocks) -> QuotaProber:
    settings = Settings(data_dir=tmp_path, codex_bin=str(codex) if codex else None)
    return QuotaProber(
        db, store, settings, clock=lambda: fake.mono, now=fake.clock, sleep=fake.sleep
    )


async def observe(db: Database, account_id: str) -> None:
    async with db.write() as tx:
        await usage.upsert_quota(
            tx,
            account_id,
            [QuotaReport(window_minutes=300, used_percent=1)],
            "polled",
            "t",
        )


async def test_sweep_queues_only_stale_or_missing_codex_chatgpt_accounts(
    db: Database, store: MemorySecretStore, tmp_path: Path, clock: FakeClock
) -> None:
    fresh = await add(db, store, "fresh", "openai", chatgpt_login(), "subscription")
    stale = await add(db, store, "stale", "openai", chatgpt_login(), "subscription")
    never = await add(db, store, "never", "openai", chatgpt_login(), "subscription")
    off = await add(db, store, "off", "openai", chatgpt_login(), "subscription")
    await add(db, store, "key", "openai", {"x": 1}, "api_key")
    await add(db, store, "claude", "anthropic", {"x": 1}, "subscription")
    await observe(db, stale)
    clock.advance(31 * 60)
    await observe(db, fresh)
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE accounts SET state = 'disabled', disabled_reason = 't'"
            " WHERE id = ?",
            (off,),
        )
    fake = Clocks(clock)
    p = timed_prober(db, store, tmp_path, None, fake)
    assert sorted(await p.sweep()) == sorted([stale, never])
    assert await p.sweep() == []  # already queued: no duplicates


async def drain(p: QuotaProber) -> list[str]:
    out = []
    while not p._queue.empty():
        account_id = p._queue.get_nowait()
        p._queued.discard(account_id)
        out.append(account_id)
    return out


async def test_sweep_is_capped_and_backs_off_after_a_recent_probe(
    db: Database, store: MemorySecretStore, tmp_path: Path, clock: FakeClock
) -> None:
    ids = [
        await add(db, store, f"c{i:02d}", "openai", chatgpt_login(), "subscription")
        for i in range(13)
    ]
    fake = Clocks(clock)
    p = timed_prober(db, store, tmp_path, None, fake)
    assert len(await p.sweep()) == 10  # SWEEP_CAP: no storm
    queued = await drain(p)
    assert set(queued) <= set(ids)
    # the rest come with the next sweep
    assert len(await p.sweep()) == 10
    await drain(p)
    # a probe (here: skipped, no binary) defers its account for the next sweeps
    target = ids[0]
    assert (await p.probe(target)).status == "skipped"
    fake.mono += 301
    clock.advance(10 * 60)
    assert target not in await p.sweep()
    await drain(p)
    assert target in await p.sweep(backoff=False)  # the start-up inventory
    await drain(p)
    clock.advance(31 * 60)  # older than the refresh age: due again
    assert target in await p.sweep()


async def test_run_loop_sweeps_after_a_delay_probes_one_at_a_time_with_a_gap(
    db: Database, store: MemorySecretStore, tmp_path: Path, clock: FakeClock
) -> None:
    a = await add(db, store, "a", "openai", chatgpt_login(), "subscription")
    b = await add(db, store, "b", "openai", chatgpt_login(), "subscription")
    fake = Clocks(clock)
    gate = asyncio.Event()
    sleeps = fake.slept

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if seconds == quota_probe.SWEEP_INTERVAL_S:
            gate.set()
            await asyncio.Event().wait()  # park the sweeper after the first sweep

    p = QuotaProber(
        db,
        store,
        Settings(data_dir=tmp_path, codex_bin=str(fake_codex(tmp_path))),
        clock=lambda: fake.mono,
        now=clock,
        sleep=sleep,
    )
    task = asyncio.create_task(p.run())
    await asyncio.wait_for(gate.wait(), 10)
    for _ in range(200):  # let both probes finish
        await asyncio.sleep(0.05)
        async with (
            db.read() as conn,
            conn.execute("SELECT count(*) FROM account_quota_probe") as cur,
        ):
            if (await cur.fetchone())[0] == 2:
                break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sleeps[0] == quota_probe.STARTUP_DELAY_S
    assert sleeps.count(quota_probe.PROBE_GAP_S) >= 2  # a gap after every probe
    for account_id in (a, b):
        s = (await usage_of(db, clock, account_id)).quota_status
        assert (s.state, s.last_probe.status) == ("known", "updated")


async def test_migration_0038_applies_to_a_database_at_0037(
    tmp_path: Path,
) -> None:
    migrations = load_migrations()
    assert [m.version for m in migrations][36:38] == [37, 38]
    path = tmp_path / "old.db"
    old = await Database.open(path, migrations=migrations[:37])
    async with old.write() as tx:
        await tx.conn.execute(
            "INSERT INTO accounts (id, name, provider, kind, secret_ref, state,"
            " created_at, updated_at) VALUES ('a', 'a', 'p', 'api_key', 'sops:a',"
            " 'available', 'x', 'x')"
        )
        await tx.conn.execute(
            "INSERT INTO account_usage (account_id, input_tokens, at)"
            " VALUES ('a', 1, 'x')"
        )
    await old.close()
    db = await Database.open(path)
    try:
        async with (
            db.read() as c,
            c.execute("SELECT unmeasured_reason FROM account_usage") as cur,
        ):
            assert [tuple(r) for r in await cur.fetchall()] == [(None,)]
        async with db.write() as tx:
            await usage.record_probe(tx, "a", "failed", "boom")
            await usage.record_probe(tx, "a", "skipped", None)
            await usage.record_probe(tx, "ghost", "failed", None)  # no account: no row
        async with db.read() as c:
            p = await usage.last_probe(c, "a")
            assert p is not None and p.status == "skipped"
            assert await usage.last_probe(c, "ghost") is None
    finally:
        await db.close()


async def test_token_limits_estimate_rolling_windows(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    c = await held(db, store)
    async with db.write() as tx:
        await accounts.update_account(
            tx,
            c,
            AccountUpdate(token_limits={"5h": 1000, "7d": 10_000}),
            "admin:test",
        )
    await report(db, c, input_tokens=100, output_tokens=200, cached_input_tokens=999)
    await report(db, c, measurement="not_reported")
    clock.advance(6 * 3600)  # first row is now outside 5h, inside 7d
    await report(db, c, input_tokens=50, output_tokens=50)
    acc = await usage_of(db, clock, c)
    s = acc.quota_status
    assert (s.state, s.reason) == ("known", None)
    w = {x.period: x for x in s.windows}
    assert (w["5h"].used, w["5h"].remaining, w["5h"].used_percent) == (100, 900, 10.0)
    assert (w["7d"].used, w["7d"].remaining, w["7d"].used_percent) == (400, 9600, 4.0)
    assert {x.source for x in s.windows} == {"estimated"}
    assert acc.token_limits == {"5h": 1000, "7d": 10_000}
    async with db.write() as tx:  # null clears; usage exists -> unknown reason
        await accounts.update_account(
            tx, c, AccountUpdate(token_limits=None), "admin:test"
        )
    assert (await usage_of(db, clock, c)).quota_status.state == "unknown"


async def test_totals_equal_the_sum_of_rows(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    c = await held(db, store)
    for i in range(4):
        await report(db, c, input_tokens=i + 1, output_tokens=2 * i, cost_usd=0.5)
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT sum(input_tokens), sum(output_tokens), sum(cost_usd), count(*)"
            " FROM account_usage WHERE account_id = ?",
            (c,),
        ) as cur,
    ):
        i, o, cost, n = tuple(await cur.fetchone())
    acc = await usage_of(db, clock, c)
    for u in (acc.usage_24h, acc.usage_total):
        assert (u.input_tokens, u.output_tokens, u.cost_usd, u.runs) == (i, o, cost, n)
