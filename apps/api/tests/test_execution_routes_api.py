"""`execution.chains` over REST: typed roundtrip, inheritance and admission.

Disposable database and fake credentials; no provider is contacted.
"""

import os
from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from fastapi.testclient import TestClient
from test_launch import ADMIN

ADMIN_TOKEN = ADMIN["Authorization"].split()[1]
KEY = "execution.chains"
CAPS = [
    "execution-chains-v1",
    "nanoclaw:harness:claude:anthropic",
    "nanoclaw:harness:claude:custom",
]


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    for name in list(os.environ):
        if name.startswith(("ARGUS_", "DOCKER_HOST")) and not name.startswith(
            "ARGUS_TEST_"
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ARGUS_DISK_GUARD_ENABLED", "false")
    with TestClient(app) as c:
        app.state.secrets = MemorySecretStore()
        yield c


SECRET = "sk-ant-api03-fixture-secret"


def account(client: TestClient, provider: str, **extra) -> str:
    body = {
        "name": f"{provider}-fixture",
        "provider": provider,
        "credential": {"api_key": SECRET},
        **extra,
    }
    res = client.post("/api/accounts", json=body, headers=ADMIN)
    assert res.status_code == 201, res.text
    return res.json()["id"]


def custom_config() -> dict:
    return {
        "base_url": "https://openrouter.ai/api/v1",
        "label": "openrouter",
        "engine": {"model": "vendor/harness"},
        "executor": {"model": "vendor/code"},
    }


def direct(account_id: str, provider: str, id: str = "direct") -> dict:
    return {
        "id": id,
        "mode": "single",
        "runtime": "nanoclaw",
        "harness": {
            "account_id": account_id,
            "provider": provider,
            "adapter": "claude",
            "model": {"source": "explicit", "id": "vendor/harness"}
            if provider != "custom"
            else {"source": "account"},
        },
    }


def put(client: TestClient, value: dict, project: str | None = None):
    url = (
        f"/api/projects/{project}/settings/{KEY}" if project else f"/api/settings/{KEY}"
    )
    return client.put(url, json={"value": value}, headers=ADMIN)


def setting(client: TestClient, project: str | None = None) -> dict:
    url = f"/api/projects/{project}/settings" if project else "/api/settings"
    body = client.get(url, headers=ADMIN).json()
    return next(s for s in body["settings"] if s["key"] == KEY)


def project(client: TestClient, slug: str = "demo") -> None:
    assert client.put(
        f"/api/projects/{slug}",
        json={"name": "Demo", "description": "d"},
        headers=ADMIN,
    ).status_code in (200, 201)


def test_registry_and_capability_catalogue(client: TestClient) -> None:
    registry = client.get("/api/settings/registry", headers=ADMIN).json()
    definition = next(d for d in registry if d["key"] == KEY)
    assert set(definition["scopes"]) == {"global", "project"}
    assert definition["default"] == {"version": 1, "difficulties": {}}
    assert "difficulties" in definition["schema"]["properties"]

    catalogue = client.get("/api/engines", headers=ADMIN).json()[
        "execution_capabilities"
    ]
    ids = {c["id"] for c in catalogue}
    assert "nanoclaw:harness:claude:anthropic" in ids
    assert "nanoclaw:executor:opencode:custom" in ids
    # An executor-only CLI is never advertised as a harness.
    assert not any(c["id"].startswith("nanoclaw:harness:opencode") for c in catalogue)


def test_global_roundtrip_is_typed_and_persists(client: TestClient) -> None:
    cloud = account(client, "anthropic")
    custom = account(client, "custom", config=custom_config())

    before = setting(client)
    assert before["source"] == "default"
    assert before["value"] == {"version": 1, "difficulties": {}}
    assert before["warnings"] == []

    value = {"version": 1, "difficulties": {"hard": [direct(cloud, "anthropic")]}}
    res = put(client, value)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["source"] == "global"
    assert body["value"] == value and body["scoped_value"] == value
    assert body["difficulty_sources"]["hard"] == "global"
    assert body["difficulty_sources"]["trivial"] == "legacy"
    assert len(body["difficulty_revisions"]["hard"]) == 64
    assert (
        body["difficulty_revisions"]["trivial"] != body["difficulty_revisions"]["hard"]
    )

    reread = setting(client)
    assert reread["value"] == value
    assert reread["revision"] == body["revision"]
    # No credential content ever rides in settings responses.
    assert SECRET not in res.text

    # Every difficulty accepts the identical contract.
    levels = {"hard": [direct(cloud, "anthropic")]}
    for level in ("trivial", "simple", "standard"):
        levels[level] = [direct(custom, "custom", id=f"{level}-custom")]
        assert put(client, {"version": 1, "difficulties": levels}).status_code == 200
    assert set(setting(client)["value"]["difficulties"]) == {
        "trivial",
        "simple",
        "standard",
        "hard",
    }


def test_project_replaces_whole_lists_and_resets_per_level(client: TestClient) -> None:
    project(client)
    cloud = account(client, "anthropic")
    custom = account(client, "custom", config=custom_config())
    global_value = {
        "version": 1,
        "difficulties": {
            "hard": [direct(cloud, "anthropic")],
            "simple": [direct(cloud, "anthropic", id="simple-global")],
        },
    }
    assert put(client, global_value).status_code == 200

    inherited = setting(client, "demo")
    assert inherited["source"] == "global"
    assert inherited["scoped_value"] == {"version": 1, "difficulties": {}}
    assert inherited["difficulty_sources"]["hard"] == "global"

    own = {
        "version": 1,
        "difficulties": {"hard": [direct(custom, "custom", id="project-only")]},
    }
    res = put(client, own, "demo")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["scoped_value"] == own
    assert [a["id"] for a in body["value"]["difficulties"]["hard"]] == ["project-only"]
    # An untouched level still inherits the global list.
    assert [a["id"] for a in body["value"]["difficulties"]["simple"]] == [
        "simple-global"
    ]
    assert body["difficulty_sources"] == {
        "trivial": "legacy",
        "simple": "global",
        "standard": "legacy",
        "hard": "project",
    }

    # A whole-list replacement never merges alternatives.
    replaced = {
        "version": 1,
        "difficulties": {"hard": [direct(cloud, "anthropic", id="replacement")]},
    }
    assert put(client, replaced, "demo").status_code == 200
    assert [
        a["id"] for a in setting(client, "demo")["value"]["difficulties"]["hard"]
    ] == ["replacement"]

    # Dropping the level from the scoped map restores inheritance, not an empty list.
    assert (
        put(
            client,
            {"version": 1, "difficulties": {"simple": [direct(cloud, "anthropic")]}},
            "demo",
        ).status_code
        == 200
    )
    after = setting(client, "demo")
    assert after["difficulty_sources"]["hard"] == "global"
    assert [a["id"] for a in after["value"]["difficulties"]["hard"]] == ["direct"]

    reset = client.delete(f"/api/projects/demo/settings/{KEY}", headers=ADMIN)
    assert reset.status_code == 200
    assert reset.json()["difficulty_sources"]["simple"] == "global"
    assert reset.json()["scoped_value"] == {"version": 1, "difficulties": {}}


def test_invalid_documents_are_rejected(client: TestClient) -> None:
    cloud = account(client, "anthropic")
    custom = account(client, "custom", config=custom_config())
    local_only = direct(cloud, "anthropic")
    cases = [
        {"version": 2, "difficulties": {"hard": [local_only]}},
        {"version": 1, "difficulties": {"hard": [local_only]}, "extra": True},
        {
            "version": 1,
            "difficulties": {
                "hard": [
                    {**local_only, "harness": {**local_only["harness"], "note": "x"}}
                ]
            },
        },
        {"version": 1, "difficulties": {"hard": []}},
        {"version": 1, "difficulties": {"impossible": [local_only]}},
        {
            "version": 1,
            "difficulties": {
                "hard": [local_only, {**local_only, "id": "direct"}],
            },
        },
        {
            "version": 1,
            "difficulties": {
                "hard": [
                    {
                        "id": "with-executor",
                        "mode": "single",
                        "runtime": "nanoclaw",
                        "harness": direct(cloud, "anthropic")["harness"],
                        "executor": direct(custom, "custom")["harness"],
                    }
                ]
            },
        },
        {
            "version": 1,
            "difficulties": {
                "hard": [
                    {
                        "id": "missing-executor",
                        "mode": "paired",
                        "runtime": "nanoclaw",
                        "harness": direct(cloud, "anthropic")["harness"],
                    }
                ]
            },
        },
        # Unknown account, provider mismatch and an unsupported model source.
        {"version": 1, "difficulties": {"hard": [direct("missing", "anthropic")]}},
        {"version": 1, "difficulties": {"hard": [direct(cloud, "custom")]}},
        {
            "version": 1,
            "difficulties": {
                "hard": [
                    {
                        **direct(cloud, "anthropic"),
                        "harness": {
                            **direct(cloud, "anthropic")["harness"],
                            "model": {"source": "account"},
                        },
                    }
                ]
            },
        },
        # A local harness may not run the claude executor.
        {
            "version": 1,
            "difficulties": {
                "hard": [
                    {
                        "id": "bad-local-pair",
                        "mode": "paired",
                        "runtime": "nanoclaw",
                        "harness": {
                            "account_id": custom,
                            "provider": "custom",
                            "adapter": "claude",
                            "model": {"source": "account"},
                        },
                        "executor": {
                            "account_id": custom,
                            "provider": "custom",
                            "adapter": "claude",
                            "model": {"source": "explicit", "id": "vendor/code"},
                        },
                    }
                ]
            },
        },
    ]
    for value in cases:
        res = put(client, value)
        assert res.status_code == 422, (value, res.text)
    # Nothing was stored by the refused writes.
    assert setting(client)["source"] == "default"


def test_disabled_and_deleted_accounts_are_warnings_not_silent_repairs(
    client: TestClient,
) -> None:
    cloud = account(client, "anthropic")
    value = {"version": 1, "difficulties": {"hard": [direct(cloud, "anthropic")]}}
    assert put(client, value).status_code == 200
    assert setting(client)["warnings"] == []

    assert (
        client.patch(
            f"/api/accounts/{cloud}", json={"enabled": False}, headers=ADMIN
        ).status_code
        == 200
    )
    saved = client.put(f"/api/settings/{KEY}", json={"value": value}, headers=ADMIN)
    assert saved.status_code == 200, saved.text
    assert f"account {cloud} is disabled" in saved.json()["warnings"]
    assert f"account {cloud} is disabled" in setting(client)["warnings"]

    assert client.delete(
        f"/api/accounts/{cloud}",
        params={"confirm_name": "anthropic-fixture"},
        headers=ADMIN,
    ).status_code in (200, 204)
    assert f"account {cloud} is missing" in setting(client)["warnings"]
    # A new save may not reference a deleted account.
    refused = put(client, value)
    assert refused.status_code == 422


def test_claim_advance_exhaust_and_reset_over_http(client: TestClient) -> None:
    cloud = account(client, "anthropic")
    custom = account(client, "custom", config=custom_config())
    chain = {
        "version": 1,
        "difficulties": {
            "hard": [
                direct(cloud, "anthropic", id="cloud-first"),
                direct(custom, "custom", id="custom-second"),
            ]
        },
    }
    assert put(client, chain).status_code == 200

    project(client)
    boot = client.post("/api/agents/bootstrap-tokens", json={}, headers=ADMIN).json()
    worker = {
        "Authorization": f"Bearer "
        f"{client.post('/api/agents/register', json={'bootstrap_token': boot['token']}).json()['token']}"
    }
    create = client.post(
        "/api/tasks",
        json={"project": "demo", "title": "chain over http", "difficulty": "hard"},
        headers=ADMIN,
    )
    assert create.status_code == 201, create.text
    task_id = create.json()["id"]

    def claim() -> dict:
        res = client.post(
            "/api/tasks/claim",
            json={
                "capabilities": CAPS,
                "runtime_providers": ["anthropic", "custom"],
            },
            headers=worker,
        )
        assert res.status_code in (200, 204), res.text
        return res.json() if res.status_code == 200 else None

    first = claim()
    execution = first["execution"]
    assert execution["alternative"]["id"] == "cloud-first"
    assert execution.get("executor") is None and execution["generation"] == 1
    # An explicit chain is not projected as a legacy model policy.
    assert first["model_policy"] is None and first["difficulty_executors"] is None
    assert first["engine_lease"]["lease_id"] == execution["harness"]["lease_id"]
    assert first.get("executor_lease") is None

    url = f"/api/tasks/{task_id}/execution"
    base = {
        "attempt": first["attempt"],
        "generation": execution["generation"],
    }
    applied = client.post(url, json={**base, "action": "applied"}, headers=worker)
    assert applied.status_code == 204, applied.text

    # Advance requires the stop acknowledgement, a classified reason and the exact fence.
    assert (
        client.post(
            url, json={**base, "action": "advance", "reason": "quota"}, headers=worker
        ).status_code
        == 422
    )
    assert (
        client.post(
            url,
            json={
                **base,
                "generation": execution["generation"] + 1,
                "action": "advance",
                "stopped": True,
                "reason": "quota",
            },
            headers=worker,
        ).status_code
        == 409
    )
    assert (
        client.post(
            url,
            json={**base, "action": "advance", "stopped": True, "reason": "security"},
            headers=worker,
        ).status_code
        == 422
    )

    advanced = client.post(
        url,
        json={**base, "action": "advance", "stopped": True, "reason": "quota"},
        headers=worker,
    )
    assert advanced.status_code == 204, advanced.text
    # A duplicate of the same transition cannot advance twice.
    assert (
        client.post(
            url,
            json={**base, "action": "advance", "stopped": True, "reason": "quota"},
            headers=worker,
        ).status_code
        == 409
    )

    second = claim()
    assert second["execution"]["alternative"]["id"] == "custom-second"
    assert second["execution"]["generation"] > execution["generation"]
    quota = client.post(
        url,
        json={
            "attempt": second["attempt"],
            "generation": second["execution"]["generation"],
            "action": "advance",
            "stopped": True,
            "reason": "quota",
        },
        headers=worker,
    )
    assert quota.status_code == 204, quota.text

    route = client.get(f"/api/tasks/{task_id}/route", headers=ADMIN).json()
    assert route["execution"]["cursor"] == 2
    assert route["state"] == "blocked"
    assert route["block_reason"] == "execution_chain_exhausted"
    assert route["resolution"] is None

    reset = client.post(
        f"/api/tasks/{task_id}/unblock",
        params={"reset_execution_route": True},
        headers=ADMIN,
    )
    assert reset.status_code == 200, reset.text
    route = client.get(f"/api/tasks/{task_id}/route", headers=ADMIN).json()
    assert route["execution"]["cursor"] == 0
    assert route["execution"]["generation"] > second["execution"]["generation"]
    assert route["execution"]["selection"] is None

    # Both accounts hit quota above, so nothing is admissible right now: the
    # reset task waits with the transient reason instead of spending an attempt.
    assert claim() is None
    route = client.get(f"/api/tasks/{task_id}/route", headers=ADMIN).json()
    assert route["state"] == "blocked"
    assert route["block_reason"] == "awaiting_execution_route"
    assert client.get(f"/api/tasks/{task_id}", headers=ADMIN).json()["attempt"] == 2
    assert {s["reason"] for s in route["execution"]["skips"]} == {
        "engine_account_unavailable"
    }


FAKE_GOOGLE_ACCESS = "ya29.FAKE_OAUTH_EXEC_CHAIN_0000000000"
FAKE_GOOGLE_REFRESH = "1//FAKE_OAUTH_REFRESH_CHAIN_00000000"
GOOGLE_CAPS = [*CAPS, "nanoclaw:executor:agy:google"]


def _register_worker(client: TestClient) -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={}, headers=ADMIN).json()
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    assert reg.status_code == 201, reg.text
    return {"Authorization": f"Bearer {reg.json()['token']}"}


def _google_oauth_account(
    client: TestClient, *, expiry: str, name: str = "google-exec"
) -> str:
    res = client.post(
        "/api/accounts",
        json={
            "name": name,
            "provider": "google",
            "credential": {
                "token": {
                    "access_token": FAKE_GOOGLE_ACCESS,
                    "token_type": "Bearer",
                    "refresh_token": FAKE_GOOGLE_REFRESH,
                    "expiry": expiry,
                    "expires_in": 3600,
                },
                "auth_method": "consumer",
                "id_token_email": "exec@example.test",
            },
        },
        headers=ADMIN,
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _paired_google(harness_id: str, google_id: str, id: str = "claude-agy") -> dict:
    return {
        "id": id,
        "mode": "paired",
        "runtime": "nanoclaw",
        "harness": {
            "account_id": harness_id,
            "provider": "anthropic",
            "adapter": "claude",
            "model": {"source": "explicit", "id": "claude-sonnet-4-6"},
        },
        "executor": {
            "account_id": google_id,
            "provider": "google",
            "adapter": "agy",
            "model": {"source": "explicit", "id": "gemini-4-neon"},
        },
    }


def _create_ready_task(
    client: TestClient,
    *,
    project_slug: str = "demo",
    title: str = "google chain task",
    difficulty: str = "hard",
    max_attempts: int = 3,
) -> str:
    res = client.post(
        "/api/tasks",
        json={
            "project": project_slug,
            "title": title,
            "difficulty": difficulty,
            "max_attempts": max_attempts,
        },
        headers=ADMIN,
    )
    assert res.status_code == 201, res.text
    task = res.json()
    if task["state"] == "awaiting_approval":
        approve = client.post(f"/api/tasks/{task['id']}/approve", headers=ADMIN)
        assert approve.status_code == 200, approve.text
    return task["id"]


def test_expired_google_executor_advances_to_fallback_and_exhausts_without_stranding(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expired Google OAuth executor whose refresh transiently fails reaches
    fallback (or exhaustion when last) via fenced advance without stranding or
    blocking the healthy harness account."""
    from argus_api.accounts.google_refresh import GoogleTokenRefresher

    monkeypatch.setenv("ARGUS_GOOGLE_OAUTH_CLIENT_ID", "cid-test")
    monkeypatch.setenv("ARGUS_GOOGLE_OAUTH_CLIENT_SECRET", "csec-test")

    async def transient_fail(_url: str, _form: dict[str, str]) -> tuple[int, dict]:
        return 503, {"error": "temporarily_unavailable"}

    app.state.google_refresher = GoogleTokenRefresher(post_form=transient_fail)

    project(client)
    harness_id = account(client, "anthropic", max_leases=2)
    google_id = _google_oauth_account(client, expiry="2020-01-01T00:00:00Z")
    worker_a = _register_worker(client)
    worker_b = _register_worker(client)

    # Unrelated agent B holds its own lease (must survive worker A's failure path).
    unrelated_task = _create_ready_task(
        client, title="unrelated task", difficulty="simple"
    )
    b_claim = client.post(
        "/api/tasks/claim",
        json={"capabilities": [], "runtime_providers": ["anthropic"]},
        headers=worker_b,
    )
    assert b_claim.status_code == 200 and b_claim.json()["id"] == unrelated_task

    chain = {
        "version": 1,
        "difficulties": {
            "hard": [
                _paired_google(harness_id, google_id, id="claude-agy"),
                direct(harness_id, "anthropic", id="claude-fallback"),
            ]
        },
    }
    assert put(client, chain).status_code == 200

    task_id = _create_ready_task(client, title="expired google executor")
    first = client.post(
        "/api/tasks/claim",
        json={"capabilities": GOOGLE_CAPS, "runtime_providers": ["anthropic"]},
        headers=worker_a,
    )
    assert first.status_code == 200, first.text
    claimed = first.json()
    assert claimed["execution"]["alternative"]["id"] == "claude-agy"
    assert claimed["executor_lease"]["account_id"] == google_id

    # Worker reports fenced executor auth_expired advance (no blanket /accounts/release).
    adv = client.post(
        f"/api/tasks/{task_id}/execution",
        json={
            "attempt": claimed["attempt"],
            "generation": claimed["execution"]["generation"],
            "action": "advance",
            "stopped": True,
            "reason": "auth_expired",
            "failed_role": "executor",
        },
        headers=worker_a,
    )
    assert adv.status_code == 204, adv.text

    # Task is requeued (not stranded), Google executor is auth_rejected, harness is untouched.
    after = client.get(f"/api/tasks/{task_id}", headers=ADMIN).json()
    assert after["state"] == "ready"
    assert after["assignee_agent_id"] is None
    assert after["failures"] == 0
    accounts_by_id = {
        a["id"]: a for a in client.get("/api/accounts", headers=ADMIN).json()
    }
    assert accounts_by_id[google_id]["lease_block"] == "auth_rejected"
    assert accounts_by_id[harness_id]["lease_block"] is None
    # Unrelated worker B still holds its lease on harness_id (1 active lease, not 0).
    assert accounts_by_id[harness_id]["active_leases"] == 1

    # Next claim lands cleanly on the fallback alternative and records applied.
    second = client.post(
        "/api/tasks/claim",
        json={"capabilities": GOOGLE_CAPS, "runtime_providers": ["anthropic"]},
        headers=worker_a,
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["execution"]["alternative"]["id"] == "claude-fallback"
    applied = client.post(
        f"/api/tasks/{task_id}/execution",
        json={
            "attempt": second_body["attempt"],
            "generation": second_body["execution"]["generation"],
            "action": "applied",
        },
        headers=worker_a,
    )
    assert applied.status_code == 204, applied.text

    # No fake credential value ever appears in task or route diagnostics.
    route_text = client.get(f"/api/tasks/{task_id}/route", headers=ADMIN).text
    assert FAKE_GOOGLE_ACCESS not in route_text
    assert FAKE_GOOGLE_REFRESH not in route_text
    assert SECRET not in route_text

    # Single-alternative chain exhausts cleanly into blocked execution_chain_exhausted.
    google_id_2 = _google_oauth_account(
        client, expiry="2020-01-01T00:00:00Z", name="google-exec-2"
    )
    assert (
        put(
            client,
            {
                "version": 1,
                "difficulties": {
                    "standard": [_paired_google(harness_id, google_id_2, id="only-agy")]
                },
            },
        ).status_code
        == 200
    )
    worker_c = _register_worker(client)
    single_task = _create_ready_task(
        client, title="single expired google", difficulty="standard"
    )
    # Free harness slot from worker_b so worker_c can claim.
    assert client.post("/api/accounts/release", headers=worker_b).status_code == 204
    c_claim = client.post(
        "/api/tasks/claim",
        json={"capabilities": GOOGLE_CAPS, "runtime_providers": ["anthropic"]},
        headers=worker_c,
    )
    assert c_claim.status_code == 200, c_claim.text
    c_body = c_claim.json()
    c_adv = client.post(
        f"/api/tasks/{single_task}/execution",
        json={
            "attempt": c_body["attempt"],
            "generation": c_body["execution"]["generation"],
            "action": "advance",
            "stopped": True,
            "reason": "auth_expired",
            "failed_role": "executor",
        },
        headers=worker_c,
    )
    assert c_adv.status_code == 204, c_adv.text
    exhausted = client.get(f"/api/tasks/{single_task}", headers=ADMIN).json()
    assert exhausted["state"] == "blocked"
    assert exhausted["block_reason"] == "execution_chain_exhausted"
    assert exhausted["assignee_agent_id"] is None


def test_rejected_google_executor_skips_to_fallback_or_blocks_without_claim(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revoked Google OAuth refresh token (`invalid_grant`) blocks the account at
    claim time and moves straight to the fallback (or awaits route with 0 claims)."""
    from argus_api.accounts.google_refresh import GoogleTokenRefresher

    monkeypatch.setenv("ARGUS_GOOGLE_OAUTH_CLIENT_ID", "cid-test")
    monkeypatch.setenv("ARGUS_GOOGLE_OAUTH_CLIENT_SECRET", "csec-test")

    async def revoked(_url: str, _form: dict[str, str]) -> tuple[int, dict]:
        return 400, {"error": "invalid_grant"}

    app.state.google_refresher = GoogleTokenRefresher(post_form=revoked)

    project(client)
    harness_id = account(client, "anthropic")
    google_id = _google_oauth_account(client, expiry="2020-01-01T00:00:00Z")
    worker = _register_worker(client)

    # 1. With a fallback alternative, claim skips the revoked Google pair cleanly.
    assert (
        put(
            client,
            {
                "version": 1,
                "difficulties": {
                    "hard": [
                        _paired_google(harness_id, google_id, id="revoked-agy"),
                        direct(harness_id, "anthropic", id="claude-fallback"),
                    ],
                    "standard": [_paired_google(harness_id, google_id, id="only-agy")],
                },
            },
        ).status_code
        == 200
    )
    t_fallback = _create_ready_task(
        client, title="revoked with fallback", difficulty="hard"
    )
    claimed = client.post(
        "/api/tasks/claim",
        json={"capabilities": GOOGLE_CAPS, "runtime_providers": ["anthropic"]},
        headers=worker,
    )
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["id"] == t_fallback
    assert body["execution"]["alternative"]["id"] == "claude-fallback"
    assert (
        client.post(
            f"/api/tasks/{t_fallback}/execution",
            json={
                "attempt": body["attempt"],
                "generation": body["execution"]["generation"],
                "action": "applied",
            },
            headers=worker,
        ).status_code
        == 204
    )

    # 2. With no fallback alternative, task blocks awaiting_execution_route with 0 claims.
    worker_2 = _register_worker(client)
    t_only = _create_ready_task(
        client, title="revoked only option", difficulty="standard"
    )
    res = client.post(
        "/api/tasks/claim",
        json={"capabilities": GOOGLE_CAPS, "runtime_providers": ["anthropic"]},
        headers=worker_2,
    )
    assert res.status_code == 204, res.text
    blocked = client.get(f"/api/tasks/{t_only}", headers=ADMIN).json()
    assert blocked["state"] == "blocked"
    assert blocked["block_reason"] == "awaiting_execution_route"
    assert blocked["attempt"] == 0
    assert blocked["assignee_agent_id"] is None
    route_text = client.get(f"/api/tasks/{t_only}/route", headers=ADMIN).text
    assert FAKE_GOOGLE_ACCESS not in route_text
    assert FAKE_GOOGLE_REFRESH not in route_text


def test_lost_tuple_ownership_before_advance_fails_fenced_and_preserves_other_leases(
    client: TestClient,
) -> None:
    """When a tuple lease is released before `action=advance`, the API must not
    strand the task claimed: it releases any remaining lease of this exact tuple,
    fails the attempt with a sanitized reason, preserves unrelated leases, and
    keeps old-attempt/old-generation fencing intact."""
    from datetime import UTC, datetime, timedelta

    project(client)
    harness_id = account(client, "anthropic", max_leases=2)
    google_id = _google_oauth_account(
        client, expiry=(datetime.now(UTC) + timedelta(hours=1)).isoformat()
    )
    worker_a = _register_worker(client)
    worker_b = _register_worker(client)

    # Unrelated worker B holds a lease on harness_id.
    _create_ready_task(client, title="other task", difficulty="simple")
    b_claim = client.post(
        "/api/tasks/claim",
        json={"capabilities": [], "runtime_providers": ["anthropic"]},
        headers=worker_b,
    )
    assert b_claim.status_code == 200

    assert (
        put(
            client,
            {
                "version": 1,
                "difficulties": {
                    "hard": [
                        _paired_google(harness_id, google_id, id="claude-agy"),
                        direct(harness_id, "anthropic", id="claude-fallback"),
                    ]
                },
            },
        ).status_code
        == 200
    )
    task_id = _create_ready_task(client, title="lost ownership race", max_attempts=3)
    claimed = client.post(
        "/api/tasks/claim",
        json={"capabilities": GOOGLE_CAPS, "runtime_providers": ["anthropic"]},
        headers=worker_a,
    )
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    attempt = body["attempt"]
    generation = body["execution"]["generation"]

    # Simulate lost-ownership race: executor lease is released externally while
    # harness lease and task claim are still live.
    assert (
        client.post("/api/accounts/release?role=executor", headers=worker_a).status_code
        == 204
    )

    # `applied` must still be refused when the tuple is no longer owned.
    applied = client.post(
        f"/api/tasks/{task_id}/execution",
        json={"attempt": attempt, "generation": generation, "action": "applied"},
        headers=worker_a,
    )
    assert applied.status_code == 409
    assert applied.json()["detail"]["code"] == "execution_lease_missing"

    # `advance` must NOT leave the task claimed or strand the remaining harness lease.
    adv = client.post(
        f"/api/tasks/{task_id}/execution",
        json={
            "attempt": attempt,
            "generation": generation,
            "action": "advance",
            "stopped": True,
            "reason": "provider_unavailable",
            "failed_role": "executor",
        },
        headers=worker_a,
    )
    assert adv.status_code == 204, adv.text

    after = client.get(f"/api/tasks/{task_id}", headers=ADMIN).json()
    assert after["state"] == "ready"
    assert after["assignee_agent_id"] is None
    assert after["failures"] == 1
    assert "execution tuple no longer owned" in (after["error"] or "")
    assert "provider_unavailable" in (after["error"] or "")
    assert FAKE_GOOGLE_ACCESS not in (after["error"] or "")
    assert SECRET not in (after["error"] or "")

    # Worker A's remaining harness lease was released, while worker B's lease survived.
    accounts_by_id = {
        a["id"]: a for a in client.get("/api/accounts", headers=ADMIN).json()
    }
    assert accounts_by_id[google_id]["active_leases"] == 0
    assert accounts_by_id[harness_id]["active_leases"] == 1

    # Fencing: late/duplicate /execution and /fail for the old attempt/generation fail with 409.
    assert (
        client.post(
            f"/api/tasks/{task_id}/execution",
            json={
                "attempt": attempt,
                "generation": generation,
                "action": "advance",
                "stopped": True,
                "reason": "provider_unavailable",
                "failed_role": "executor",
            },
            headers=worker_a,
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/tasks/{task_id}/fail",
            json={"attempt": attempt, "error": "late fail", "reason": "channel_error"},
            headers=worker_a,
        ).status_code
        == 409
    )


def test_valid_google_executor_credential_claims_and_records_applied(
    client: TestClient,
) -> None:
    """Negative control: a valid fake Google executor credential claims cleanly,
    records `applied`, and retains both tuple leases."""
    from datetime import UTC, datetime, timedelta

    project(client)
    harness_id = account(client, "anthropic")
    google_id = _google_oauth_account(
        client, expiry=(datetime.now(UTC) + timedelta(hours=1)).isoformat()
    )
    worker = _register_worker(client)

    assert (
        put(
            client,
            {
                "version": 1,
                "difficulties": {
                    "hard": [_paired_google(harness_id, google_id, id="claude-agy")]
                },
            },
        ).status_code
        == 200
    )
    task_id = _create_ready_task(client, title="valid google executor")
    claimed = client.post(
        "/api/tasks/claim",
        json={"capabilities": GOOGLE_CAPS, "runtime_providers": ["anthropic"]},
        headers=worker,
    )
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["execution"]["alternative"]["id"] == "claude-agy"
    assert body["executor_lease"]["account_id"] == google_id
    applied = client.post(
        f"/api/tasks/{task_id}/execution",
        json={
            "attempt": body["attempt"],
            "generation": body["execution"]["generation"],
            "action": "applied",
        },
        headers=worker,
    )
    assert applied.status_code == 204, applied.text
    route = client.get(f"/api/tasks/{task_id}/route", headers=ADMIN).json()
    assert route["state"] == "claimed"
    assert route["execution"]["selection"]["alternative"]["id"] == "claude-agy"
    accounts_by_id = {
        a["id"]: a for a in client.get("/api/accounts", headers=ADMIN).json()
    }
    assert accounts_by_id[harness_id]["active_leases"] == 1
    assert accounts_by_id[google_id]["active_leases"] == 1


def _paired_dsh(
    harness_id: str,
    custom_id: str,
    *,
    model: dict | None = None,
    id: str = "claude-dsh",
) -> dict:
    return {
        "id": id,
        "mode": "paired",
        "runtime": "nanoclaw",
        "harness": {
            "account_id": harness_id,
            "provider": "anthropic",
            "adapter": "claude",
            "model": {"source": "explicit", "id": "claude-sonnet-4-6"},
        },
        "executor": {
            "account_id": custom_id,
            "provider": "custom",
            "adapter": "dsh",
            "model": model or {"source": "account"},
        },
    }


def test_dsh_routes_refused_when_argus_dsh_enabled_is_not_true(
    client: TestClient,
) -> None:
    project(client)
    harness_id = account(client, "anthropic")
    custom_id = account(client, "custom", config=custom_config())

    # Catalogue hides both the engine entry and dsh execution capabilities by default.
    engines = client.get("/api/engines", headers=ADMIN).json()
    assert "dsh" not in {e["id"] for e in engines["engines"]}
    assert not any(c["adapter"] == "dsh" for c in engines["execution_capabilities"])

    # Task create with dsh executor_model is refused.
    pin = {
        "executor": "dsh",
        "model": "deepseek/deepseek-chat",
        "account_provider": "custom",
    }
    created = client.post(
        "/api/tasks",
        json={"project": "demo", "title": "dsh pin", "executor_model": pin},
        headers=ADMIN,
    )
    assert created.status_code == 422, created.text
    assert created.json()["detail"]["code"] == "dsh_disabled"

    # Task edit setting dsh executor_model is refused.
    plain = client.post(
        "/api/tasks",
        json={"project": "demo", "title": "plain"},
        headers=ADMIN,
    ).json()
    edited = client.patch(
        f"/api/tasks/{plain['id']}",
        json={
            "expected_version": plain["version"],
            "changes": {"executor_model": pin},
        },
        headers=ADMIN,
    )
    assert edited.status_code == 422, edited.text
    assert edited.json()["detail"]["code"] == "dsh_disabled"

    # Execution chain and difficulty.executors referencing dsh are refused.
    chain_res = put(
        client,
        {"version": 1, "difficulties": {"hard": [_paired_dsh(harness_id, custom_id)]}},
    )
    assert chain_res.status_code == 422, chain_res.text
    assert chain_res.json()["detail"]["code"] == "dsh_disabled"

    diff_res = client.put(
        "/api/settings/difficulty.executors",
        json={"value": {"standard": ["dsh"]}},
        headers=ADMIN,
    )
    assert diff_res.status_code == 422, diff_res.text
    assert diff_res.json()["detail"]["code"] == "dsh_disabled"


def test_dsh_routes_enabled_engine_model_fallback_and_missing_model_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in list(os.environ):
        if name.startswith(("ARGUS_", "DOCKER_HOST")) and not name.startswith(
            "ARGUS_TEST_"
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ARGUS_DISK_GUARD_ENABLED", "false")
    monkeypatch.setenv("ARGUS_DSH_ENABLED", "true")
    with TestClient(app) as client:
        app.state.secrets = MemorySecretStore()
        project(client)

        # Catalogue advertises dsh and both custom/local capabilities when enabled.
        engines = client.get("/api/engines", headers=ADMIN).json()
        assert "dsh" in {e["id"] for e in engines["engines"]}
        cap_ids = {c["id"] for c in engines["execution_capabilities"]}
        assert {
            "nanoclaw:executor:dsh:custom",
            "nanoclaw:executor:dsh:local",
        } <= cap_ids

        # Model policy accepts dsh chains.
        pol = client.put(
            "/api/models",
            json={
                "harness": {"claude": ["claude-sonnet-4-6"]},
                "implementation": {"dsh": ["deepseek/deepseek-chat"]},
            },
            headers=ADMIN,
        )
        assert pol.status_code == 200, pol.text

        harness_id = account(client, "anthropic", max_leases=2)
        # Custom account with engine model ONLY (no executor model): dsh falls back to engine.model.
        engine_only_id = account(
            client,
            "custom",
            name="custom-engine-only",
            config={
                "base_url": "https://openrouter.ai/api/v1",
                "label": "openrouter",
                "engine": {"model": "deepseek/deepseek-chat"},
                "executor": {"model": None},
            },
        )
        # Custom account with NEITHER executor nor engine model: must fail with explicit reason.
        no_model_id = account(
            client,
            "custom",
            name="custom-no-model",
            config={
                "base_url": "https://openrouter.ai/api/v1",
                "label": "openrouter",
                "engine": {"model": None},
                "executor": {"model": None},
            },
        )

        # Saving a chain against the model-less custom account fails with explicit reason.
        bad_chain = put(
            client,
            {
                "version": 1,
                "difficulties": {"hard": [_paired_dsh(harness_id, no_model_id)]},
            },
        )
        assert bad_chain.status_code == 422, bad_chain.text
        assert "executor_model_missing" in bad_chain.text

        # Saving a chain against the engine-only custom account succeeds and resolves engine.model.
        ok_chain = put(
            client,
            {
                "version": 1,
                "difficulties": {"hard": [_paired_dsh(harness_id, engine_only_id)]},
            },
        )
        assert ok_chain.status_code == 200, ok_chain.text
        assert ok_chain.json()["warnings"] == []

        worker = _register_worker(client)
        task_id = _create_ready_task(client, title="dsh chain task", difficulty="hard")

        # Worker without nanoclaw:executor:dsh:custom cannot claim the dsh chain.
        denied = client.post(
            "/api/tasks/claim",
            json={"capabilities": CAPS, "runtime_providers": ["anthropic", "custom"]},
            headers=worker,
        )
        assert denied.status_code == 204

        # Reset/unblock and claim with nanoclaw:executor:dsh:custom: lands cleanly with engine.model.
        client.post(f"/api/tasks/{task_id}/unblock", headers=ADMIN)
        dsh_caps = [*CAPS, "nanoclaw:executor:dsh:custom"]
        claimed = client.post(
            "/api/tasks/claim",
            json={
                "capabilities": dsh_caps,
                "runtime_providers": ["anthropic", "custom"],
            },
            headers=worker,
        )
        assert claimed.status_code == 200, claimed.text
        body = claimed.json()
        assert body["execution"]["executor"]["selector"]["adapter"] == "dsh"
        assert body["execution"]["executor"]["selector"]["provider"] == "custom"
        assert body["execution"]["executor"]["model"] == "deepseek/deepseek-chat"
        assert body["executor_lease"]["account_id"] == engine_only_id
        assert body["executor_lease"]["settings"]["model"] == "deepseek/deepseek-chat"

        # Task-pin path: dsh@custom accepted when enabled, and executor lease falls back to engine.model.
        worker2 = _register_worker(client)
        pin_task = client.post(
            "/api/tasks",
            json={
                "project": "demo",
                "title": "dsh pinned task",
                "difficulty": "standard",
                "executor_model": {
                    "executor": "dsh",
                    "model": "deepseek/deepseek-chat",
                    "account_provider": "custom",
                },
            },
            headers=ADMIN,
        )
        assert pin_task.status_code == 201, pin_task.text
        # Release worker's leases so engine_only_id is free, then claim & lease executor.
        assert client.post("/api/accounts/release", headers=worker).status_code == 204
        c2 = client.post(
            "/api/tasks/claim",
            json={"capabilities": dsh_caps, "runtime_providers": ["anthropic"]},
            headers=worker2,
        )
        assert c2.status_code == 200, c2.text
        c2_body = c2.json()
        assert c2_body["id"] == pin_task.json()["id"]
        exec_lease = client.post(
            "/api/accounts/lease",
            json={
                "role": "executor",
                "runtime_providers": ["custom"],
                "reuse_held": False,
                "task_id": c2_body["id"],
                "attempt": c2_body["attempt"],
            },
            headers=worker2,
        )
        assert exec_lease.status_code == 200, exec_lease.text
        assert exec_lease.json()["account_id"] == engine_only_id
        assert exec_lease.json()["settings"]["model"] == "deepseek/deepseek-chat"

        # Disable the engine-only account so only the model-less custom account remains: explicit failure.
        assert (
            client.patch(
                f"/api/accounts/{engine_only_id}",
                json={"enabled": False},
                headers=ADMIN,
            ).status_code
            == 200
        )
        no_model_lease = client.post(
            "/api/accounts/lease",
            json={
                "role": "executor",
                "runtime_providers": ["custom"],
                "reuse_held": False,
                "task_id": c2_body["id"],
                "attempt": c2_body["attempt"],
            },
            headers=worker2,
        )
        assert no_model_lease.status_code == 409
        assert "executor or engine model" in no_model_lease.text


def test_account_failover_reaches_normal_secret_delivery(client: TestClient):
    worker = _register_worker(client)
    anth_a = account(
        client,
        "anthropic",
        name="anth-a",
        credential={"api_key": "sk-ant-api03-fixture-a"},
        priority=10,
    )
    anth_b = account(
        client,
        "anthropic",
        name="anth-b",
        credential={"api_key": "sk-ant-api03-fixture-b"},
        priority=5,
    )
    chain = [
        {
            "id": "single-opus",
            "mode": "single",
            "runtime": "nanoclaw",
            "harness": {
                "adapter": "claude",
                "provider": "anthropic",
                "model": {"source": "explicit", "id": "claude-opus-4"},
            },
        }
    ]
    put = client.put(
        "/api/settings/execution.chains",
        json={"value": {"version": 1, "difficulties": {"hard": chain}}},
        headers=ADMIN,
    )
    assert put.status_code == 200, put.text
    created = client.post(
        "/api/tasks",
        json={
            "project": "demo",
            "title": "secret delivery failover",
            "difficulty": "hard",
        },
        headers=ADMIN,
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]

    claim1 = client.post(
        "/api/tasks/claim",
        json={"capabilities": CAPS, "runtime_providers": ["anthropic"]},
        headers=worker,
    )
    assert claim1.status_code == 200, claim1.text
    c1 = claim1.json()
    assert c1["engine_lease"]["account_id"] == anth_a
    assert c1["engine_lease"]["credential"] == {"api_key": "sk-ant-api03-fixture-a"}

    rep = client.post(
        f"/api/tasks/{task_id}/execution",
        json={
            "attempt": c1["attempt"],
            "generation": c1["execution"]["generation"],
            "action": "advance",
            "stopped": True,
            "reason": "quota",
        },
        headers=worker,
    )
    assert rep.status_code == 204, rep.text

    claim2 = client.post(
        "/api/tasks/claim",
        json={"capabilities": CAPS, "runtime_providers": ["anthropic"]},
        headers=worker,
    )
    assert claim2.status_code == 200, claim2.text
    c2 = claim2.json()
    assert c2["execution"]["alternative"]["id"] == "single-opus"
    assert c2["execution"]["harness"]["account_id"] == anth_b
    assert c2["execution"]["harness"]["model"] == "claude-opus-4"
    assert c2["engine_lease"]["account_id"] == anth_b
    assert c2["engine_lease"]["credential"] == {"api_key": "sk-ant-api03-fixture-b"}
