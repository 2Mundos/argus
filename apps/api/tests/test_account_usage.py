"""Account usage: reports, quota snapshots, aggregation, and the Codex quota probe
(docs/specs/account-usage.spec.md)."""

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
from argus_api.accounts import service as accounts
from argus_api.accounts import usage
from argus_api.accounts.quota_probe import QuotaProber
from argus_api.config import Settings
from argus_api.db import Database, ts
from argus_api.errors import ValidationFailedError
from argus_api.events import Event
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from argus_core.accounts import AccountCreate, QuotaReport, UsageReport
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from pydantic import ValidationError

A = "agent-a"


@pytest.fixture
def store() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture(autouse=True)
async def agent(db: Database) -> None:
    await insert_agent(db, A)


async def add(
    db: Database, store: MemorySecretStore, name: str, provider: str, credential: dict
) -> str:
    await store.put(name, credential)
    async with db.write() as tx:
        account = await accounts.create_account(
            tx,
            name,
            AccountCreate(name=name, provider=provider, secret_ref=f"sops:{name}"),
            f"sops:{name}",
            "admin:test",
            store,
        )
    return account.id


async def report(db: Database, account_id: str, **kw) -> None:
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
    async with db.write() as tx:
        await accounts.lease(tx, A, ["openai"], store)
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
    async with db.write() as tx:
        await accounts.lease(tx, A, ["cursor"], store, "executor")
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
    async with db.write() as tx:
        await accounts.lease(tx, A, ["openai"], store)
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
    async with db.write() as tx:
        await accounts.lease(tx, A, ["openai"], store)
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
    async with db.write() as tx:
        await accounts.lease(tx, A, ["openai"], store)
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
