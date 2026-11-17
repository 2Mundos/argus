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
