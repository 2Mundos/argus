"""Quota from Claude's OAuth usage and OpenRouter's key, and the diagnostics for
accounts with no readable source (docs/specs/account-usage.spec.md 3.3).

The provider bodies are sanitized stand-ins in the documented shapes, served
through an in-process transport. None is a capture of a live response."""

import base64
import json
import logging
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2 as httpx
import pytest
from api_support import FakeClock
from argus_api.accounts import quota_probe, quota_readers
from argus_api.accounts import service as accounts
from argus_api.accounts.quota_probe import QuotaProber
from argus_api.config import Settings
from argus_api.db import Database
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from argus_core.accounts import AccountCreate
from fastapi.testclient import TestClient

TOKEN = "sk-ant-oat01-FAKEFAKEFAKEFAKEFAKE"
OR_KEY = "sk-or-v1-FAKEKEYFAKEKEY"
ADMIN_TOKEN = "q" * 40
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
OR_CONFIG = {
    "base_url": "https://openrouter.ai/api/v1",
    "label": "openrouter",
    "engine": {"model": "anthropic/claude-sonnet-4"},
}

# Flat shape: `utilization` is the percent USED. One window is zero, one is full,
# one is absent (null): none of them may become another.
CLAUDE_FLAT = {
    "five_hour": {"utilization": 0.0, "resets_at": "2026-10-23T15:00:00.000000+00:00"},
    "seven_day": {"utilization": 100, "resets_at": "2026-10-28T09:00:00+00:00"},
    "seven_day_opus": None,
    "seven_day_sonnet": {"utilization": 42.5, "resets_at": None},
}
CLAUDE_LIMITS = {
    "five_hour": None,
    "limits": [
        {"kind": "session", "percent": 12, "resets_at": "2026-10-23T15:00:00Z"},
        {"kind": "weekly_all", "percent": 64, "resets_at": "2026-10-28T09:00:00Z"},
        {
            "kind": "weekly_scoped",
            "percent": 30,
            "resets_at": "2026-10-28T09:00:00Z",
            "scope": {"model": {"display_name": "Opus 4"}},
        },
    ],
}
OR_CAPPED = {
    "data": {
        "label": "sk-or-v1-abc...xyz",
        "limit": 20.0,
        "limit_remaining": 0,
        "limit_reset": "monthly",
        "usage": 20.0,
        "is_free_tier": False,
    }
}
OR_UNCAPPED = {
    "data": {
        "limit": None,
        "limit_remaining": None,
        "usage": 3.25,
        "is_free_tier": False,
    }
}


class Provider:
    """Answers GETs by script and records the requests."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.body: object = CLAUDE_FLAT
        self.headers: dict[str, str] = {}
        self.error: Exception | None = None
        self.routes: dict[str, tuple[int, object]] = {}  # path -> (status, body)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error:
            raise self.error
        if request.url.path in self.routes:
            status, body = self.routes[request.url.path]
            if isinstance(body, bytes):
                return httpx.Response(status, content=body)
            return httpx.Response(status, json=body)
        return httpx.Response(self.status, json=self.body, headers=self.headers)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


@pytest.fixture
def provider() -> Provider:
    return Provider()


@pytest.fixture
def store() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def db(tmp_path: Path, clock: FakeClock):
    database = await Database.open(tmp_path / "argus.db", readers=2, clock=clock)
    yield database
    await database.close()


def prober(db, store, tmp_path, provider, clock) -> QuotaProber:
    return QuotaProber(
        db,
        store,
        Settings(data_dir=tmp_path),
        now=lambda: clock.now,
        transport=provider.transport,
    )


async def add(db, store, name, provider, credential, kind, config=None) -> str:
    await store.put(name, credential)
    async with db.write() as tx:
        account = await accounts.create_account(
            tx,
            name,
            AccountCreate(
                name=name,
                provider=provider,
                kind=kind,
                secret_ref=f"sops:{name}",
                config=config,
            ),
            f"sops:{name}",
            "admin:test",
            store,
        )
    return account.id


def cursor_login(expires_in=timedelta(hours=2)) -> dict:
    def part(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    exp = int((datetime.now(UTC) + expires_in).timestamp())
    return {
        "accessToken": f"{part({'alg': 'none'})}.{part({'exp': exp})}.sig",
        "refreshToken": "cursor-refresh-FAKE",
    }


DASH = "/aiserver.v1.DashboardService/"
# Connect JSON in the shapes the dashboard answers with (sanitized; int64 as strings).
CURSOR_EMPTY_BALANCE: dict = {}
CURSOR_STATUS_NO_GRANT = {
    "usageLimitPolicyStatus": {
        "canConfigureSpendLimit": True,
        "canAdjustOnDemand": True,
        "recommendedOnDemandLimitCents": "5000",
        "onDemandMinCents": "0",
        "onDemandMaxCents": "100000",
        "creditRequestButtonHidden": False,
    }
}
CURSOR_PLAN = {
    "planInfo": {
        "planName": "Pro",
        "price": "$20/mo",
        "billingCycleEnd": "1793000000000",
        "planOwner": "individual",
        "includedUsagePeriod": "monthly",
    },
    "nextUpgrade": {},
}
CURSOR_STATUS_GRANTS = {
    "resetAtMs": "1793000000000",
    "activeGrants": [
        {"remainingCents": "0", "totalCents": "2000", "expiresAtMs": "1793000000000"},
        {"remainingCents": "350", "totalCents": "500", "expiresAtMs": "1794000000000"},
    ],
}


def cursor_routes(provider: Provider, balance=None, status=None, plan=None) -> None:
    provider.routes = {
        DASH + "GetCreditGrantsBalance": (
            200,
            CURSOR_EMPTY_BALANCE if balance is None else balance,
        ),
        DASH + "GetUsageLimitStatusAndActiveGrants": (
            200,
            CURSOR_STATUS_NO_GRANT if status is None else status,
        ),
        DASH + "GetPlanInfo": (200, CURSOR_PLAN if plan is None else plan),
    }


def full_login(expires_in=timedelta(hours=2)) -> dict:
    when = datetime.now(UTC) + expires_in
    return {
        "claudeAiOauth": {
            "accessToken": TOKEN,
            "refreshToken": "sk-ant-ort01-FAKE",
            "expiresAt": int(when.timestamp() * 1000),
            "scopes": ["user:inference", "user:profile"],
        }
    }


async def account_of(db, clock, account_id):
    async with db.read() as conn:
        return await accounts.get_account(conn, account_id, clock.now)


# -- parsing --------------------------------------------------------------------------


def test_claude_flat_windows_read_percent_used_and_keep_zero_and_full() -> None:
    got = {r.window: r for r in quota_readers.claude_windows(CLAUDE_FLAT)}
    assert set(got) == {"300m", "10080m", "weekly_sonnet"}  # the null window is absent
    assert got["300m"].used_percent == 0.0  # zero stays zero
    assert got["10080m"].used_percent == 100.0  # full stays full: no inversion
    assert got["300m"].resets_at == datetime(2026, 10, 23, 15, tzinfo=UTC)
    assert got["weekly_sonnet"].resets_at is None


def test_claude_structured_limits_are_read_when_the_flat_keys_are_null() -> None:
    got = {
        r.window: r.used_percent for r in quota_readers.claude_windows(CLAUDE_LIMITS)
    }
    assert got == {"session": 12.0, "10080m": 64.0, "weekly_opus_4": 30.0}


@pytest.mark.parametrize("body", [None, [], {}, {"five_hour": {"utilization": "x"}}])
def test_claude_unreadable_bodies_give_no_windows(body) -> None:
    assert quota_readers.claude_windows(body) == []


def test_openrouter_credit_keeps_zero_remaining_and_uncapped_keys_apart() -> None:
    capped = quota_readers.openrouter_credit(OR_CAPPED)
    assert (capped.limit, capped.remaining, capped.used) == (20.0, 0.0, 20.0)
    assert capped.limit_reset == "monthly"
    free = quota_readers.openrouter_credit(OR_UNCAPPED)
    assert (free.limit, free.remaining, free.used) == (None, None, 3.25)
    broken = {"data": {"limit": 5, "usage": 1}}  # capped, remaining not stated
    with pytest.raises(quota_readers.ReadError) as err:
        quota_readers.openrouter_credit(broken)
    assert err.value.code == "bad_response"


# -- Claude ---------------------------------------------------------------------------


async def test_claude_login_is_read_with_its_own_token_and_stored(
    db, store, tmp_path, provider, clock, caplog
) -> None:
    c = await add(db, store, "cl", "anthropic", full_login(), "subscription")
    other = await add(db, store, "cl2", "anthropic", full_login(), "subscription")
    p = prober(db, store, tmp_path, provider, clock)
    with caplog.at_level(logging.DEBUG):
        result = await p.refresh(c)
    assert result.status == "updated" and result.code is None
    (req,) = provider.requests
    assert str(req.url) == "https://api.anthropic.com/api/oauth/usage"
    assert req.method == "GET"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert req.headers["anthropic-beta"] == "oauth-2025-04-20"
    assert TOKEN not in caplog.text
    assert [(q.window, q.used_percent, q.source) for q in result.quota] == [
        ("300m", 0.0, "polled"),
        ("10080m", 100.0, "polled"),
        ("weekly_sonnet", 42.5, "polled"),
    ]
    status = result.quota_status
    assert (status.state, status.collector) == ("known", "claude_oauth_usage")
    assert (await account_of(db, clock, other)).quota == []  # nothing is shared


@pytest.mark.parametrize(
    ("status", "headers", "code"),
    [
        (401, {}, "credential_rejected"),
        (403, {}, "oauth_scope_missing"),
        (429, {"retry-after": "30"}, "provider_rate_limited"),
        (503, {}, "fetch_failed"),
    ],
)
async def test_claude_failures_are_distinct_and_keep_the_last_good_figures(
    db, store, tmp_path, provider, clock, status, headers, code
) -> None:
    c = await add(db, store, "cl", "anthropic", full_login(), "subscription")
    p = prober(db, store, tmp_path, provider, clock)
    assert (await p.refresh(c)).status == "updated"
    clock.advance(10 * 60)  # past the per-account floor
    p._last.clear()
    provider.status, provider.headers, provider.body = status, headers, {}
    result = await p.refresh(c)
    assert (result.status, result.code) == ("failed", code)
    assert len(result.quota) == 3  # last known good figures are still there
    account = await account_of(db, clock, c)
    assert account.quota_status.state == "known"  # still fresh, not zeroed
    assert account.quota_status.last_probe.code == code
    clock.advance(2 * 3600)  # and stale once they age, never erased
    assert (await account_of(db, clock, c)).quota_status.state == "stale"


async def test_a_network_error_is_a_fetch_failure_without_its_text(
    db, store, tmp_path, provider, clock
) -> None:
    c = await add(db, store, "cl", "anthropic", full_login(), "subscription")
    provider.error = httpx.ConnectError(f"boom {TOKEN}")
    result = await prober(db, store, tmp_path, provider, clock).refresh(c)
    assert (result.status, result.code) == ("failed", "fetch_failed")
    assert TOKEN not in (result.reason or "")
    assert result.quota_status.reason == "fetch_failed"


async def test_setup_token_scope_gap_is_named(
    db, store, tmp_path, provider, clock
) -> None:
    c = await add(db, store, "cl", "anthropic", {"oauth_token": TOKEN}, "subscription")
    provider.status = 403
    result = await prober(db, store, tmp_path, provider, clock).refresh(c)
    assert result.code == "oauth_scope_missing"
    assert "setup-token" in (result.reason or "")
    assert result.quota_status.reason == "oauth_scope_missing"


async def test_expired_login_and_api_key_make_no_request(
    db, store, tmp_path, provider, clock
) -> None:
    expired = await add(db, store, "old", "anthropic", full_login(), "subscription")
    doc = full_login()
    doc["claudeAiOauth"]["expiresAt"] = int(
        (datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1000
    )
    await store.put("old", doc)
    key = await add(
        db, store, "key", "anthropic", {"api_key": "sk-ant-api03-FAKE"}, "api_key"
    )
    p = prober(db, store, tmp_path, provider, clock)
    assert (await p.probe(expired)).code == "credential_expired"
    assert (await p.probe(key)).code == "api_key_account"
    assert provider.requests == []
    assert (await account_of(db, clock, key)).quota_status.reason == "api_key_account"


async def test_a_missing_credential_is_reported_not_raised(
    db, store, tmp_path, provider, clock
) -> None:
    c = await add(db, store, "gone", "anthropic", full_login(), "subscription")
    del store._data["gone"]
    result = await prober(db, store, tmp_path, provider, clock).refresh(c)
    assert (result.status, result.code) == ("skipped", "credential_missing")
    assert provider.requests == []


# -- OpenRouter -----------------------------------------------------------------------


async def test_openrouter_key_credit_is_stored_as_credit_not_a_window(
    db, store, tmp_path, provider, clock
) -> None:
    c = await add(db, store, "or", "custom", {"api_key": OR_KEY}, "api_key", OR_CONFIG)
    provider.body = OR_CAPPED
    result = await prober(db, store, tmp_path, provider, clock).refresh(c)
    (req,) = provider.requests
    assert str(req.url) == "https://openrouter.ai/api/v1/key"
    assert req.headers["authorization"] == f"Bearer {OR_KEY}"
    assert result.quota == []  # no invented token window
    credit = result.credit
    assert (credit.unit, credit.limit, credit.remaining, credit.used) == (
        "usd",
        20.0,
        0.0,  # zero remaining is a measurement
        20.0,
    )
    assert credit.collector == "openrouter_key"
    account = await account_of(db, clock, c)
    assert (account.quota_status.state, account.credit.remaining) == ("known", 0.0)
    assert account.usage_total.cost_usd == 0  # reported spend is a different thing


async def test_uncapped_key_has_a_null_remaining_not_zero(
    db, store, tmp_path, provider, clock
) -> None:
    c = await add(db, store, "or", "custom", {"api_key": OR_KEY}, "api_key", OR_CONFIG)
    provider.body = OR_UNCAPPED
    result = await prober(db, store, tmp_path, provider, clock).refresh(c)
    assert (result.credit.limit, result.credit.remaining, result.credit.used) == (
        None,
        None,
        3.25,
    )


async def test_openrouter_failure_keeps_the_last_credit(
    db, store, tmp_path, provider, clock
) -> None:
    c = await add(db, store, "or", "custom", {"api_key": OR_KEY}, "api_key", OR_CONFIG)
    p = prober(db, store, tmp_path, provider, clock)
    provider.body = OR_CAPPED
    await p.refresh(c)
    p._last.clear()
    provider.status = 401
    result = await p.refresh(c)
    assert (result.status, result.code) == ("failed", "credential_rejected")
    assert result.credit.limit == 20.0


# -- accounts with no readable source -------------------------------------------------


async def test_accounts_without_a_source_say_which_kind_of_absence(
    db, store, tmp_path, provider, clock
) -> None:
    cursor = await add(db, store, "cu", "cursor", {"api_key": "k"}, "api_key")
    other_config = OR_CONFIG | {"label": "acme", "base_url": "https://llm.acme.test/v1"}
    acme = await add(
        db, store, "ac", "custom", {"api_key": "k"}, "api_key", other_config
    )
    p = prober(db, store, tmp_path, provider, clock)
    expected = {
        cursor: "admin_key_required",
        acme: "no_provider_source",
    }
    for account_id, code in expected.items():
        before = await account_of(db, clock, account_id)
        assert (before.quota_status.state, before.quota_status.reason) == (
            "unknown",
            code,
        )
        assert (await p.probe(account_id)).code == code
        assert (await account_of(db, clock, account_id)).quota == []
    assert provider.requests == []


# -- triggers -------------------------------------------------------------------------


async def test_sweep_queues_claude_and_openrouter_but_not_unreadable_accounts(
    db, store, tmp_path, provider, clock
) -> None:
    cl = await add(db, store, "cl", "anthropic", full_login(), "subscription")
    orr = await add(
        db, store, "or", "custom", {"api_key": OR_KEY}, "api_key", OR_CONFIG
    )
    await add(db, store, "cu", "cursor", {"api_key": "k"}, "api_key")
    login = await add(db, store, "cul", "cursor", cursor_login(), "subscription")
    await add(db, store, "key", "anthropic", {"api_key": "sk-ant-api03-F"}, "api_key")
    p = prober(db, store, tmp_path, provider, clock)
    assert sorted(await p.sweep()) == sorted([cl, orr, login])
    assert sorted(await p.sweep()) == []  # already queued: no duplicates


async def test_exhaustion_report_queues_a_claude_account_but_not_an_api_key(
    db, store, tmp_path, provider, clock
) -> None:
    from argus_api.events import Event

    cl = await add(db, store, "cl", "anthropic", full_login(), "subscription")
    key = await add(
        db, store, "key", "anthropic", {"api_key": "sk-ant-api03-F"}, "api_key"
    )
    p = prober(db, store, tmp_path, provider, clock)
    for account_id, expected in ((cl, [cl]), (key, [])):
        event = Event(
            1, clock.now, "agent:a", "account", account_id, "account.exhausted", {}
        )
        assert await p._stale_accounts(event) == expected


# -- REST and MCP ---------------------------------------------------------------------


@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: Provider
) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        secrets = MemorySecretStore()
        app.state.secrets = secrets
        app.state.quota._secrets = secrets
        app.state.quota._transport = provider.transport
        app.state.quota.request = lambda _id: None  # no background read races a test
        yield c


def create(client, name, provider_name, credential, **extra):
    body = {"name": name, "provider": provider_name, "credential": credential, **extra}
    res = client.post("/api/accounts", json=body, headers=ADMIN)
    assert res.status_code == 201, res.text
    return res.json()


def test_creating_an_account_queues_its_first_reading(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued: list[str] = []
    monkeypatch.setattr(app.state.quota, "request", queued.append)
    made = create(client, "or", "custom", {"api_key": OR_KEY}, config=OR_CONFIG)
    assert queued == [made["id"]]
    unreadable = create(client, "cu", "cursor", {"api_key": "k"})
    assert unreadable["quota_status"]["reason"] == "admin_key_required"
    local = create(client, "lo", "local", {"base_url": "http://127.0.0.1:8080"})
    assert local["quota_status"]["reason"] == "local_no_quota"


def test_refresh_over_rest_returns_the_outcome_and_the_stored_figures(
    client: TestClient, provider: Provider
) -> None:
    made = create(client, "or", "custom", {"api_key": OR_KEY}, config=OR_CONFIG)
    provider.body = OR_CAPPED
    res = client.post(f"/api/accounts/{made['id']}/quota/refresh", headers=ADMIN)
    assert res.status_code == 200
    body = res.json()
    assert (body["status"], body["code"]) == ("updated", None)
    assert body["credit"]["remaining"] == 0.0
    assert body["quota_status"]["state"] == "known"
    got = client.get(f"/api/accounts/{made['id']}", headers=ADMIN).json()
    assert got["credit"]["limit"] == 20.0 and got["credit"]["unit"] == "usd"
    assert OR_KEY not in json.dumps(got) + json.dumps(body)
    again = client.post(f"/api/accounts/{made['id']}/quota/refresh", headers=ADMIN)
    assert again.json()["status"] == "rate_limited"
    assert again.json()["credit"]["remaining"] == 0.0  # still the stored one


def test_refresh_of_an_unreadable_account_is_skipped_with_its_reason(
    client: TestClient,
) -> None:
    made = create(client, "cu", "cursor", {"api_key": "k"})
    assert made["kind"] == "api_key"
    body = client.post(
        f"/api/accounts/{made['id']}/quota/refresh", headers=ADMIN
    ).json()
    assert (body["status"], body["code"]) == ("skipped", "admin_key_required")
    assert body["quota"] == [] and body["credit"] is None
    assert body["quota_status"]["state"] == "unknown"


def test_refresh_needs_a_manager(client: TestClient) -> None:
    made = create(client, "cu", "cursor", {"api_key": "k"})
    url = f"/api/accounts/{made['id']}/quota/refresh"
    assert client.post(url).status_code == 401


def _mcp(client: TestClient, name: str, **arguments):
    res = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={
            **ADMIN,
            "Accept": "application/json, text/event-stream",
            "Host": "127.0.0.1",
        },
    )
    assert res.status_code == 200, res.text
    return json.loads(res.json()["result"]["content"][0]["text"])


def test_mcp_lists_quota_and_refreshes_like_rest(
    client: TestClient, provider: Provider
) -> None:
    made = create(client, "or", "custom", {"api_key": OR_KEY}, config=OR_CONFIG)
    provider.body = OR_CAPPED
    refreshed = _mcp(client, "refresh_account_quota", account_id=made["id"])
    assert (refreshed["status"], refreshed["credit"]["remaining"]) == ("updated", 0.0)
    listed = _mcp(client, "list_accounts")["accounts"]
    (row,) = [a for a in listed if a["id"] == made["id"]]
    assert row["credit"]["limit"] == 20.0
    assert row["quota_status"]["state"] == "known"


def test_module_constants_bound_the_loop() -> None:
    # bounded: one 15 s read per account, at most 10 queued per sweep, 5 min floor
    assert quota_probe.PROBE_TIMEOUT_S == 15.0
    assert quota_probe.SWEEP_CAP == 10 and quota_probe.MIN_INTERVAL_S == 300.0
    assert quota_readers.TIMEOUT_S == 15.0


# -- Claude: what the live response carries ------------------------------------------

CLAUDE_LIVE = {
    "five_hour": {
        "utilization": "37.5",  # a numeric string
        "resets_at": "2026-10-23T15:00:00Z",
        "limit_dollars": None,
        "used_dollars": None,
        "remaining_dollars": None,
        "locked_reason": None,
    },
    "seven_day": {
        "utilization": 0,
        "resets_at": "2026-10-28T09:00:00Z",
        "locked_reason": "usage_limit_reached",  # locked is not 0% used
    },
    "limits": [
        {
            "kind": "session",
            "percent": 37.5,
            "resets_at": "2026-10-23T15:00:00Z",
            "scope": None,
            "severity": "warning",
            "group": "plan",
            "is_active": True,
        },
        {
            "kind": "weekly_all",
            "percent": 0,
            "resets_at": None,
            "severity": "ok",
            "is_active": True,
        },
        {
            "kind": "weekly_scoped",
            "percent": 80,
            "resets_at": None,
            "severity": "ok",
            "scope": {"model": {"display_name": "Opus 4"}},
            "is_active": False,
        },
    ],
    "extra_usage": {"is_enabled": False},
    "spend": {"used": 1},
    "seven_day_breakdown": {},
}


def test_claude_numeric_strings_and_locked_windows_are_not_misread() -> None:
    got = quota_readers.claude_usage(CLAUDE_LIVE)
    assert [(r.window, r.used_percent) for r in got.windows] == [("300m", 37.5)]
    # the weekly window is locked: absent from the windows, named, never 0% used
    assert got.locked == ["10080m"]


def test_claude_limits_skip_inactive_entries_and_duplicates_of_flat_keys() -> None:
    body = {
        "five_hour": {"utilization": "10"},
        "limits": [
            {
                "kind": "session",
                "percent": 99,
                "is_active": True,
            },  # same window as flat
            {"kind": "weekly_all", "percent": "25", "is_active": True},
            {"kind": "weekly_scoped", "percent": 80, "is_active": False},
        ],
    }
    got = {r.window: r.used_percent for r in quota_readers.claude_usage(body).windows}
    assert got == {"300m": 10.0, "10080m": 25.0}


async def test_a_fully_locked_response_is_a_named_outcome_not_zero(
    db, store, tmp_path, provider, clock
) -> None:
    c = await add(db, store, "cl", "anthropic", full_login(), "subscription")
    provider.body = {"five_hour": {"utilization": 0, "locked_reason": "x"}}
    result = await prober(db, store, tmp_path, provider, clock).refresh(c)
    assert (result.status, result.code, result.quota) == ("failed", "window_locked", [])


async def test_a_locked_window_beside_a_readable_one_is_noted(
    db, store, tmp_path, provider, clock
) -> None:
    c = await add(db, store, "cl", "anthropic", full_login(), "subscription")
    provider.body = CLAUDE_LIVE
    result = await prober(db, store, tmp_path, provider, clock).refresh(c)
    assert result.status == "updated"
    assert [q.window for q in result.quota] == ["300m"]
    account = await account_of(db, clock, c)
    assert "10080m" in (account.quota_status.last_probe.reason or "")


# -- Cursor (an imported login) -------------------------------------------------------


def test_cursor_empty_and_string_cents_never_become_zero() -> None:
    none = quota_readers.cursor_reading({}, CURSOR_STATUS_NO_GRANT, {})
    assert none.credit is None  # {} and limits-only: nothing numeric to show
    grants = quota_readers.cursor_reading({}, CURSOR_STATUS_GRANTS, CURSOR_PLAN)
    c = grants.credit
    assert (c.limit, c.remaining, c.used) == (25.0, 3.5, 21.5)  # "2000"+"500" cents
    assert c.resets_at == datetime.fromtimestamp(1793000000, UTC)
    balance = quota_readers.cursor_reading(
        {"totalCents": "1000", "usedCents": "1000"}, {}, {}
    )
    assert (balance.credit.limit, balance.credit.remaining) == (10.0, 0.0)  # real zero
    partial = quota_readers.cursor_reading(
        {}, {"activeGrants": [{"remainingCents": "5"}]}, {}
    )
    assert partial.credit is None  # a grant without its total is not summed
    junk = quota_readers.cursor_reading({"totalCents": "abc", "usedCents": -1}, {}, {})
    assert junk.credit is None


async def test_cursor_login_reads_credit_with_the_three_dashboard_calls(
    db, store, tmp_path, provider, clock, caplog
) -> None:
    c = await add(db, store, "cul", "cursor", cursor_login(), "subscription")
    cursor_routes(provider, status=CURSOR_STATUS_GRANTS)
    with caplog.at_level(logging.DEBUG):
        result = await prober(db, store, tmp_path, provider, clock).refresh(c)
    assert (result.status, result.code) == ("updated", None)
    assert [r.url.path for r in provider.requests] == [
        DASH + n for n in quota_readers.CURSOR_CALLS
    ]
    for r in provider.requests:
        assert r.method == "POST" and r.url.host == "api2.cursor.sh"
        assert r.headers["connect-protocol-version"] == "1"
        assert r.headers["authorization"].startswith("Bearer ")
    token = cursor_login()["accessToken"].split(".")[0]
    assert token not in caplog.text
    assert (result.credit.remaining, result.credit.collector) == (
        3.5,
        "cursor_dashboard",
    )
    assert result.quota == []  # no invented window
    assert result.quota_status.state == "known"


async def test_cursor_login_without_numbers_is_named_not_zero(
    db, store, tmp_path, provider, clock
) -> None:
    c = await add(db, store, "cul", "cursor", cursor_login(), "subscription")
    cursor_routes(provider)  # {} balance, limits-only status, a plan
    result = await prober(db, store, tmp_path, provider, clock).refresh(c)
    assert (result.status, result.code, result.credit) == (
        "skipped",
        "no_usage_window_for_login",
        None,
    )
    assert "monthly" in result.reason and "cycle ends" in result.reason
    assert result.quota_status.reason == "no_usage_window_for_login"
    assert result.quota_status.state == "unknown"


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (401, {}, "credential_rejected"),
        (403, {}, "credential_rejected"),
        (429, {}, "provider_rate_limited"),
        (502, {}, "fetch_failed"),
        (200, b"<html>not json</html>", "bad_response"),
        (200, [], "bad_response"),
    ],
)
async def test_cursor_failures_are_distinct(
    db, store, tmp_path, provider, clock, status, body, code
) -> None:
    c = await add(db, store, "cul", "cursor", cursor_login(), "subscription")
    cursor_routes(provider)
    provider.routes[DASH + "GetPlanInfo"] = (status, body)
    result = await prober(db, store, tmp_path, provider, clock).refresh(c)
    assert (result.status, result.code) == ("failed", code)


async def test_cursor_api_key_and_expired_login_make_no_request(
    db, store, tmp_path, provider, clock
) -> None:
    key = await add(db, store, "ck", "cursor", {"api_key": "k"}, "api_key")
    old = await add(
        db, store, "co", "cursor", cursor_login(timedelta(hours=-1)), "subscription"
    )
    p = prober(db, store, tmp_path, provider, clock)
    got = await p.probe(key)
    assert (got.status, got.code) == ("skipped", "admin_key_required")
    assert "login" in got.reason and "Admin API" in got.reason  # the other source
    assert (await p.probe(old)).code in ("credential_invalid", "credential_expired")
    assert provider.requests == []
    status = (await account_of(db, clock, key)).quota_status
    assert status.reason == "admin_key_required"
