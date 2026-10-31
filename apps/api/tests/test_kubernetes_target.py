"""KubernetesTarget against an in-memory CoreV1Api (no cluster needed)."""

import json
import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from argus_api.fleet.targets import (
    ContainerSpec,
    KubernetesPlacement,
    KubernetesTarget,
    TargetError,
    TargetRegistry,
    pod_manifest,
    pod_status,
)
from argus_core.fleet import PoolUpsert
from kubernetes.client.exceptions import ApiException
from pydantic import ValidationError

ECHO_DOCKERFILE = (
    Path(__file__).resolve().parents[3] / "examples/echo-worker/Dockerfile"
)

NS_NAME = "workers"


def spec(**kw) -> ContainerSpec:
    base = {
        "instance_id": "11111111-2222-3333-4444-555555555555",
        "image": "worker:1",
        "env": {"ARGUS_URL": "http://argus", "ARGUS_BOOTSTRAP_TOKENS": "t"},
        "labels": {
            "argus.managed": "true",
            "argus.pool": "web",
            "argus.instance_id": "11111111-2222-3333-4444-555555555555",
        },
        "kubernetes": KubernetesPlacement(NS_NAME),
    }
    return ContainerSpec(**{**base, **kw})


def pod(name: str, phase: str = "Running", labels=None, exit_code=None, waiting=None):
    cs = []
    if exit_code is not None:
        cs = [NS(state=NS(terminated=NS(exit_code=exit_code), waiting=None))]
    elif waiting:
        cs = [
            NS(
                state=NS(
                    terminated=None, waiting=NS(reason=waiting, message="back-off")
                )
            )
        ]
    return NS(
        metadata=NS(name=name, labels=labels or {}),
        status=NS(phase=phase, container_statuses=cs, conditions=[]),
    )


class FakeCoreV1:
    def __init__(self) -> None:
        self.pods: dict[str, NS] = {}
        self.created: list[dict] = []
        self.deleted: list[tuple[str, int]] = []
        self.fail_with: ApiException | None = None

    def create_namespaced_pod(self, namespace, body):
        if self.fail_with:
            raise self.fail_with
        name = body["metadata"]["name"]
        if name in self.pods:
            raise ApiException(status=409, reason="AlreadyExists", http_resp=None)
        self.created.append(body)
        self.pods[name] = pod(name, labels=body["metadata"]["labels"])

    def delete_namespaced_pod(self, name, namespace, grace_period_seconds=None):
        if name not in self.pods:
            raise ApiException(status=404, reason="NotFound", http_resp=None)
        self.deleted.append((name, grace_period_seconds))
        # Deletion takes time: the pod lingers (still Running) until removed.

    def list_namespaced_pod(self, namespace, label_selector=""):
        want = dict(
            p.split("=") if "=" in p else (p, None) for p in label_selector.split(",")
        )
        items = [
            p
            for p in self.pods.values()
            if all(
                k in p.metadata.labels and (v is None or p.metadata.labels[k] == v)
                for k, v in want.items()
            )
        ]
        return NS(items=items)

    def read_namespaced_pod(self, name, namespace):
        return self.pods[name]

    def read_namespaced_pod_log(
        self, name, namespace, tail_lines=None, timestamps=None, _preload_content=True
    ):
        p = self.pods.get(name)
        if p is None:
            raise ApiException(status=404, reason="NotFound", http_resp=None)
        if p.status.phase == "Pending":
            raise ApiException(status=400, reason="BadRequest", http_resp=None)
        body = b"2026-01-01T00:00:00Z hello\n"
        # Like the real client: the raw response, not a decoded string.
        return body if _preload_content else NS(data=body)


@pytest.fixture
def api() -> FakeCoreV1:
    return FakeCoreV1()


@pytest.fixture
def target(api: FakeCoreV1) -> KubernetesTarget:
    return KubernetesTarget("kind-test", NS_NAME, api=api)


def test_echo_worker_image_runs_as_a_numeric_user() -> None:
    # The pod sets runAsNonRoot without runAsUser, so the kubelet can only verify
    # a numeric image USER; a name such as `nobody` leaves the pod unstartable
    # (CreateContainerConfigError) and the worker never registers.
    users = re.findall(r"^USER\s+(\S+)", ECHO_DOCKERFILE.read_text(), re.M)
    assert users
    assert re.fullmatch(r"[1-9]\d*(:\d+)?", users[-1]), users[-1]
    assert pod_manifest(spec(), NS_NAME)["spec"]["securityContext"]["runAsNonRoot"]


def test_manifest_is_a_hardened_one_shot_pod() -> None:
    m = pod_manifest(
        spec(
            cpus=0.5,
            memory_mb=256,
            kubernetes=KubernetesPlacement(
                NS_NAME,
                service_account="worker",
                image_pull_secrets=["regcred"],
                node_selector={"pool": "gpu"},
            ),
        ),
        NS_NAME,
    )
    assert m["metadata"]["name"] == "argus-11111111-2222-3333-4444-555555555555"
    s = m["spec"]
    assert s["restartPolicy"] == "Never"  # replacing an instance is Argus's call
    assert s["automountServiceAccountToken"] is False
    assert s["serviceAccountName"] == "worker"
    assert s["imagePullSecrets"] == [{"name": "regcred"}]
    assert s["nodeSelector"] == {"pool": "gpu"}
    assert s["securityContext"] == {
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    c = s["containers"][0]
    assert c["securityContext"] == {
        "runAsNonRoot": True,
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert c["resources"] == {
        "limits": {"cpu": "500m", "memory": "256Mi"},
        "requests": {"cpu": "500m", "memory": "256Mi"},
    }
    assert {"name": "ARGUS_URL", "value": "http://argus"} in c["env"]
    assert m["metadata"]["labels"]["argus.pool"] == "web"


@pytest.mark.parametrize(
    ("phase", "exit_code", "waiting", "expected"),
    [
        ("Pending", None, None, ("created", None)),
        ("Pending", None, "ImagePullBackOff", ("created", None)),
        ("Running", None, None, ("running", None)),
        ("Unknown", None, None, ("running", None)),  # not confirmed gone
        ("Succeeded", 0, None, ("exited", 0)),
        ("Failed", 137, None, ("exited", 137)),
        ("Failed", None, None, ("exited", 1)),  # evicted: no container status
    ],
)
def test_pod_status(phase, exit_code, waiting, expected) -> None:
    assert pod_status(pod("p", phase, exit_code=exit_code, waiting=waiting)) == expected


async def test_start_is_idempotent_by_name(
    target: KubernetesTarget, api: FakeCoreV1
) -> None:
    first = await target.start(spec())
    again = await target.start(spec())  # an ambiguous retry: 409 -> same pod
    assert first == again == "argus-11111111-2222-3333-4444-555555555555"
    assert len(api.created) == 1


async def test_start_failures_become_target_errors(
    target: KubernetesTarget, api: FakeCoreV1
) -> None:
    body = json.dumps({"message": 'pods "x" is forbidden: exceeded quota'})
    api.fail_with = ApiException(status=403, reason="Forbidden", http_resp=None)
    api.fail_with.body = body
    with pytest.raises(TargetError, match=r"403 Forbidden.*exceeded quota"):
        await target.start(spec())


async def test_list_filters_by_label_and_maps_status(
    target: KubernetesTarget, api: FakeCoreV1
) -> None:
    await target.start(spec())
    other = spec(
        instance_id="aaaaaaaa-0000-0000-0000-000000000000",
        labels={
            "argus.managed": "true",
            "argus.pool": "other",
            "argus.instance_id": "x",
        },
    )
    await target.start(other)
    api.pods[other.name].status.phase = "Failed"
    found = await target.list({"argus.pool": "web"})
    assert [(p.instance_id, p.status) for p in found] == [
        ("11111111-2222-3333-4444-555555555555", "running")
    ]


async def test_stop_is_graceful_and_a_missing_pod_is_fine(
    target: KubernetesTarget, api: FakeCoreV1
) -> None:
    ref = await target.start(spec())
    await target.stop(ref, grace=timedelta(seconds=20))
    assert api.deleted == [(ref, 20)]
    # While terminating the pod still lists as running: not confirmation.
    [p] = await target.list({"argus.pool": "web"})
    assert p.status == "running"
    await target.stop("argus-gone", grace=timedelta(seconds=1))  # 404: nothing to do


async def test_logs_explain_a_pod_that_has_not_started(
    target: KubernetesTarget, api: FakeCoreV1
) -> None:
    ref = await target.start(spec())
    assert "hello" in await target.logs(ref, tail=10)
    api.pods[ref] = pod(ref, "Pending", labels={}, waiting="ImagePullBackOff")
    assert "ImagePullBackOff" in await target.logs(ref, tail=10)
    assert await target.logs("argus-missing", tail=10) == ""


async def test_logs_are_decoded_text(target: KubernetesTarget, api: FakeCoreV1) -> None:
    ref = await target.start(spec())
    text = await target.logs(ref, tail=5)
    assert text == "2026-01-01T00:00:00Z hello\n"
    assert not text.startswith("b'")  # the client's stringified-bytes bug


async def test_descendant_listing_by_label_key(
    target: KubernetesTarget, api: FakeCoreV1
) -> None:
    await target.start(spec(labels={**spec().labels, "owner": "inst-1"}))
    found = await target.list_by_label("owner")
    assert [(c.value, c.running) for c in found] == [("inst-1", True)]


def test_registry_requires_an_explicit_context() -> None:
    reg = TargetRegistry()
    with pytest.raises(TargetError, match=r"explicit context"):
        reg.get("kubernetes", None, "default")
    t = reg.get("kubernetes", "kind-argus-it", "workers")
    assert isinstance(t, KubernetesTarget)
    assert reg.get("kubernetes", "kind-argus-it", "workers") is t
    assert reg.get("kubernetes", "kind-argus-it", "other") is not t


def test_pool_validation_for_the_kubernetes_target() -> None:
    ok = {"image": "w:1", "max_replicas": 2, "target": "kubernetes"}
    with pytest.raises(ValidationError, match=r"needs the `kubernetes` options"):
        PoolUpsert(**ok)
    with pytest.raises(ValidationError):  # no context: no silent current-context
        PoolUpsert(**ok, kubernetes={"namespace": "w"})
    with pytest.raises(ValidationError, match=r"plain runtime"):
        PoolUpsert(
            **ok,
            runtime="nanoclaw",
            kubernetes={"context": "c"},
            nanoclaw={"install_dir": "/a", "instances_root": "/b", "agent_image": "i"},
        )
    with pytest.raises(ValidationError):  # namespace must be DNS-1123
        PoolUpsert(**ok, kubernetes={"context": "c", "namespace": "Bad_NS"})
    PoolUpsert(**ok, kubernetes={"context": "kind-argus-it", "namespace": "workers"})
