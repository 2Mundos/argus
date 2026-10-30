"""Onboarding progress (docs/specs/onboarding.spec.md)."""

import os
from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from fastapi.testclient import TestClient
from test_launch import ADMIN, agent_token, bearer, make_pool

ADMIN_TOKEN = ADMIN["Authorization"].split()[1]


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    for name in list(os.environ):
        if name.startswith(("ARGUS_", "DOCKER_HOST")) and not name.startswith(
            "ARGUS_TEST_"
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        app.state.secrets = MemorySecretStore()
        yield c


def test_fresh_install_facts_are_false(client: TestClient) -> None:
    res = client.get("/api/onboarding", headers=ADMIN)
    assert res.status_code == 200, res.text
    assert res.json() == {
        "account": False,
        "worker": False,
        "orchestrator": False,
        "dismissed": False,
    }


def test_non_admin_gets_403(client: TestClient) -> None:
    worker = bearer(agent_token(client, "worker"))
    orch = bearer(agent_token(client, "orchestrator"))
    assert client.get("/api/onboarding", headers=worker).status_code == 403
    assert client.get("/api/onboarding", headers=orch).status_code == 403


def test_harness_account_sets_account_fact(client: TestClient) -> None:
    created = client.post(
        "/api/accounts",
        json={
            "name": "claude",
            "provider": "anthropic",
            "credential": {"api_key": "sk-ant-api03-fake"},
        },
        headers=ADMIN,
    )
    assert created.status_code == 201, created.text
    assert client.get("/api/onboarding", headers=ADMIN).json()["account"] is True

    # Repository accounts alone do not satisfy the account step.
    acc_id = created.json()["id"]
    deleted = client.delete(
        f"/api/accounts/{acc_id}",
        params={"confirm_name": "claude"},
        headers=ADMIN,
    )
    assert deleted.status_code == 204, deleted.text
    gitea = client.post(
        "/api/accounts",
        json={
            "name": "forge",
            "provider": "gitea",
            "credential": {"username": "argus-writer", "token": "tok"},
        },
        headers=ADMIN,
    )
    assert gitea.status_code == 201, gitea.text
    assert client.get("/api/onboarding", headers=ADMIN).json()["account"] is False


def test_pool_sets_worker_fact(client: TestClient) -> None:
    assert client.get("/api/onboarding", headers=ADMIN).json()["worker"] is False
    make_pool(client)
    assert client.get("/api/onboarding", headers=ADMIN).json()["worker"] is True


def test_dismiss_via_setting(client: TestClient) -> None:
    before = client.get("/api/onboarding", headers=ADMIN).json()
    assert before["dismissed"] is False

    worker = bearer(agent_token(client, "worker"))
    assert (
        client.put(
            "/api/settings/onboarding.dismissed",
            json={"value": True},
            headers=worker,
        ).status_code
        == 403
    )

    res = client.put(
        "/api/settings/onboarding.dismissed",
        json={"value": True},
        headers=ADMIN,
    )
    assert res.status_code == 200, res.text
    assert res.json()["value"] is True
    assert client.get("/api/onboarding", headers=ADMIN).json()["dismissed"] is True

    # Project scope is refused.
    client.put(
        "/api/projects/demo",
        json={"name": "Demo", "description": "d"},
        headers=ADMIN,
    )
    bad = client.put(
        "/api/projects/demo/settings/onboarding.dismissed",
        json={"value": False},
        headers=ADMIN,
    )
    assert bad.status_code == 422, bad.text
