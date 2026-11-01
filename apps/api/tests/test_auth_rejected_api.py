"""`POST /api/accounts/{id}/auth-rejected` through the installed HTTP wiring
(account-usage.spec.md 3.5): only the agent holding the account's lease for the
current attempt may record it, and it never exhausts the account."""

import json
import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from fastapi.testclient import TestClient

ADMIN_TOKEN = "test-admin-token-" + "x" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers=ADMIN) as c:
        app.state.secrets = MemorySecretStore()
        yield c


def register(client: TestClient) -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
    res = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    assert res.status_code == 201
    return {"Authorization": f"Bearer {res.json()['token']}"}


def add_account(client: TestClient, name: str, priority: int) -> str:
    res = client.post(
        "/api/accounts",
        json={
            "name": name,
            "provider": "anthropic",
            "priority": priority,
            "credential": {"api_key": f"sk-ant-api03-{name}"},
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


def claim_and_lease(client: TestClient, worker: dict[str, str]) -> tuple[dict, str]:
    res = client.post("/api/tasks", json={"project": "demo", "title": "t"})
    assert res.status_code == 201
    task = client.post("/api/tasks/claim", json={}, headers=worker).json()
    lease = client.post(
        "/api/accounts/lease",
        json={
            "runtime_providers": ["anthropic"],
            "role": "engine",
            "task_id": task["id"],
            "attempt": task["attempt"],
        },
        headers=worker,
    )
    assert lease.status_code == 200, lease.text
    return task, lease.json()["account_id"]


def reject(
    client: TestClient,
    account: str,
    task: dict,
    headers: dict[str, str] | None,
    **kw: object,
):
    body = {"task_id": task["id"], "attempt": task["attempt"], "role": "engine"}
    return client.post(
        f"/api/accounts/{account}/auth-rejected",
        json=body | kw,
        headers=headers if headers is not None else {"Authorization": ""},
    )


def account(client: TestClient, account_id: str) -> dict:
    res = client.get(f"/api/accounts/{account_id}")
    assert res.status_code == 200, res.text
    return res.json()


def rows(tmp_path: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(next(tmp_path.glob("*.db")))
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def rejections(tmp_path: Path, kind: str = "account.auth_rejected") -> list[dict]:
    return [
        {"subject_id": subject, "data": json.loads(data)}
        for subject, data in rows(
            tmp_path,
            f"SELECT subject_id, data FROM events WHERE kind = '{kind}' ORDER BY id",
        )
    ]


@pytest.fixture
def setup(client: TestClient) -> tuple[dict[str, str], dict, str, str]:
    held = add_account(client, "claude-a", 100)
    other = add_account(client, "claude-b", 90)
    worker = register(client)
    task, leased = claim_and_lease(client, worker)
    assert leased == held
    return worker, task, held, other


def test_holder_records_auth_rejected_without_exhausting(
    client: TestClient,
    setup: tuple[dict[str, str], dict, str, str],
    tmp_path: Path,
) -> None:
    worker, task, held, other = setup
    res = reject(client, held, task, worker, source="structured")
    assert res.status_code == 204, res.text
    got = account(client, held)
    assert got["last_rejected_at"] is not None
    assert (got["state"], got["exhausted_until"]) == ("available", None)
    assert account(client, other)["last_rejected_at"] is None
    [event] = rejections(tmp_path)
    assert event["subject_id"] == held
    assert event["data"] == {
        "task_id": task["id"],
        "attempt": task["attempt"],
        "role": "engine",
        "source": "structured",
        "reason": "Claude authentication failed",
    }
    assert "sk-ant" not in str(event)
    # Still leased: the agent keeps the account until the attempt ends.
    assert rows(
        tmp_path,
        "SELECT account_id FROM account_leases WHERE released_at IS NULL",
    ) == [(held,)]


@pytest.mark.parametrize(
    ("headers", "status"),
    [(ADMIN, 403), (None, 401), ({"Authorization": "Bearer not-a-token"}, 401)],
)
def test_requires_an_agent_token(
    client: TestClient,
    setup: tuple[dict[str, str], dict, str, str],
    headers: dict[str, str] | None,
    status: int,
    tmp_path: Path,
) -> None:
    _worker, task, held, _other = setup
    assert reject(client, held, task, headers).status_code == status
    assert rejections(tmp_path) == []
    assert account(client, held)["last_rejected_at"] is None


def test_foreign_unknown_or_unheld_account_is_refused(
    client: TestClient,
    setup: tuple[dict[str, str], dict, str, str],
    tmp_path: Path,
) -> None:
    worker, task, held, other = setup
    stranger = register(client)
    for acct, headers in ((other, worker), ("acc_missing", worker), (held, stranger)):
        res = reject(client, acct, task, headers)
        assert res.status_code == 409, (acct, res.text)
        assert res.json()["detail"]["code"] == "not_leased"
    assert rejections(tmp_path) == []
    assert account(client, other)["last_rejected_at"] is None


def test_role_mismatch_is_refused(
    client: TestClient,
    setup: tuple[dict[str, str], dict, str, str],
    tmp_path: Path,
) -> None:
    worker, task, held, _other = setup
    res = reject(client, held, task, worker, role="executor")
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "not_leased"
    assert rejections(tmp_path) == []


def test_stale_attempt_or_other_task_is_refused(
    client: TestClient,
    setup: tuple[dict[str, str], dict, str, str],
    tmp_path: Path,
) -> None:
    worker, task, held, _other = setup
    stale = reject(client, held, task, worker, attempt=task["attempt"] + 1)
    assert stale.status_code == 409, stale.text
    unclaimed = client.post("/api/tasks", json={"project": "demo", "title": "u"})
    res = reject(client, held, task, worker, task_id=unclaimed.json()["id"])
    assert res.status_code == 409, res.text
    missing = reject(client, held, task, worker, task_id="tsk_missing")
    assert missing.status_code in (404, 409), missing.text
    assert rejections(tmp_path) == []
    assert account(client, held)["last_rejected_at"] is None


def test_later_ordinary_fail_does_not_exhaust_claude(
    client: TestClient,
    setup: tuple[dict[str, str], dict, str, str],
    tmp_path: Path,
) -> None:
    """What the channel sends after a structured authentication_failed: the
    auth-rejected report, then an ordinary provider_failure with the fixed reason."""
    worker, task, held, _other = setup
    assert reject(client, held, task, worker).status_code == 204
    res = client.post(
        f"/api/tasks/{task['id']}/fail",
        json={
            "attempt": task["attempt"],
            "error": "Claude authentication failed",
            "reason": "provider_failure",
        },
        headers=worker,
    )
    assert res.status_code == 200, res.text
    assert res.json().get("failover") is None
    got = account(client, held)
    assert (got["state"], got["exhausted_until"]) == ("available", None)
    assert rejections(tmp_path, "account.exhausted") == []
    assert len(rejections(tmp_path)) == 1


def test_accounts_api_exposes_credential_state(client: TestClient) -> None:
    account_id = add_account(client, "cred-a", 1)
    listed = client.get("/api/accounts").json()
    assert listed[0]["credential"] == {"state": "present", "expires_at": None}
    assert (
        client.get(f"/api/accounts/{account_id}").json()["credential"]["state"]
        == "present"
    )
    app.state.secrets._data.clear()
    assert (
        client.get(f"/api/accounts/{account_id}").json()["credential"]["state"]
        == "missing"
    )
