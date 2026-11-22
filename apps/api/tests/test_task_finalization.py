"""Execution versus follow-up of an attempt (task-finalization.spec.md)."""

import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from argus_api.tasks.finalization import CHECKPOINT_STALE_S, derive, workspace_facts
from argus_core.agents import LOST_AFTER_S
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


def register(client: TestClient, role: str = "worker") -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": role}).json()
    res = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    assert res.status_code == 201
    return {"Authorization": f"Bearer {res.json()['token']}"}


def claim(client: TestClient, worker: dict[str, str], **fields: object) -> dict:
    client.post("/api/tasks", json={"project": "demo", "title": "t", **fields})
    res = client.post("/api/tasks/claim", json={}, headers=worker)
    assert res.status_code == 200, res.text
    return res.json()


def report(client, worker, task, attempt, state, detail=None):
    return client.post(
        f"/api/tasks/{task['id']}/attempts/{attempt}/checkpoint",
        json={"state": state, "detail": detail},
        headers=worker,
    )


def fin(client: TestClient, task: dict) -> dict | None:
    return client.get(f"/api/tasks/{task['id']}").json()["finalization"]


def test_execution_then_failed_follow_up_is_not_success(client: TestClient) -> None:
    worker = register(client)
    task = claim(client, worker)
    assert fin(client, task)["step"] == "executing"
    # The claim is still open: a follow-up cannot have started.
    assert report(client, worker, task, 1, "running").status_code == 409

    done = client.post(
        f"/api/tasks/{task['id']}/complete",
        json={"attempt": 1, "result": {"summary": "ok"}},
        headers=worker,
    )
    assert done.status_code == 200, done.text
    f = fin(client, task)
    assert (f["step"], f["claim_end_action"], f["checkpoint"]) == (
        "finished",
        "complete",
        None,
    )
    assert f["execution_s"] is not None

    assert report(client, worker, task, 1, "saved").status_code == 409  # not started
    assert report(client, worker, task, 1, "running").status_code == 204
    assert report(client, worker, task, 1, "running").status_code == 204  # repeat
    f = fin(client, task)
    assert (f["step"], f["checkpoint"]["state"]) == ("finalizing_checkpoint", "running")
    assert f["checkpoint"]["duration_s"] is None

    assert (
        report(client, worker, task, 1, "failed", "env_checkpoint_limit").status_code
        == 204
    )
    f = fin(client, task)
    assert (f["step"], f["checkpoint"]["state"]) == ("finished", "failed")
    assert f["checkpoint"]["detail"] == "env_checkpoint_limit"
    assert f["checkpoint"]["duration_s"] is not None
    # Terminal and fixed: the same report repeats, a different one is refused.
    assert (
        report(client, worker, task, 1, "failed", "env_checkpoint_limit").status_code
        == 204
    )
    assert report(client, worker, task, 1, "saved").status_code == 409
    # The outcome of the attempt is untouched by the follow-up.
    assert client.get(f"/api/tasks/{task['id']}").json()["state"] == "succeeded"


def test_report_is_fenced_to_the_attempt_and_its_holder(client: TestClient) -> None:
    first = register(client)
    other = register(client)
    task = claim(client, first)
    client.post(f"/api/tasks/{task['id']}/release", json={"reason": "x"})
    assert (
        report(client, other, task, 1, "running").status_code == 409
    )  # not the holder
    assert (
        report(client, first, task, 2, "running").status_code == 404
    )  # no such attempt

    # A newer attempt (another worker) is claimed; the stale report only touches attempt 1.
    second = client.post("/api/tasks/claim", json={}, headers=other)
    assert second.status_code == 200, second.text
    assert second.json()["attempt"] == 2
    assert report(client, first, task, 1, "running").status_code == 204
    assert fin(client, task)["step"] == "executing"  # attempt 2 unaffected
    assert fin(client, task)["checkpoint"] is None


def test_expiry_and_release_give_no_execution_duration(client: TestClient) -> None:
    worker = register(client)
    task = claim(client, worker)
    client.post(f"/api/tasks/{task['id']}/release", json={"reason": "x"})
    f = fin(client, task)
    assert (f["step"], f["claim_end_action"], f["execution_s"]) == (
        "finished",
        "release",
        None,
    )


def test_silent_running_follow_up_is_unknown_not_a_result() -> None:
    now = datetime(2026, 11, 1, tzinfo=UTC)
    started = now - timedelta(seconds=CHECKPOINT_STALE_S)
    row = {
        "attempt": 1,
        "claimed_at": (started - timedelta(hours=1)).isoformat(),
        "claim_ended_at": started.isoformat(),
        "claim_end_action": "fail",
        "checkpoint_state": "running",
        "checkpoint_started_at": started.isoformat(),
        "checkpoint_ended_at": None,
        "checkpoint_detail": None,
    }
    assert derive(row, "ready", now).step == "finalizing_checkpoint"
    late = derive(row, "ready", now + timedelta(seconds=1))
    assert (late.step, late.checkpoint.state, late.checkpoint.duration_s) == (
        "unknown",
        "unknown",
        None,
    )
    # An attempt from before the migration has nothing recorded.
    legacy = {**row, "claim_ended_at": None, "checkpoint_state": None}
    assert derive(legacy, "succeeded", now) is None


def test_workspace_owner_and_checkpoint_status(client: TestClient) -> None:
    worker = register(client)
    agent_id = client.get("/api/agents").json()[0]["id"]
    ref = "checkpoint:abc123:" + "f" * 40
    task = claim(client, worker)

    def workspace(headers: dict[str, str] | None = None) -> dict:
        res = client.get(f"/api/tasks/{task['id']}", headers=headers or ADMIN)
        assert res.status_code == 200, res.text
        return res.json()["workspace"]

    assert workspace() == {
        "owner_agent_id": agent_id,
        "owner_node_id": None,
        "owner_status": "live",
        "checkpoint": "unknown",
    }
    client.post(
        f"/api/tasks/{task['id']}/complete",
        json={"attempt": 1, "result": {"summary": "ok"}},
        headers=worker,
    )
    assert workspace()["checkpoint"] == "unknown"  # no checkpoint step recorded
    report(client, worker, task, 1, "running")
    assert workspace()["checkpoint"] == "unknown"
    report(client, worker, task, 1, "saved", ref)
    assert workspace()["checkpoint"] == "available"
    # The worker's own read shows no reference, detail or path-like value.
    body = client.get(f"/api/tasks/{task['id']}", headers=worker).json()["workspace"]
    assert body == {
        "owner_agent_id": agent_id,
        "owner_node_id": None,
        "owner_status": "live",
        "checkpoint": "available",
    }
    assert "checkpoint:" not in str(body) and "/" not in str(body)


def test_workspace_is_null_without_an_attempt(client: TestClient) -> None:
    task = client.post("/api/tasks", json={"project": "demo", "title": "t"}).json()
    assert client.get(f"/api/tasks/{task['id']}").json()["workspace"] is None


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _attempt_row(**overrides: object) -> dict[str, object]:
    """A newest-attempt row as the join returns it, with a live owner by default."""
    row: dict[str, object] = {
        "agent_id": "agt_1",
        "checkpoint_state": None,
        "owner_state": "active",
        "owner_last_heartbeat_at": NOW.isoformat(),
        "owner_runtime": None,
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("state", "status"),
    [
        ("saved", "available"),
        ("failed", "unavailable"),
        ("nothing_to_save", "unavailable"),
        ("running", "unknown"),
        (None, "unknown"),
        ("from_a_newer_version", "unknown"),
    ],
)
def test_workspace_facts_maps_checkpoint_state(state: str | None, status: str) -> None:
    facts = workspace_facts(_attempt_row(checkpoint_state=state), NOW)
    assert facts is not None
    assert (facts.owner_agent_id, facts.checkpoint) == ("agt_1", status)
    assert workspace_facts(None, NOW) is None


@pytest.mark.parametrize(
    ("state", "heartbeat", "status"),
    [
        ("active", NOW.isoformat(), "live"),
        ("draining", NOW.isoformat(), "live"),
        ("active", (NOW - timedelta(seconds=LOST_AFTER_S + 1)).isoformat(), "stale"),
        ("active", None, "stale"),
        ("gone", NOW.isoformat(), "ended"),
        ("lost", NOW.isoformat(), "ended"),
        (None, NOW.isoformat(), "unknown"),
        ("from_a_newer_version", NOW.isoformat(), "unknown"),
    ],
)
def test_workspace_facts_maps_owner_status(
    state: str | None, heartbeat: str | None, status: str
) -> None:
    facts = workspace_facts(
        _attempt_row(owner_state=state, owner_last_heartbeat_at=heartbeat), NOW
    )
    assert facts is not None
    assert facts.owner_status == status


def test_workspace_facts_maps_owner_node_id() -> None:
    reported = workspace_facts(
        _attempt_row(owner_runtime=json.dumps({"node_id": "host-a"})), NOW
    )
    assert reported is not None and reported.owner_node_id == "host-a"
    for runtime in (None, "", "not json", json.dumps({}), json.dumps({"node_id": ""})):
        facts = workspace_facts(_attempt_row(owner_runtime=runtime), NOW)
        assert facts is not None and facts.owner_node_id is None


def test_claim_offers_every_earlier_attempts_workspace_owner(
    client: TestClient,
) -> None:
    first = register(client)
    second = register(client)
    task = claim(client, first)
    assert task["prior_workspaces"] == []  # a first attempt has no earlier work
    first_id = client.get(f"/api/tasks/{task['id']}").json()["workspace"][
        "owner_agent_id"
    ]
    assert report(client, first, task, 1, "running").status_code == 409  # still open
    client.post(f"/api/tasks/{task['id']}/release", json={"reason": "x"})
    assert report(client, first, task, 1, "running").status_code == 204
    retry = client.post("/api/tasks/claim", json={}, headers=second).json()
    assert retry["attempt"] == 2
    # Attempt 1's follow-up never finished: the claim says so, not "nothing to save".
    assert retry["prior_workspaces"] == [
        {
            "attempt": 1,
            "owner_agent_id": first_id,
            "owner_node_id": None,
            "owner_status": "live",
            "checkpoint_state": "running",
        }
    ]
    assert report(client, first, task, 1, "nothing_to_save").status_code == 204
    # The report that landed after the claim shows on a re-read, for the same attempt.
    reread = client.get(
        f"/api/tasks/{task['id']}/prior-workspaces", params={"attempt": 2}
    )
    assert [p["checkpoint_state"] for p in reread.json()] == ["nothing_to_save"]
    client.post(f"/api/tasks/{task['id']}/release", json={"reason": "x"})
    third = client.post("/api/tasks/claim", json={}, headers=first).json()
    assert third["attempt"] == 3
    assert [
        (p["attempt"], p["checkpoint_state"]) for p in third["prior_workspaces"]
    ] == [(1, "nothing_to_save"), (2, None)]

    # Operator retry resets attempt to 1, but earlier generations stay in prior_workspaces.
    gen1 = claim(client, first, max_attempts=1)
    assert gen1["attempt"] == 1
    assert gen1["prior_workspaces"] == []
    failed = client.post(
        f"/api/tasks/{gen1['id']}/fail",
        json={"attempt": 1, "error": "boom"},
        headers=first,
    )
    assert failed.status_code == 200 and failed.json()["state"] == "failed"
    assert report(client, first, gen1, 1, "running").status_code == 204
    assert (
        report(client, first, gen1, 1, "failed", "env_checkpoint_limit").status_code
        == 204
    )
    retried = client.post(f"/api/tasks/{gen1['id']}/retry")
    assert retried.status_code == 200 and retried.json()["attempt"] == 0

    gen2 = client.post("/api/tasks/claim", json={}, headers=second).json()
    assert gen2["id"] == gen1["id"]
    assert gen2["attempt"] == 1
    assert gen2["prior_workspaces"] == [
        {
            "attempt": 1,
            "owner_agent_id": first_id,
            "owner_node_id": None,
            "owner_status": "live",
            "checkpoint_state": "failed",
        }
    ]

    # A second retry generation keeps every earlier attempt-1 row in claim order.
    second_id = client.get(f"/api/tasks/{gen1['id']}").json()["workspace"][
        "owner_agent_id"
    ]
    assert (
        client.post(
            f"/api/tasks/{gen1['id']}/fail",
            json={"attempt": 1, "error": "boom2"},
            headers=second,
        ).status_code
        == 200
    )
    assert report(client, second, gen2, 1, "running").status_code == 204
    assert report(client, second, gen2, 1, "nothing_to_save").status_code == 204
    assert client.post(f"/api/tasks/{gen1['id']}/retry").status_code == 200
    gen3 = client.post("/api/tasks/claim", json={}, headers=first).json()
    assert gen3["attempt"] == 1
    assert [
        (p["attempt"], p["owner_agent_id"], p["checkpoint_state"])
        for p in gen3["prior_workspaces"]
    ] == [(1, first_id, "failed"), (1, second_id, "nothing_to_save")]


def attest(
    client: TestClient,
    task_id: str,
    attempt: int,
    reason: str,
    *,
    headers: dict[str, str] | None = None,
    owner_agent_id: str | None = None,
    override_failed: bool = False,
):
    body: dict[str, object] = {"reason": reason}
    if override_failed:
        body["override_failed"] = True
    if owner_agent_id is not None:
        body["owner_agent_id"] = owner_agent_id
    return client.post(
        f"/api/tasks/{task_id}/attempts/{attempt}/checkpoint/attest",
        json=body,
        headers=headers or ADMIN,
    )


def test_admin_attestation_sets_nothing_to_save_and_audits_and_refuses_unsafe_cases(
    client: TestClient,
) -> None:
    first = register(client)
    second = register(client)
    orchestrator = register(client, "orchestrator")
    task = claim(client, first, max_attempts=3)
    task_id = task["id"]
    first_id = client.get(f"/api/tasks/{task_id}").json()["workspace"]["owner_agent_id"]

    # Refused while claim is open.
    open_res = attest(client, task_id, 1, "legacy ended attempt")
    assert open_res.status_code == 409
    assert open_res.json()["detail"]["code"] == "claim_open"

    # End the claim without any checkpoint report; owner is still live -> refused.
    released = client.post(f"/api/tasks/{task_id}/release", json={"reason": "requeue"})
    assert released.status_code == 200
    assert attest(client, task_id, 1, "x", override_failed=True).status_code == 409
    live_res = attest(client, task_id, 1, "owner still running")
    assert live_res.status_code == 409
    assert live_res.json()["detail"]["code"] == "owner_live"

    # End the owner agent (simulating instance replacement before the fix).
    assert client.post("/api/agents/me/leave", headers=first).status_code == 200

    # Refused for non-admin callers (worker and orchestrator tokens) and blank reason.
    assert attest(client, task_id, 1, "not admin", headers=second).status_code == 403
    assert (
        attest(client, task_id, 1, "not admin", headers=orchestrator).status_code == 403
    )
    assert attest(client, task_id, 1, "   ").status_code == 422

    # Admin attestation succeeds, records actor and reason in audit events, and unblocks rework.
    reason = "verified PR #42 merged and ephemeral instance replaced"
    ok = attest(client, task_id, 1, reason)
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["task_id"] == task_id
    assert body["attempt"] == 1
    assert body["owner_agent_id"] == first_id
    assert body["owner_status"] == "ended"
    assert body["checkpoint_state"] == "nothing_to_save"
    assert body["actor"] == "admin:preset"
    assert body["reason"] == reason

    task_read = client.get(f"/api/tasks/{task_id}").json()
    assert task_read["state"] == "ready"
    assert task_read["finalization"]["checkpoint"]["state"] == "nothing_to_save"
    assert "admin:preset" in task_read["finalization"]["checkpoint"]["detail"]

    ev = [
        e
        for e in client.get("/api/events", params={"subject_id": task_id}).json()
        if e["kind"] == "task.checkpoint_attested"
    ]
    assert len(ev) == 1
    assert ev[0]["actor"] == "admin:preset"
    assert ev[0]["data"]["reason"] == reason
    assert ev[0]["data"]["attempt"] == 1
    assert ev[0]["data"]["owner_agent_id"] == first_id
    assert ev[0]["data"]["checkpoint_state"] == "nothing_to_save"

    # Already-recorded state (nothing_to_save) is refused on repeat.
    again = attest(client, task_id, 1, "repeat")
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "checkpoint_already_recorded"

    # Next cross-instance claim sees attempt 1 proven as nothing_to_save.
    rework = client.post("/api/tasks/claim", json={}, headers=second).json()
    assert rework["attempt"] == 2
    assert rework["prior_workspaces"] == [
        {
            "attempt": 1,
            "owner_agent_id": first_id,
            "owner_node_id": None,
            "owner_status": "ended",
            "checkpoint_state": "nothing_to_save",
        }
    ]

    # Refused when a prior checkpoint state (running / saved / failed) is already recorded.
    assert (
        client.post(
            f"/api/tasks/{task_id}/fail",
            json={"attempt": 2, "error": "boom"},
            headers=second,
        ).status_code
        == 200
    )
    assert report(client, second, task, 2, "running").status_code == 204
    assert (
        report(
            client, second, task, 2, "failed", "env_checkpoint_store_missing"
        ).status_code
        == 204
    )
    assert client.post("/api/agents/me/leave", headers=second).status_code == 200
    recorded = attest(client, task_id, 2, "cannot override failed")
    assert recorded.status_code == 409
    assert recorded.json()["detail"]["code"] == "checkpoint_already_recorded"

    # `failed` alone is superseded only with the explicit flag; the audit keeps what it replaced.
    done = attest(
        client, task_id, 2, "unpublished commit is on the forge", override_failed=True
    )
    assert done.status_code == 200
    assert done.json()["previous_state"] == "failed"
    audit = [
        e
        for e in client.get(
            "/api/events", params={"subject_id": task_id}, headers=ADMIN
        ).json()
        if e["kind"] == "task.checkpoint_attested" and e["data"]["attempt"] == 2
    ]
    assert audit[0]["data"]["previous_state"] == "failed"
    assert audit[0]["data"]["previous_detail"] == "env_checkpoint_store_missing"
    # A superseded state is nothing_to_save, which the flag does not replace.
    again = attest(client, task_id, 2, "twice", override_failed=True)
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "checkpoint_already_recorded"
