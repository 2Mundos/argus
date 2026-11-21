"""Google (Antigravity) accounts: import validation, executor-only lease, catalogue
and admin-only replacement (docs/specs/google-accounts.spec.md). Fake tokens only."""

import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from argus_api.accounts.google_refresh import GoogleTokenRefresher
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from fastapi.testclient import TestClient

ADMIN_TOKEN = "t" * 40
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
FAKE_KEY = "AIzaSyFAKE_API_KEY_TEST_000000000000"
FAKE_ACCESS = "ya29.FAKE_OAUTH_ACCESS_TEST_0000000000"
FAKE_REFRESH = "1//FAKE_OAUTH_REFRESH_TEST_000000000"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        app.state.secrets = MemorySecretStore()
        yield c


def _future_expiry() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


def _create(
    client: TestClient,
    credential: dict | None,
    *,
    name: str = "google-main",
    **extra,
):
    body: dict = {"name": name, "provider": "google", **extra}
    if credential is not None:
        body["credential"] = credential
    return client.post("/api/accounts", headers=ADMIN, json=body)


def _register_worker(client: TestClient) -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={}, headers=ADMIN).json()
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    return {"Authorization": f"Bearer {reg.json()['token']}"}


def test_import_api_key_and_oauth_derive_kind_and_never_echo(
    client: TestClient,
) -> None:
    r_key = _create(
        client, {"api_key": FAKE_KEY}, name="google-key", kind="subscription"
    )
    assert r_key.status_code == 201, r_key.text
    key_acc = r_key.json()
    assert key_acc["provider"] == "google"
    assert key_acc["kind"] == "api_key"
    assert key_acc["quota_status"]["state"] == "unknown"
    assert key_acc["quota_status"]["reason"] == "no_provider_source"
    assert FAKE_KEY not in r_key.text

    r_oauth = _create(
        client,
        {
            "token": {
                "access_token": FAKE_ACCESS,
                "token_type": "Bearer",
                "refresh_token": FAKE_REFRESH,
                "expiry": _future_expiry(),
                "expires_in": 3600,
            },
            "auth_method": "consumer",
        },
        name="google-oauth",
        kind="api_key",
    )
    assert r_oauth.status_code == 201, r_oauth.text
    oauth_acc = r_oauth.json()
    assert oauth_acc["provider"] == "google"
    assert oauth_acc["kind"] == "subscription"
    assert FAKE_ACCESS not in r_oauth.text
    assert FAKE_REFRESH not in r_oauth.text


def test_import_rejects_bad_shapes_and_secret_ref_only(client: TestClient) -> None:
    bad = _create(client, {"api_key": FAKE_ACCESS}, name="bad-key")
    assert bad.status_code == 422
    assert FAKE_ACCESS not in bad.text

    no_cred = _create(client, None, name="ref-only", secret_ref="sops:existing")
    assert no_cred.status_code == 422


def test_google_leases_only_for_executor_role_and_updates_catalogue(
    client: TestClient,
) -> None:
    created = _create(client, {"api_key": FAKE_KEY}, name="google-exec")
    assert created.status_code == 201
    acc_id = created.json()["id"]

    # Engine catalogue reflects the registered google account on antigravity.
    cat = client.get("/api/engines", headers=ADMIN).json()
    agy = next(e for e in cat["engines"] if e["id"] == "antigravity")
    assert agy["axes"]["executor"] == "agy"
    assert agy["axes"]["account_provider"] == "google"
    assert agy["readiness"]["authenticated"]["ok"] is True
    assert agy["accounts"]["available"] == 1

    # Seed a project and task claim so leasing can bind to an active claim.
    proj = client.put("/api/projects/p", headers=ADMIN, json={"name": "Demo"})
    assert proj.status_code == 200, proj.text
    created_task = client.post(
        "/api/tasks",
        headers=ADMIN,
        json={"project": "p", "title": "google lease test", "spec": "s"},
    )
    assert created_task.status_code == 201, created_task.text
    task = created_task.json()
    if task["state"] == "awaiting_approval":
        approve = client.post(f"/api/tasks/{task['id']}/approve", headers=ADMIN)
        assert approve.status_code == 200, approve.text
    worker = _register_worker(client)
    claimed_res = client.post("/api/tasks/claim", headers=worker, json={})
    assert claimed_res.status_code == 200, claimed_res.text
    claimed = claimed_res.json()
    claim = {"task_id": task["id"], "attempt": claimed["attempt"]}

    # Engine role never leases google.
    eng = client.post(
        "/api/accounts/lease",
        headers=worker,
        json={"role": "engine", "runtime_providers": ["google"], **claim},
    )
    assert eng.status_code == 409

    # Executor role leases google.
    exe = client.post(
        "/api/accounts/lease",
        headers=worker,
        json={"role": "executor", "runtime_providers": ["google"], **claim},
    )
    assert exe.status_code == 200, exe.text
    lease = exe.json()
    assert lease["account_id"] == acc_id
    assert lease["provider"] == "google"
    assert lease["role"] == "executor"

    # Worker cannot replace google credential (admin only).
    rot = client.put(
        f"/api/accounts/{acc_id}/credential",
        headers=worker,
        json={"credential": {"api_key": "AIzaSyOTHER_000000000000"}, **claim},
    )
    assert rot.status_code == 403

    # Admin can replace API key with OAuth credential and kind updates.
    admin_rot = client.put(
        f"/api/accounts/{acc_id}/credential",
        headers=ADMIN,
        json={
            "credential": {
                "token": {
                    "access_token": FAKE_ACCESS,
                    "expiry": _future_expiry(),
                },
                "auth_method": "consumer",
            }
        },
    )
    assert admin_rot.status_code == 204
    updated = client.get("/api/accounts", headers=ADMIN).json()
    row = next(a for a in updated if a["id"] == acc_id)
    assert row["kind"] == "subscription"

    # Auth rejection writes account.auth_rejected with fixed reason.
    rej = client.post(
        f"/api/accounts/{acc_id}/auth-rejected",
        headers=worker,
        json={"role": "executor", "source": "structured", **claim},
    )
    assert rej.status_code == 204


def test_custom_google_endpoint_account_is_not_an_agy_executor_account(
    client: TestClient,
) -> None:
    """A Google AI Studio key saved as `custom` (label `google`) serves only the
    OpenAI-compatible path; the agy executor needs provider `google`."""
    custom = client.post(
        "/api/accounts",
        headers=ADMIN,
        json={
            "name": "studio-as-custom",
            "provider": "custom",
            "credential": {"api_key": FAKE_KEY},
            "config": {
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                "label": "google",
            },
        },
    )
    assert custom.status_code == 201, custom.text
    assert FAKE_KEY not in custom.text

    def agy_available() -> int:
        cat = client.get("/api/engines", headers=ADMIN).json()
        agy = next(e for e in cat["engines"] if e["id"] == "antigravity")
        return agy["accounts"]["available"]

    assert agy_available() == 0

    native = _create(client, {"api_key": FAKE_KEY}, name="studio-native")
    assert native.status_code == 201, native.text
    assert native.json()["provider"] == "google"
    assert native.json()["kind"] == "api_key"
    assert FAKE_KEY not in native.text
    assert agy_available() == 1


def _seed_claim(client: TestClient) -> tuple[dict[str, str], dict]:
    proj = client.put("/api/projects/p-refresh", headers=ADMIN, json={"name": "Demo"})
    assert proj.status_code == 200, proj.text
    created_task = client.post(
        "/api/tasks",
        headers=ADMIN,
        json={"project": "p-refresh", "title": "google refresh test", "spec": "s"},
    )
    assert created_task.status_code == 201, created_task.text
    task = created_task.json()
    if task["state"] == "awaiting_approval":
        approve = client.post(f"/api/tasks/{task['id']}/approve", headers=ADMIN)
        assert approve.status_code == 200, approve.text
    worker = _register_worker(client)
    claimed_res = client.post("/api/tasks/claim", headers=worker, json={})
    assert claimed_res.status_code == 200, claimed_res.text
    claimed = claimed_res.json()
    return worker, {"task_id": task["id"], "attempt": claimed["attempt"]}


def test_lease_refreshes_expiring_google_oauth_via_http_and_persists_to_sops(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARGUS_GOOGLE_OAUTH_CLIENT_ID", "client-id-test")
    monkeypatch.setenv("ARGUS_GOOGLE_OAUTH_CLIENT_SECRET", "client-secret-test")
    calls: list[dict[str, str]] = []

    async def fake_post(url: str, form: dict[str, str]) -> tuple[int, dict]:
        assert url == "https://oauth2.googleapis.com/token"
        calls.append(dict(form))
        return (
            200,
            {
                "access_token": "ya29.REFRESHED_ACCESS_TOKEN_00000000",
                "expires_in": 3600,
                "token_type": "Bearer",
                "refresh_token": "1//ROTATED_REFRESH_TOKEN_000000",
            },
        )

    app.state.google_refresher = GoogleTokenRefresher(post_form=fake_post)
    created = _create(
        client,
        {
            "token": {
                "access_token": FAKE_ACCESS,
                "token_type": "Bearer",
                "refresh_token": FAKE_REFRESH,
                "expiry": "2020-01-01T00:00:00Z",
                "expires_in": 3600,
            },
            "auth_method": "consumer",
            "id_token_email": "operator@example.test",
        },
        name="google-expiring",
    )
    assert created.status_code == 201, created.text
    acc_id = created.json()["id"]
    worker, claim = _seed_claim(client)

    exe = client.post(
        "/api/accounts/lease",
        headers=worker,
        json={"role": "executor", "runtime_providers": ["google"], **claim},
    )
    assert exe.status_code == 200, exe.text
    lease = exe.json()
    assert lease["credential"]["token"]["access_token"] == (
        "ya29.REFRESHED_ACCESS_TOKEN_00000000"
    )
    assert lease["credential"]["auth_method"] == "consumer"
    assert lease["credential"]["id_token_email"] == "operator@example.test"
    assert len(calls) == 1
    assert calls[0]["grant_type"] == "refresh_token"
    assert calls[0]["refresh_token"] == FAKE_REFRESH
    assert calls[0]["client_id"] == "client-id-test"

    stored = app.state.secrets.get(f"account-{acc_id}")
    assert stored is not None
    assert stored["token"]["access_token"] == "ya29.REFRESHED_ACCESS_TOKEN_00000000"
    assert stored["token"]["refresh_token"] == "1//ROTATED_REFRESH_TOKEN_000000"

    # A second lease reuses the fresh stored token without calling Google again.
    exe2 = client.post(
        "/api/accounts/lease",
        headers=worker,
        json={"role": "executor", "runtime_providers": ["google"], **claim},
    )
    assert exe2.status_code == 200
    assert len(calls) == 1


def test_lease_refreshes_expiring_google_oauth_via_agy_cli_with_isolated_env(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARGUS_GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("ARGUS_GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/should/be/scrubbed")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/should/be/scrubbed")
    monkeypatch.setenv("GEMINI_API_KEY", "should-be-scrubbed")

    probe_log = tmp_path / "agy-env.json"
    fake_agy = tmp_path / "fake-agy"
    future_iso = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    fake_agy.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "home = pathlib.Path(os.environ['HOME'])\n"
        "tok = home / '.gemini' / 'antigravity-cli' / 'antigravity-oauth-token'\n"
        "doc = json.loads(tok.read_text())\n"
        f"pathlib.Path({str(probe_log)!r}).write_text(json.dumps({{\n"
        "  'dbus': os.environ.get('DBUS_SESSION_BUS_ADDRESS'),\n"
        "  'xdg_runtime': os.environ.get('XDG_RUNTIME_DIR'),\n"
        "  'gemini_key': os.environ.get('GEMINI_API_KEY'),\n"
        "  'no_browser': os.environ.get('NO_BROWSER'),\n"
        "  'staged_expiry': doc['token']['expiry'],\n"
        "  'home': str(home),\n"
        "}))\n"
        "doc['token']['access_token'] = 'ya29.CLI_REFRESHED_ACCESS_00000000'\n"
        f"doc['token']['expiry'] = {future_iso!r}\n"
        "tok.write_text(json.dumps(doc))\n"
        "sys.exit(1)\n"
    )
    fake_agy.chmod(0o755)
    app.state.google_refresher = GoogleTokenRefresher(agy_bin=str(fake_agy))

    near_expiry = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()
    created = _create(
        client,
        {
            "token": {
                "access_token": FAKE_ACCESS,
                "token_type": "Bearer",
                "refresh_token": FAKE_REFRESH,
                "expiry": near_expiry,
            },
            "auth_method": "consumer",
            "id_token_email": "operator@example.test",
        },
        name="google-cli-refresh",
    )
    assert created.status_code == 201, created.text
    worker, claim = _seed_claim(client)

    exe = client.post(
        "/api/accounts/lease",
        headers=worker,
        json={"role": "executor", "runtime_providers": ["google"], **claim},
    )
    assert exe.status_code == 200, exe.text
    assert exe.json()["credential"]["token"]["access_token"] == (
        "ya29.CLI_REFRESHED_ACCESS_00000000"
    )

    observed = json.loads(probe_log.read_text())
    assert observed["dbus"] is None
    assert observed["xdg_runtime"] is None
    assert observed["gemini_key"] is None
    assert observed["no_browser"] == "1"
    assert observed["staged_expiry"] == "1970-01-01T00:00:00Z"
    assert not Path(observed["home"]).exists()


def test_permanent_invalid_grant_blocks_revoked_google_oauth_account(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARGUS_GOOGLE_OAUTH_CLIENT_ID", "client-id-test")
    monkeypatch.setenv("ARGUS_GOOGLE_OAUTH_CLIENT_SECRET", "client-secret-test")

    async def fake_post(url: str, form: dict[str, str]) -> tuple[int, dict]:
        return (400, {"error": "invalid_grant", "error_description": "Token revoked"})

    app.state.google_refresher = GoogleTokenRefresher(post_form=fake_post)
    created = _create(
        client,
        {
            "token": {
                "access_token": FAKE_ACCESS,
                "refresh_token": FAKE_REFRESH,
                "expiry": "2020-01-01T00:00:00Z",
            },
            "auth_method": "consumer",
        },
        name="google-revoked",
    )
    assert created.status_code == 201
    worker, claim = _seed_claim(client)

    # Refresh fails permanently -> records account.auth_rejected -> lease skips it (409).
    exe = client.post(
        "/api/accounts/lease",
        headers=worker,
        json={"role": "executor", "runtime_providers": ["google"], **claim},
    )
    assert exe.status_code == 409
    assert FAKE_REFRESH not in exe.text
    assert FAKE_ACCESS not in exe.text


def test_lease_downloads_pinned_agy_client_when_host_has_no_agy(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In containerized API installs (`deploy/Dockerfile.api`), `agy` is not on PATH;
    `GoogleTokenRefresher` downloads the pinned client via `accounts/clients.py`."""
    from argus_api.accounts import clients, google_refresh

    monkeypatch.delenv("ARGUS_GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("ARGUS_GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(google_refresh, "resolve_agy_bin", lambda explicit=None: None)

    future_iso = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    fake_agy = tmp_path / "downloaded-agy"
    fake_agy.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "tok = pathlib.Path(os.environ['HOME']) / '.gemini' / 'antigravity-cli' / 'antigravity-oauth-token'\n"
        "doc = json.loads(tok.read_text())\n"
        "doc['token']['access_token'] = 'ya29.DOWNLOADED_AGY_REFRESH_00000'\n"
        f"doc['token']['expiry'] = {future_iso!r}\n"
        "tok.write_text(json.dumps(doc))\n"
        "sys.exit(1)\n"
    )
    fake_agy.chmod(0o755)
    ensured: list[str] = []

    async def fake_ensure(settings, name: str) -> Path:
        ensured.append(name)
        return fake_agy

    monkeypatch.setattr(clients, "ensure", fake_ensure)
    app.state.google_refresher = GoogleTokenRefresher(settings=app.state.settings)

    created = _create(
        client,
        {
            "token": {
                "access_token": FAKE_ACCESS,
                "refresh_token": FAKE_REFRESH,
                "expiry": "2020-01-01T00:00:00Z",
            },
            "auth_method": "consumer",
        },
        name="google-docker-refresh",
    )
    assert created.status_code == 201, created.text
    worker, claim = _seed_claim(client)

    exe = client.post(
        "/api/accounts/lease",
        headers=worker,
        json={"role": "executor", "runtime_providers": ["google"], **claim},
    )
    assert exe.status_code == 200, exe.text
    assert ensured == ["agy"]
    assert exe.json()["credential"]["token"]["access_token"] == (
        "ya29.DOWNLOADED_AGY_REFRESH_00000"
    )


def test_execution_chain_claim_refreshes_expired_google_oauth_executor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An execution-chain claim reserves and delivers the `google` executor lease
    atomically inside `POST /api/tasks/claim` (sending engine-only `runtime_providers`);
    the control plane must refresh an expiring Google OAuth token before delivery."""
    monkeypatch.setenv("ARGUS_GOOGLE_OAUTH_CLIENT_ID", "client-id-test")
    monkeypatch.setenv("ARGUS_GOOGLE_OAUTH_CLIENT_SECRET", "client-secret-test")
    calls: list[dict[str, str]] = []

    async def fake_post(url: str, form: dict[str, str]) -> tuple[int, dict]:
        calls.append(dict(form))
        return (
            200,
            {
                "access_token": "ya29.CHAIN_REFRESHED_ACCESS_00000",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

    app.state.google_refresher = GoogleTokenRefresher(post_form=fake_post)

    eng = client.post(
        "/api/accounts",
        headers=ADMIN,
        json={
            "name": "claude-harness",
            "provider": "anthropic",
            "credential": {"api_key": "sk-ant-api03-fixture-secret"},
        },
    )
    assert eng.status_code == 201, eng.text
    eng_id = eng.json()["id"]

    goog = _create(
        client,
        {
            "token": {
                "access_token": FAKE_ACCESS,
                "token_type": "Bearer",
                "refresh_token": FAKE_REFRESH,
                "expiry": "2020-01-01T00:00:00Z",
                "expires_in": 3600,
            },
            "auth_method": "consumer",
            "id_token_email": "operator@example.test",
        },
        name="google-chain-exec",
    )
    assert goog.status_code == 201, goog.text
    goog_id = goog.json()["id"]

    proj = client.put("/api/projects/p-chain", headers=ADMIN, json={"name": "Chain"})
    assert proj.status_code == 200, proj.text
    chain = {
        "version": 1,
        "difficulties": {
            "standard": [
                {
                    "id": "claude-agy",
                    "mode": "paired",
                    "runtime": "nanoclaw",
                    "harness": {
                        "account_id": eng_id,
                        "provider": "anthropic",
                        "adapter": "claude",
                        "model": {"source": "explicit", "id": "claude-sonnet-4-6"},
                    },
                    "executor": {
                        "account_id": goog_id,
                        "provider": "google",
                        "adapter": "agy",
                        "model": {"source": "explicit", "id": "gemini-4-neon"},
                    },
                }
            ],
        },
    }
    put_chain = client.put(
        "/api/projects/p-chain/settings/execution.chains",
        headers=ADMIN,
        json={"value": chain},
    )
    assert put_chain.status_code == 200, put_chain.text

    created_task = client.post(
        "/api/tasks",
        headers=ADMIN,
        json={"project": "p-chain", "title": "chain google refresh", "spec": "s"},
    )
    assert created_task.status_code == 201, created_task.text
    task = created_task.json()
    if task["state"] == "awaiting_approval":
        approve = client.post(f"/api/tasks/{task['id']}/approve", headers=ADMIN)
        assert approve.status_code == 200, approve.text

    worker = _register_worker(client)
    claimed = client.post(
        "/api/tasks/claim",
        headers=worker,
        json={
            "capabilities": [
                "execution-chains-v1",
                "nanoclaw:harness:claude:anthropic",
                "nanoclaw:executor:agy:google",
            ],
            "runtime_providers": ["anthropic", "openai", "local", "custom"],
        },
    )
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["executor_lease"] is not None
    assert body["executor_lease"]["account_id"] == goog_id
    assert body["executor_lease"]["credential"]["token"]["access_token"] == (
        "ya29.CHAIN_REFRESHED_ACCESS_00000"
    )
    assert len(calls) == 1


# --- mid-lease credential refresh (POST /accounts/{id}/credential/refresh) ---

REFRESHED_1 = "ya29.MIDLEASE_REFRESHED_ONE_000000000"
REFRESHED_2 = "ya29.MIDLEASE_REFRESHED_TWO_000000000"


def _leased_google(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, post_form
) -> tuple[dict[str, str], dict, str, str]:
    """A claimed task whose agent holds an executor lease on a fresh google account."""
    monkeypatch.setenv("ARGUS_GOOGLE_OAUTH_CLIENT_ID", "client-id-test")
    monkeypatch.setenv("ARGUS_GOOGLE_OAUTH_CLIENT_SECRET", "client-secret-test")
    app.state.google_refresher = GoogleTokenRefresher(post_form=post_form)
    created = _create(
        client,
        {
            "token": {
                "access_token": FAKE_ACCESS,
                "token_type": "Bearer",
                "refresh_token": FAKE_REFRESH,
                "expiry": _future_expiry(),
                "expires_in": 3600,
            },
            "auth_method": "consumer",
        },
        name="google-midlease",
    )
    assert created.status_code == 201, created.text
    acc_id = created.json()["id"]
    worker, claim = _seed_claim(client)
    first = client.post(
        "/api/accounts/lease",
        headers=worker,
        json={"role": "executor", "runtime_providers": ["google"], **claim},
    )
    assert first.status_code == 200, first.text
    assert first.json()["credential"]["token"]["access_token"] == FAKE_ACCESS
    return worker, claim, acc_id, first.json()["lease_id"]


def _refresh(client, worker, claim, acc_id, **extra):
    return client.post(
        f"/api/accounts/{acc_id}/credential/refresh",
        headers=worker,
        json={"role": "executor", **claim, **extra},
    )


async def _async_expire(acc_id: str) -> None:
    doc = app.state.secrets.get(f"account-{acc_id}")
    doc["token"]["expiry"] = "2020-01-01T00:00:00Z"
    await app.state.secrets.put(f"account-{acc_id}", doc)


def test_midlease_refresh_after_first_invocation_expires_then_is_stable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First invocation succeeds on the delivered token; once it expires the
    executor gets a refreshed one, repeated calls reuse it (no extra Google call)."""
    calls: list[dict[str, str]] = []

    async def fake_post(url: str, form: dict[str, str]) -> tuple[int, dict]:
        calls.append(dict(form))
        return 200, {"access_token": REFRESHED_1, "expires_in": 3600}

    worker, claim, acc_id, lease_id = _leased_google(client, monkeypatch, fake_post)

    # Not near expiry: current credential returned, provider not contacted.
    ok = _refresh(client, worker, claim, acc_id)
    assert ok.status_code == 200, ok.text
    assert ok.json()["credential"]["token"]["access_token"] == FAKE_ACCESS
    assert calls == []

    # Token expires mid-lease.
    import asyncio

    asyncio.run(_async_expire(acc_id))
    res = _refresh(client, worker, claim, acc_id)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["lease_id"] == lease_id
    assert body["role"] == "executor"
    assert body["credential"]["token"]["access_token"] == REFRESHED_1
    assert len(calls) == 1

    for _ in range(3):
        again = _refresh(client, worker, claim, acc_id)
        assert again.status_code == 200
        assert again.json()["credential"]["token"]["access_token"] == REFRESHED_1
    assert len(calls) == 1


def test_midlease_forced_refresh_is_bounded_by_cooldown(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 on a token that looks fresh may force one refresh; hammering it does
    not reach the provider again inside the cooldown."""
    calls: list[int] = []

    async def fake_post(url: str, form: dict[str, str]) -> tuple[int, dict]:
        calls.append(1)
        return 200, {"access_token": REFRESHED_2, "expires_in": 3600}

    worker, claim, acc_id, _ = _leased_google(client, monkeypatch, fake_post)
    first = _refresh(client, worker, claim, acc_id, force=True)
    assert first.status_code == 200, first.text
    assert first.json()["credential"]["token"]["access_token"] == REFRESHED_2
    for _ in range(5):
        again = _refresh(client, worker, claim, acc_id, force=True)
        assert again.status_code == 503
        assert again.json()["detail"]["code"] == "credential_refresh_unavailable"
    assert len(calls) == 1


def test_midlease_transient_failure_fails_closed_without_retry_storm(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    async def fake_post(url: str, form: dict[str, str]) -> tuple[int, dict]:
        calls.append(1)
        return 503, {"error": "temporarily_unavailable"}

    worker, claim, acc_id, _ = _leased_google(client, monkeypatch, fake_post)
    import asyncio

    asyncio.run(_async_expire(acc_id))
    for _ in range(4):
        res = _refresh(client, worker, claim, acc_id)
        assert res.status_code == 503
        assert res.json()["detail"]["code"] == "credential_refresh_unavailable"
        assert FAKE_ACCESS not in res.text and FAKE_REFRESH not in res.text
    assert len(calls) == 1  # the stale token is never returned in its place


def test_midlease_permanent_rejection_is_non_retryable_and_blocks_account(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    async def fake_post(url: str, form: dict[str, str]) -> tuple[int, dict]:
        calls.append(1)
        return 400, {"error": "invalid_grant"}

    worker, claim, acc_id, _ = _leased_google(client, monkeypatch, fake_post)
    import asyncio

    asyncio.run(_async_expire(acc_id))
    res = _refresh(client, worker, claim, acc_id)
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "credential_refresh_rejected"
    assert FAKE_ACCESS not in res.text and FAKE_REFRESH not in res.text
    accounts = {a["id"]: a for a in client.get("/api/accounts", headers=ADMIN).json()}
    assert accounts[acc_id]["lease_block"] == "auth_rejected"
    again = _refresh(client, worker, claim, acc_id)
    assert again.status_code == 409
    assert len(calls) == 1


def test_midlease_refresh_is_fenced_by_claim_and_lease(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_post(url: str, form: dict[str, str]) -> tuple[int, dict]:
        raise AssertionError("fenced calls must not reach the provider")

    worker, claim, acc_id, _ = _leased_google(client, monkeypatch, fake_post)
    import asyncio

    asyncio.run(_async_expire(acc_id))

    # Another agent, a stale attempt and the wrong role are all refused.
    other = _register_worker(client)
    assert _refresh(client, other, claim, acc_id).status_code == 409
    stale = {**claim, "attempt": claim["attempt"] + 1}
    assert _refresh(client, worker, stale, acc_id).status_code == 409
    assert _refresh(client, worker, claim, acc_id, role="engine").status_code == 409

    # Revocation: once the lease is released no credential is returned.
    assert client.post("/api/accounts/release", headers=worker).status_code == 204
    res = _refresh(client, worker, claim, acc_id)
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "not_leased"
    assert FAKE_REFRESH not in res.text
