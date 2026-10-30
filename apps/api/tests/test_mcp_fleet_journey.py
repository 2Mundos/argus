"""The fleet journey over MCP alone (MCP operations 1, 3.3).

Inspect what runs, recycle an instance without interrupting its work, wait for
the effect instead of polling, change an option safely, and read it all back.
The reconciler passes are driven by the test against a fake target.
"""

from collections.abc import Generator
from pathlib import Path

import pytest
from api_support import FakeTarget
from argus_api.fleet.reconciler import FleetSettings, reconcile_pool
from argus_api.main import app
from fastapi.testclient import TestClient
from test_mcp import ADMIN_TOKEN, register, tool

ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ARGUS_RECONCILE_INTERVAL_S", "3600")  # the test drives it
    with TestClient(app) as c:
        yield c


def test_fleet_journey_over_mcp(client: TestClient) -> None:
    target = FakeTarget()
    client.app.state.targets.override("docker", None, target)
    settings = FleetSettings(
        argus_url="http://argus.test:8000", max_starts_in_flight=64
    )

    def reconcile() -> None:
        async def once() -> None:
            await reconcile_pool(
                client.app.state.db, "web", client.app.state.targets, settings
            )

        client.portal.call(once)

    client.put(
        "/api/pools/web",
        json={
            "image": "w:1",
            "desired_replicas": 1,
            "max_replicas": 2,
            "env": {"KEY": "s3cret"},
        },
        headers=ADMIN,
    ).raise_for_status()
    reconcile()
    reconcile()
    [ref] = target.containers
    boot = target.specs[ref].env["ARGUS_BOOTSTRAP_TOKENS"].split(",")[0]
    worker = client.post("/api/agents/register", json={"bootstrap_token": boot}).json()[
        "token"
    ]
    tool(
        client,
        worker,
        "heartbeat",
        runtime={"worker_version": "1.2", "engine": "codex"},
    )
    orch = register(client, "orchestrator")

    # Inspect: what runs, and how each fact is known.
    pool = tool(client, orch, "inspect_pool", name="web")
    assert pool["pool"]["env"] == {"KEY": "***"}
    [inst] = pool["instances"]
    runtime = inst["runtime"]
    assert runtime["image_digest"] == {"value": "sha256:w:1", "source": "started"}
    assert runtime["worker_version"] == {"value": "1.2", "source": "validated"}
    assert runtime["executor"] == {"value": None, "source": "unknown"}
    assert runtime["ready"] == {"value": True, "source": "validated"}

    # Recycle while the worker holds a task: nothing is interrupted.
    cursor = tool(client, orch, "wait_for_events", wait_s=0)["next_cursor"]
    tool(client, orch, "create_task", title="t", project="global")
    claimed = tool(client, worker, "claim_task", wait_s=0)["task"]
    assert tool(client, orch, "recycle_instance", instance_id=inst["id"])["draining"]
    reconcile()
    seen = tool(
        client,
        orch,
        "wait_for_events",
        after=cursor,
        wait_s=0,
        subject_type="instance",
        subject_id=inst["id"],
    )
    assert [e["kind"] for e in seen["events"]] == ["instance.recycling"]
    assert tool(client, orch, "get_instance", instance_id=inst["id"])["state"] == (
        "running"
    )

    tool(
        client,
        worker,
        "complete_task",
        task_id=claimed["id"],
        attempt=claimed["attempt"],
        summary="done",
    )
    reconcile()  # idle now: stop it
    reconcile()  # confirmed gone; the replacement is reserved and started
    done = tool(
        client,
        orch,
        "wait_for_events",
        after=seen["next_cursor"],
        wait_s=0,
        subject_type="instance",
        subject_id=inst["id"],
    )
    assert [e["kind"] for e in done["events"]] == [
        "instance.terminating",
        "instance.terminated",
    ]
    [fresh] = tool(client, orch, "inspect_pool", name="web")["instances"]
    assert fresh["id"] != inst["id"]
    assert tool(client, orch, "get_task", task_id=claimed["id"])["state"] == (
        "succeeded"
    )

    # Change an option safely: admins only, guarded by the version read.
    version = tool(client, orch, "inspect_pool", name="web")["pool"]["version"]
    refused = tool(client, orch, "update_pool", name="web", expected_version=version)
    assert "error" in refused
    updated = tool(
        client,
        ADMIN_TOKEN,
        "update_pool",
        name="web",
        expected_version=version,
        desired_replicas=2,
    )
    assert (updated["desired_replicas"], updated["env"]) == (2, {"KEY": "***"})
    reconcile()
    after = tool(client, orch, "inspect_pool", name="web")
    assert len(after["instances"]) == 2
    assert {i["runtime"]["image"]["source"] for i in after["instances"]} == {
        "requested"
    }
