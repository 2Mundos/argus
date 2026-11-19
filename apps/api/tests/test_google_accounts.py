"""Google (Antigravity) accounts: import validation, executor-only lease, catalogue
and admin-only replacement (docs/specs/google-accounts.spec.md). Fake tokens only."""

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
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
