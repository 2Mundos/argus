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
        # How worker containers/pods reach this process: host.docker.internal on
        # Docker Desktop; on Linux CI, the Docker network's gateway address.
        "ARGUS_PUBLIC_URL": (
            f"http://{os.environ.get('ARGUS_IT_HOST', 'host.docker.internal')}:{port}"
        ),
        "ARGUS_RECONCILE_INTERVAL_S": "1",
        # Socket-mounted NanoClaw pools need the process-level opt-in (PUT path).
        "ARGUS_NANOCLAW_ALLOW_DOCKER_SOCKET": "true",
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
        argus.post("/tasks", json={"project": "demo", "title": f"job {n}"}).json()["id"]
        for n in range(4)
    ]

    def all_done() -> bool:
        return all(argus.get(f"/tasks/{i}").json()["state"] == "succeeded" for i in ids)

    wait_for(all_done, 60, "tasks to complete")
    assert argus.get(f"/tasks/{ids[0]}").json()["result"]["summary"] == "echo: job 0"

    argus.post(f"/pools/{POOL}/scale", json={"desired_replicas": 0})
    wait_for(lambda: not active_agents(), 60, "workers to drain and leave")
    wait_for(lambda: not docker_ps(), 60, "containers to be removed")

    def instances_now() -> list[dict]:
        return argus.get(
            "/instances", params={"pool": POOL, "include_terminated": True}
        ).json()

    # The reconciler records the end on its next tick, after the containers go.
    wait_for(
        lambda: [i["state"] for i in instances_now()] == ["terminated"] * 2,
        30,
        "instances to be recorded as terminated",
    )
    instances = instances_now()
    # Scale-down drains the workers; the reconciler ends an idle drained instance
    # as "drained" unless the worker leaves ("all agents left") or its container
    # exits cleanly first. Any other reason (a crash, a lost agent) fails.
    assert {i["exit_reason"] for i in instances} <= {
        "drained",
        "all agents left",
        "exited (0)",
    }


NANOCLAW_DIR = os.environ.get("ARGUS_NANOCLAW_INSTALL_DIR")
NANOCLAW_AGENT_IMAGE = os.environ.get("ARGUS_NANOCLAW_AGENT_IMAGE")


@pytest.mark.skipif(
    not (NANOCLAW_DIR and NANOCLAW_AGENT_IMAGE),
    reason="set ARGUS_NANOCLAW_INSTALL_DIR (built NanoClaw with the Argus channel)"
    " and ARGUS_NANOCLAW_AGENT_IMAGE",
)
def test_nanoclaw_runtime_registers_and_leaves_nothing_behind(
    argus: httpx.Client,
) -> None:
    import tempfile

    subprocess.run(
        [
            "docker",
            "build",
            "-q",
            "-t",
            "argus-nanoclaw-host:test",
            str(ROOT / "deploy/nanoclaw-host"),
        ],
        check=True,
        capture_output=True,
    )
    root = tempfile.mkdtemp(dir="/tmp", prefix="nci-")  # short: Unix socket paths
    os.chmod(root, 0o777)
    sock_gid = str(os.stat("/var/run/docker.sock").st_gid)
    pool = "it-nc"
    body = {
        "runtime": "nanoclaw",
        "image": "argus-nanoclaw-host:test",
        "desired_replicas": 1,
        "max_replicas": 1,
        "docker": {"extra_hosts": ["host.docker.internal:host-gateway"]},
        "nanoclaw": {
            "install_dir": NANOCLAW_DIR,
            "instances_root": root,
            "agent_image": NANOCLAW_AGENT_IMAGE,
            "user": f"{os.getuid()}:{os.getgid()}",
            "group_add": [sock_gid],
        },
    }

    def host_containers() -> list[str]:
        out = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label=argus.pool={pool}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.split()

    def agent_containers() -> list[str]:
        out = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"ancestor={NANOCLAW_AGENT_IMAGE}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.split()

    try:
        assert argus.put(f"/pools/{pool}", json=body).status_code == 200
        wait_for(
            lambda: len(argus.get("/agents", params={"state": "active"}).json()) >= 1,
            90,
            "the NanoClaw host to register with Argus",
        )
        # A task makes NanoClaw spawn an agent container (its model call will fail
        # without a credential; the container is what we are checking).
        argus.post("/tasks", json={"project": "demo", "title": "nanoclaw it"})
        wait_for(lambda: bool(agent_containers()), 60, "an agent container")

        [inst] = argus.get("/instances", params={"pool": pool}).json()
        assert argus.post(f"/instances/{inst['id']}/terminate").status_code == 200
        argus.put(f"/pools/{pool}", json={**body, "desired_replicas": 0})
        wait_for(lambda: not host_containers(), 60, "the host container to go")
        wait_for(lambda: not agent_containers(), 60, "the agent container to go too")
    finally:
        argus.put(f"/pools/{pool}", json={**body, "desired_replicas": 0})
        for ids in (host_containers(), agent_containers()):
            if ids:
                subprocess.run(["docker", "rm", "-f", *ids], capture_output=True)
        subprocess.run(["rm", "-rf", root], check=False)


K8S_CONTEXT = os.environ.get("ARGUS_K8S_CONTEXT")
K8S_NAMESPACE = os.environ.get("ARGUS_K8S_NAMESPACE", "workers")
KIND_CLUSTER = os.environ.get("ARGUS_KIND_CLUSTER")  # load the image into kind


def _k8s_diagnostics(api, argus: httpx.Client, pool: str) -> str:
    """Why pods did not register: pod phase and container state, events, instances."""
    lines = []
    for pod in api.list_namespaced_pod(K8S_NAMESPACE).items:
        statuses = (pod.status.container_statuses if pod.status else None) or []
        states = [
            cs.state.waiting or cs.state.terminated or cs.state.running
            for cs in statuses
            if cs.state
        ]
        lines.append(f"pod {pod.metadata.name}: {pod.status.phase} {states}")
    lines += [
        f"event {ev.involved_object.name}: {ev.reason}: {ev.message}"
        for ev in api.list_namespaced_event(K8S_NAMESPACE).items
    ]
    lines += [
        f"instance {i['id']}: {i['state']} {i.get('exit_reason')}"
        for i in argus.get("/instances", params={"pool": pool}).json()
    ]
    return "\n".join(lines)


@pytest.mark.skipif(
    not K8S_CONTEXT,
    reason="set ARGUS_K8S_CONTEXT (a kubeconfig context; use an isolated cluster such as kind)",
)
def test_kubernetes_target_scales_runs_work_and_cleans_up(argus: httpx.Client) -> None:
    """Pods as instances: scale up, do work, an orphan pod is removed, scale down."""
    from kubernetes import client, config

    if KIND_CLUSTER:
        subprocess.run(
            ["kind", "load", "docker-image", IMAGE, "--name", KIND_CLUSTER],
            check=True,
            capture_output=True,
        )
    api = client.CoreV1Api(config.new_client_from_config(context=K8S_CONTEXT))
    try:  # the namespace is the test's to create on a throwaway cluster
        api.create_namespace({"metadata": {"name": K8S_NAMESPACE}})
    except Exception as exc:
        if getattr(exc, "status", None) != 409:
            raise
    pool = "it-k8s"

    def pods() -> list[str]:
        found = api.list_namespaced_pod(
            K8S_NAMESPACE, label_selector=f"argus.pool={pool}"
        )
        return [p.metadata.name for p in found.items]

    body = {
        "target": "kubernetes",
        "image": IMAGE,
        "desired_replicas": 2,
        "max_replicas": 2,
        "env": {"ECHO_WORK_SECONDS": "0.5"},
        "kubernetes": {"context": K8S_CONTEXT, "namespace": K8S_NAMESPACE},
    }
    try:
        assert argus.put(f"/pools/{pool}", json=body).status_code == 200
        try:
            wait_for(
                lambda: (
                    len(argus.get("/agents", params={"state": "active"}).json()) == 2
                ),
                120,
                "two pods to register",
            )
        except AssertionError as exc:
            raise AssertionError(
                f"{exc}\n{_k8s_diagnostics(api, argus, pool)}"
            ) from exc
        assert len(pods()) == 2

        ids = [
            argus.post(
                "/tasks", json={"project": "demo", "title": f"k8s job {n}"}
            ).json()["id"]
            for n in range(3)
        ]
        wait_for(
            lambda: all(
                argus.get(f"/tasks/{i}").json()["state"] == "succeeded" for i in ids
            ),
            60,
            "tasks to complete on the pods",
        )

        # The logs endpoint returns decoded text from the pod.
        [first, *_] = argus.get("/instances", params={"pool": pool}).json()
        text = argus.get(f"/instances/{first['id']}/logs").text
        assert "registered as" in text
        assert not text.startswith("b'")

        # An orphan (our labels, no instance row) is stopped by the reconciler.
        api.create_namespaced_pod(
            K8S_NAMESPACE,
            {
                "metadata": {
                    "name": "argus-orphan-test",
                    "labels": {
                        "argus.managed": "true",
                        "argus.pool": pool,
                        "argus.instance_id": "00000000-0000-0000-0000-000000000000",
                    },
                },
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {"name": "c", "image": IMAGE, "command": ["sleep", "300"]}
                    ],
                },
            },
        )
        wait_for(
            lambda: "argus-orphan-test" not in pods(),
            60,
            "the orphan pod to be removed",
        )

        argus.post(f"/pools/{pool}/scale", json={"desired_replicas": 0})
        wait_for(lambda: not pods(), 90, "pods to be removed")
        wait_for(
            lambda: all(
                i["state"] == "terminated"
                for i in argus.get(
                    "/instances", params={"pool": pool, "include_terminated": True}
                ).json()
            ),
            60,
            "instances to be confirmed terminated",
        )
    finally:
        argus.put(f"/pools/{pool}", json={**body, "desired_replicas": 0})
        for name in pods():
            api.delete_namespaced_pod(name, K8S_NAMESPACE, grace_period_seconds=0)


BRIDGE_NODE_IMAGE = os.environ.get("ARGUS_BRIDGE_SMOKE_IMAGE", "node:22-slim")


def test_custom_bridge_published_port_reaches_agent_containers() -> None:
    """The custom-harness bridge runs in the NanoClaw host container, on its own
    network; agent containers run on the default bridge. Started the way the fleet
    starts a NanoClaw host (DockerTarget, published on HOST_GATEWAY), the bridge must
    answer an agent container at host.docker.internal:<published port>, and refuse a
    request without its key."""
    import json as _json
    import uuid

    from argus_api.fleet.runtimes import CUSTOM_BRIDGE_PORT
    from argus_api.fleet.targets import HOST_GATEWAY, ContainerSpec, DockerTarget, Mount

    engines = ROOT / "integrations/nanoclaw/engines"
    net = f"argus-it-bridge-{uuid.uuid4().hex[:8]}"
    key = uuid.uuid4().hex + uuid.uuid4().hex
    subprocess.run(
        ["docker", "network", "create", net], check=True, capture_output=True
    )
    target = DockerTarget(host=None)
    spec = ContainerSpec(
        instance_id=f"it-bridge-{uuid.uuid4().hex[:8]}",
        image=BRIDGE_NODE_IMAGE,
        env={},
        labels={"argus.it": "bridge"},
        network=net,
        mounts=[Mount(str(engines), "/engines", "ro")],
        ports={f"{CUSTOM_BRIDGE_PORT}/tcp": (HOST_GATEWAY, None)},
    )
    try:
        subprocess.run(
            ["docker", "pull", "-q", BRIDGE_NODE_IMAGE], check=True, capture_output=True
        )
        # The image's default command is node; run the smoke server instead.
        spec_cmd = [
            "node",
            "/engines/bridge-smoke-server.mjs",
            str(CUSTOM_BRIDGE_PORT),
            key,
        ]
        client = target._docker()
        client.containers.run(
            spec.image,
            spec_cmd,
            name=spec.name,
            detach=True,
            network=spec.network,
            volumes={str(engines): {"bind": "/engines", "mode": "ro"}},
            ports=target._ports(client, spec.ports),
            labels=spec.labels,
        )
        wait_for(
            lambda: (
                "bridge-smoke listening"
                in subprocess.run(
                    ["docker", "logs", spec.name], capture_output=True, text=True
                ).stdout
            ),
            60,
            "the smoke bridge",
        )
        published = subprocess.run(
            ["docker", "port", spec.name, f"{CUSTOM_BRIDGE_PORT}/tcp"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        # Same rule as the NanoClaw side (parsePublishedPort): first line with a port.
        host_port = next(
            line.rsplit(":", 1)[1] for line in published.splitlines() if ":" in line
        )
        probe = (
            "const u='http://host.docker.internal:" + host_port + "/v1/messages';"
            "const body=JSON.stringify({model:'smoke',max_tokens:8,"
            "messages:[{role:'user',content:'hi'}]});"
            "const h={'content-type':'application/json'};"
            "const a=await fetch(u,{method:'POST',headers:h,body});"
            "const b=await fetch(u,{method:'POST',headers:{...h,'x-argus-bridge-key':process.argv[1]},body});"
            "console.log(JSON.stringify({nokey:a.status,key:b.status,body:await b.json()}));"
        )
        out = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "bridge",
                "--add-host",
                "host.docker.internal:host-gateway",
                BRIDGE_NODE_IMAGE,
                "node",
                "--input-type=module",
                "-e",
                probe,
                key,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        result = _json.loads(out.stdout.strip().splitlines()[-1])
        assert result["nokey"] == 401
        assert result["key"] == 200
        assert "bridge-ok" in _json.dumps(result["body"])
    finally:
        subprocess.run(["docker", "rm", "-f", spec.name], capture_output=True)
        subprocess.run(["docker", "network", "rm", net], capture_output=True)
