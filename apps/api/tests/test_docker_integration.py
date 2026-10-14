"""Real Docker: Argus scales a pool of echo workers up, they do work, it scales
them down. Opt-in (needs a Docker daemon): ARGUS_DOCKER_TESTS=1 / make test-docker.
"""

import os
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path

import httpx2 as httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ARGUS_DOCKER_TESTS") != "1", reason="set ARGUS_DOCKER_TESTS=1"
)

ROOT = Path(__file__).resolve().parents[3]
IMAGE = "argus-echo-worker:test"
TOKEN = "docker-it-admin-token-" + "d" * 32
POOL = "it-echo"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def wait_for(check: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


def docker_ps() -> list[str]:
    out = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=argus.pool={POOL}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


@pytest.fixture(scope="module")
def argus(tmp_path_factory: pytest.TempPathFactory) -> Generator[httpx.Client]:
    import uvicorn

    subprocess.run(
        ["docker", "build", "-q", "-t", IMAGE, str(ROOT / "examples/echo-worker")],
        check=True,
        capture_output=True,
    )
    port = free_port()
    os.environ |= {
        "ARGUS_DATA_DIR": str(tmp_path_factory.mktemp("argus")),
        "ARGUS_ADMIN_TOKEN": TOKEN,
        "ARGUS_PUBLIC_URL": f"http://host.docker.internal:{port}",
        "ARGUS_RECONCILE_INTERVAL_S": "1",
    }
    from argus_api.main import app

    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{port}/api",
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=10,
    )
    wait_for(lambda: server.started, 20, "Argus to start")
    try:
        yield client
    finally:
        client.put(f"/pools/{POOL}", json=_pool(0))
        server.should_exit = True
        thread.join(timeout=30)
        if ids := docker_ps():
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True)


def _pool(desired: int) -> dict:
    return {
        "image": IMAGE,
        "desired_replicas": desired,
        "max_replicas": 3,
        "env": {"ECHO_WORK_SECONDS": "0.5"},
        "docker": {"extra_hosts": ["host.docker.internal:host-gateway"]},
    }


def test_scale_up_work_and_scale_down(argus: httpx.Client) -> None:
    assert argus.put(f"/pools/{POOL}", json=_pool(2)).status_code == 200

    def active_agents() -> list[dict]:
        return argus.get("/agents", params={"state": "active"}).json()

    wait_for(lambda: len(active_agents()) == 2, 90, "two workers to register")
    assert len(docker_ps()) == 2

    ids = [
        argus.post("/tasks", json={"project": "global", "title": f"job {n}"}).json()[
            "id"
        ]
        for n in range(4)
    ]

    def all_done() -> bool:
        return all(argus.get(f"/tasks/{i}").json()["state"] == "succeeded" for i in ids)

    wait_for(all_done, 60, "tasks to complete")
    assert argus.get(f"/tasks/{ids[0]}").json()["result"]["summary"] == "echo: job 0"

    argus.post(f"/pools/{POOL}/scale", json={"desired_replicas": 0})
    wait_for(lambda: not active_agents(), 60, "workers to drain and leave")
    wait_for(lambda: not docker_ps(), 60, "containers to be removed")
    instances = argus.get(
        "/instances", params={"pool": POOL, "include_terminated": True}
    ).json()
    assert [i["state"] for i in instances] == ["terminated", "terminated"]
    assert {i["exit_reason"] for i in instances} <= {"all agents left", "exited (0)"}
