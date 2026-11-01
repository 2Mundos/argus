"""Attempt usage through the installed HTTP wiring (account usage spec 3.10):
worker routes write, `GET /api/accounts/cost` reads. No live provider is used."""

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


@pytest.fixture
def worker(client: TestClient) -> dict[str, str]:
    return register(client)


@pytest.fixture
def account(client: TestClient) -> str:
    res = client.post(
        "/api/accounts",
        json={
            "name": "acc",
            "provider": "openai",
            "credential": {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-test-not-real"},
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


def claim(client: TestClient, worker: dict[str, str], title: str | None = "t") -> dict:
    if title is not None:
        res = client.post("/api/tasks", json={"project": "demo", "title": title})
        assert res.status_code == 201
    res = client.post("/api/tasks/claim", json={}, headers=worker)
    assert res.status_code == 200, res.text
    task = res.json()
    lease = client.post(
        "/api/accounts/lease",
        json={
            "runtime_providers": ["openai"],
            "role": "engine",
            "task_id": task["id"],
            "attempt": task["attempt"],
        },
        headers=worker,
    )
    assert lease.status_code == 200, lease.text
    return task


def report(
    client: TestClient,
    worker: dict[str, str],
    account: str,
    task: dict,
    run_key: str,
    **kw: object,
) -> int:
    body = {
        "task_id": task["id"],
        "attempt": task["attempt"],
        "run_key": run_key,
        "input_tokens": 100,
        "output_tokens": 10,
        "model": "gpt-5",
        **kw,
    }
    return client.post(
        f"/api/accounts/{account}/usage", json=body, headers=worker
    ).status_code


def complete(client: TestClient, worker: dict[str, str], task: dict) -> None:
    res = client.post(
        f"/api/tasks/{task['id']}/complete",
        json={"attempt": task["attempt"], "result": {"summary": "done"}},
        headers=worker,
    )
    assert res.status_code == 200, res.text


def cost(client: TestClient) -> dict:
    res = client.get("/api/accounts/cost", params={"window": "24h"})
    assert res.status_code == 200, res.text
    return res.json()


def acct(client: TestClient, account: str) -> dict:
    return {a["account_id"]: a for a in cost(client)["by_account"]}[account]


def test_measured_report_then_complete_is_one_measured_attempt(
    client: TestClient, worker: dict[str, str], account: str
) -> None:
    task = claim(client, worker)
    assert report(client, worker, account, task, "r1") == 204
    complete(client, worker, task)
    a = acct(client, account)
    assert (a["attempts"], a["measured_attempts"], a["not_reported_attempts"]) == (
        1,
        1,
        0,
    )
    assert (a["input_tokens"], a["output_tokens"]) == (100, 10)


def test_crash_without_report_is_not_reported_never_zero(
    client: TestClient, worker: dict[str, str], account: str
) -> None:
    task = claim(client, worker)
    complete(client, worker, task)
    a = acct(client, account)
    assert (a["attempts"], a["measured_attempts"], a["not_reported_attempts"]) == (
        1,
        0,
        1,
    )
    assert a["input_tokens"] is None
    assert a["output_tokens"] is None
    assert a["estimated_cost_usd"] is None


def test_late_and_duplicate_reports_upgrade_once(
    client: TestClient, worker: dict[str, str], account: str
) -> None:
    task = claim(client, worker)
    complete(client, worker, task)  # placeholder written
    assert report(client, worker, account, task, "r1") == 204  # late
    assert report(client, worker, account, task, "r1") == 204  # duplicate
    a = acct(client, account)
    assert (a["attempts"], a["measured_attempts"], a["not_reported_attempts"]) == (
        1,
        1,
        0,
    )
    assert a["runs"] == 1
    assert (a["input_tokens"], a["output_tokens"]) == (100, 10)
    # A later not_reported event cannot overwrite measured data.
    assert (
        report(
            client,
            worker,
            account,
            task,
            "r2",
            input_tokens=None,
            output_tokens=None,
            measurement="not_reported",
            reason="no_usage_in_output",
        )
        == 204
    )
    a = acct(client, account)
    assert (a["measured_attempts"], a["input_tokens"]) == (1, 100)


@pytest.mark.parametrize("path", ["release", "block", "fail"])
def test_leaving_the_claim_leaves_an_outcome_and_retry_adds_another(
    client: TestClient, worker: dict[str, str], account: str, path: str
) -> None:
    task = claim(client, worker)
    if path == "release":
        res = client.post(
            f"/api/tasks/{task['id']}/release",
            json={"reason": "recycle", "attempt": task["attempt"]},
        )
    elif path == "block":
        res = client.post(
            f"/api/tasks/{task['id']}/block",
            json={"attempt": task["attempt"], "reason": "worker_env"},
            headers=worker,
        )
    else:
        res = client.post(
            f"/api/tasks/{task['id']}/fail",
            json={"attempt": task["attempt"], "error": "boom"},
            headers=worker,
        )
    assert res.status_code == 200, res.text
    a = acct(client, account)
    assert (a["attempts"], a["not_reported_attempts"], a["measured_attempts"]) == (
        1,
        1,
        0,
    )
    assert a["input_tokens"] is None
    if path != "release":  # a failure backs off; only a release requeues at once
        return
    # The retry is a new attempt of the same task: counted separately.
    second = claim(client, worker, None)
    assert second["id"] == task["id"]
    assert second["attempt"] == task["attempt"] + 1
    assert report(client, worker, account, second, "r-retry") == 204
    complete(client, worker, second)
    a = acct(client, account)
    assert (a["attempts"], a["measured_attempts"], a["not_reported_attempts"]) == (
        2,
        1,
        1,
    )
    assert a["input_tokens"] == 100


def test_quota_exhaustion_parks_the_task_and_leaves_a_placeholder(
    client: TestClient, worker: dict[str, str], account: str
) -> None:
    task = claim(client, worker)
    res = client.post(
        f"/api/accounts/{account}/exhausted",
        json={
            "runtime_providers": ["openai"],
            "task_id": task["id"],
            "attempt": task["attempt"],
            "reason": "quota",
        },
        headers=worker,
    )
    assert res.status_code == 200, res.text
    a = acct(client, account)
    assert (a["attempts"], a["not_reported_attempts"], a["measured_attempts"]) == (
        1,
        1,
        0,
    )


def test_mixed_coverage_sums_only_measured(
    client: TestClient, worker: dict[str, str], account: str
) -> None:
    measured = claim(client, worker, "m")
    report(client, worker, account, measured, "m1")
    complete(client, worker, measured)
    unknown = claim(client, worker, "u")
    complete(client, worker, unknown)
    a = acct(client, account)
    assert (a["attempts"], a["measured_attempts"], a["not_reported_attempts"]) == (
        2,
        1,
        1,
    )
    assert a["input_tokens"] == 100  # the unknown attempt adds nothing, not zero
    totals = cost(client)["totals"]
    assert (totals["attempts"], totals["measured_attempts"]) == (2, 1)


def test_report_for_an_unclaimed_attempt_is_unattributed_not_misattributed(
    client: TestClient, worker: dict[str, str], account: str
) -> None:
    """Documented limitation (spec 3.10): a report naming an attempt this agent never
    claimed is stored with attempt=null. The real attempt keeps its not_reported
    outcome; the tokens are counted in the account's runs, never as that attempt's."""
    task = claim(client, worker)
    assert report(client, worker, account, task, "ghost", attempt=99) == 204
    complete(client, worker, task)
    a = acct(client, account)
    assert a["attempts"] == 1
    assert a["not_reported_attempts"] == 1
    assert a["measured_attempts"] == 0
    assert a["runs"] == 2
    assert a["measured_runs"] == 1  # the orphan row's tokens remain visible
    assert a["input_tokens"] == 100
