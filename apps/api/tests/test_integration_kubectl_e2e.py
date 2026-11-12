"""End to end, isolated: Argus delivers a Kubernetes integration to the attempt, the
NanoClaw channel's own installer writes the kubeconfig, and a real kubectl uses it
against a fake HTTPS Kubernetes API whose CA this test mints
(docs/specs/integrations.spec.md 3.6, 3.7). No real cluster, no real credential, no
network beyond 127.0.0.1, and neither ~/.kube nor KUBECONFIG of the host is read.

kubectl comes from ARGUS_TEST_KUBECTL or PATH; without it (or without node) the test
is skipped with a reason.
"""

import base64
import json
import os
import shutil
import ssl
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
from pathlib import Path
from typing import ClassVar

import pytest
from api_support import FakeClock, insert_agent, insert_project
from argus_api.db import Database
from argus_api.errors import ConflictError, ForbiddenError, ValidationFailedError
from argus_api.integrations import service
from argus_api.secrets import MemorySecretStore
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from test_integration_delivery import (
    TOKEN,
    A,
    account,
    deliver,
    integration,
    make_task,
    start,
    status,
)

ROOT = Path(__file__).resolve().parents[3]
CHANNEL = ROOT / "integrations" / "nanoclaw" / "argus-integrations.ts"
KUBECTL = os.environ.get("ARGUS_TEST_KUBECTL") or shutil.which("kubectl")
NODE = shutil.which("node")
pytestmark = [
    pytest.mark.skipif(
        KUBECTL is None, reason="no kubectl (set ARGUS_TEST_KUBECTL or put it on PATH)"
    ),
    pytest.mark.skipif(NODE is None, reason="node runs the channel's installer"),
]

NAMESPACE = "team-a"
POD_LIST = {
    "kind": "PodList",
    "apiVersion": "v1",
    "metadata": {"resourceVersion": "1"},
    "items": [
        {
            "kind": "Pod",
            "apiVersion": "v1",
            "metadata": {
                "name": "web-0",
                "namespace": NAMESPACE,
                "creationTimestamp": "2026-01-01T00:00:00Z",
            },
            "status": {"phase": "Running"},
        }
    ],
}
API_RESOURCES = {
    "kind": "APIResourceList",
    "groupVersion": "v1",
    "resources": [
        {
            "name": name,
            "singularName": singular,
            "namespaced": True,
            "kind": kind,
            "verbs": ["create", "delete", "get", "list"],
        }
        for name, singular, kind in [
            ("pods", "pod", "Pod"),
            ("configmaps", "configmap", "ConfigMap"),
        ]
    ],
}


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _cert(subject: str, key, issuer=None, issuer_key=None, *, ca: bool):
    now = datetime.now(UTC)  # TLS checks real time, not the database clock
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer.subject if issuer else name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # check-tests: allow elapsed-time — a certificate validity window, not a timing assert
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                (issuer_key or key).public_key()
            ),
            critical=False,
        )
    )
    if ca:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    if not ca:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(IPv4Address("127.0.0.1"))]),
            critical=False,
        )
    return builder.sign(issuer_key or key, hashes.SHA256())


class FakeKubernetes(BaseHTTPRequestHandler):
    """Reads pods of the one namespace with the one token; everything else is 401
    without the bearer and 403 with it (as RBAC for a read-only Role would)."""

    token = ""
    seen: ClassVar[list[tuple[str, str, str | None]]] = []

    def log_message(self, *args) -> None:
        pass  # no request lines (with or without headers) in the test output

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _status(self, code: int, reason: str) -> None:
        self._send(
            code,
            {
                "kind": "Status",
                "apiVersion": "v1",
                "status": "Failure",
                "reason": reason,
                "code": code,
                "message": f"{reason.lower()}: {self.command} {self.path}",
            },
        )

    def _handle(self) -> None:
        path = self.path.split("?", 1)[0]
        auth = self.headers.get("Authorization")
        self.seen.append((self.command, path, auth))
        if auth != f"Bearer {self.token}":
            return self._status(401, "Unauthorized")
        if self.command == "GET" and path == "/api":
            return self._send(200, {"kind": "APIVersions", "versions": ["v1"]})
        if self.command == "GET" and path == "/apis":
            return self._send(200, {"kind": "APIGroupList", "groups": []})
        if self.command == "GET" and path == "/api/v1":
            return self._send(200, API_RESOURCES)
        if self.command == "GET" and path == f"/api/v1/namespaces/{NAMESPACE}/pods":
            return self._send(200, POD_LIST)
        return self._status(403, "Forbidden")

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = _handle


@pytest.fixture
def cluster(tmp_path: Path) -> Iterator[tuple[str, str]]:
    """A fake API server on 127.0.0.1; yields its URL and the base64 CA bundle."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _cert("argus-test-ca", ca_key, ca=True)
    key = ec.generate_private_key(ec.SECP256R1())
    cert = _cert("127.0.0.1", key, ca, ca_key, ca=False)
    (tmp_path / "tls").mkdir()
    (tmp_path / "tls" / "cert.pem").write_bytes(_pem(cert))
    (tmp_path / "tls" / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(tmp_path / "tls" / "cert.pem", tmp_path / "tls" / "key.pem")
    FakeKubernetes.token = TOKEN
    FakeKubernetes.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeKubernetes)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"https://127.0.0.1:{server.server_address[1]}",
            base64.b64encode(_pem(ca)).decode(),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def channel(op: str, payload: dict) -> dict:
    """Run the channel's installer (install or recheck) under node with a source that
    answers from `payload`, the way it would answer from Argus."""
    script = f"""
      import * as m from {json.dumps(CHANNEL.as_uri())};
      const p = JSON.parse(await new Promise((ok) => {{
        let s = ""; process.stdin.on("data", (d) => (s += d)); process.stdin.on("end", () => ok(s));
      }}));
      const source = {{
        status: async () => p.states,
        credential: async (_t, id) => p.credentials[id],
      }};
      const out = p.op === "install"
        ? await m.installTaskIntegrations({{ ...p.opts, source }})
        : await m.recheckTaskIntegrations(p.opts.groupDir, p.installed, source);
      console.log(JSON.stringify(out));
    """
    done = subprocess.run(
        [
            NODE or "node",
            "--experimental-strip-types",
            "--input-type=module",
            "-e",
            script,
        ],
        input=json.dumps({"op": op, **payload}),
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ["PATH"], "NODE_NO_WARNINGS": "1"},
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def kubectl(
    tmp_path: Path, kubeconfig: Path, *args: str
) -> subprocess.CompletedProcess:
    home = tmp_path / "home"  # an empty HOME: the host's ~/.kube is never read
    home.mkdir(exist_ok=True)
    return subprocess.run(
        [KUBECTL or "kubectl", "--kubeconfig", str(kubeconfig), *args],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ["PATH"], "HOME": str(home)},
    )


def _jsonable(model) -> dict:
    return json.loads(model.model_dump_json())


async def test_kubectl_reads_pods_and_is_refused_writes_and_other_namespaces(
    db: Database, clock: FakeClock, tmp_path: Path, cluster: tuple[str, str]
) -> None:
    server, ca_data = cluster
    store = MemorySecretStore()
    await insert_agent(db, A)
    await insert_project(db, "other")
    await account(db, store, "acc-claude", "anthropic")
    body = service.IntegrationCreate.model_validate(
        {
            "kind": "kubernetes",
            "name": "fake-cluster",
            "config": {
                "server": "https://k8s.example.test",
                "namespace": NAMESPACE,
                "ca_data": ca_data,
            },
            "secret": {"token": TOKEN},
            "expires_at": clock.now + timedelta(seconds=60),
        }
    )
    iid = (await service.create(db, store, "demo", body, "admin:test")).id
    async with db.write() as tx:  # validation itself is covered in test_integrations.py
        await tx.conn.execute(
            "UPDATE integrations SET validation = 'ok' WHERE id = ?", (iid,)
        )
        # The API refuses loopback servers; only this test points one at its fake.
        await tx.conn.execute(
            "UPDATE integrations SET config = json_set(config, '$.server', ?)"
            " WHERE id = ?",
            (server, iid),
        )
    foreign = await integration(db, store, "other")

    # Negative control: another project's integration is never selected.
    with pytest.raises(ValidationFailedError):
        await make_task(db, foreign)
    task_id = await make_task(db, iid)
    await start(db, store)
    states = await status(db, store, task_id)
    credential = await deliver(
        db, store, task_id, iid, version=states[0].secret_version
    )
    # Negative control: nor delivered, even by id, to a task that did not select it.
    with pytest.raises(ForbiddenError):
        await deliver(db, store, task_id, foreign)

    group = tmp_path / "group"
    group.mkdir()
    payload = {
        "opts": {
            "groupDir": str(group),
            "taskId": task_id,
            "attempt": 1,
            "selected": [iid],
            "engineProvider": "anthropic",
            "executorProvider": None,
        },
        "states": [_jsonable(s) for s in states],
        "credentials": {iid: _jsonable(credential)},
    }
    out = channel("install", payload)
    assert out["ok"] is True
    assert TOKEN not in json.dumps(out)  # what the channel logs and prompts from
    installed = out["installed"]
    kubeconfig = Path(installed["dir"]) / "kubeconfig"
    assert kubeconfig.stat().st_mode & 0o777 == 0o600
    assert Path(installed["dir"]).stat().st_mode & 0o777 == 0o700

    # The context is pinned to the namespace: no -n, and the CA is the test's own.
    got = kubectl(tmp_path, kubeconfig, "get", "pods", "-o", "name")
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "pod/web-0"
    for args in (
        ("get", "pods", "-n", "kube-system"),
        ("delete", "pod", "web-0"),
        ("create", "configmap", "probe"),
    ):
        refused = kubectl(tmp_path, kubeconfig, *args)
        assert refused.returncode != 0, args
        assert "Forbidden" in refused.stderr or "forbidden" in refused.stderr, args
    methods = {method for method, _, _ in FakeKubernetes.seen}
    assert {"GET", "POST", "DELETE"} <= methods
    assert {auth for _, _, auth in FakeKubernetes.seen} == {f"Bearer {TOKEN}"}

    # The bearer is required: the same request without it is 401.
    unauthenticated = ssl.create_default_context(
        cadata=base64.b64decode(ca_data).decode()
    )
    direct = urllib.request.build_opener(  # no proxy from the environment
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=unauthenticated),
    )
    with pytest.raises(urllib.error.HTTPError) as err:
        direct.open(f"{server}/api/v1/namespaces/{NAMESPACE}/pods")
    assert err.value.code == 401

    # Negative control: once expired, Argus refuses it and the next check removes
    # the file, so kubectl has nothing left to use.
    clock.advance(61)
    with pytest.raises(ConflictError) as expired:
        await deliver(db, store, task_id, iid)
    assert expired.value.code == "integration_expired"
    later = await status(db, store, task_id)
    assert [s.readiness for s in later] == ["expired"]
    out = channel(
        "recheck",
        {**payload, "states": [_jsonable(s) for s in later], "installed": installed},
    )
    assert out["state"] == "removed"
    assert not kubeconfig.exists()
    assert kubectl(tmp_path, kubeconfig, "get", "pods").returncode != 0

    # Negative control: a revoked integration is refused too.
    await service.revoke(db, store, "demo", iid, "admin:test")
    with pytest.raises(ConflictError) as revoked:
        await deliver(db, store, task_id, iid)
    assert revoked.value.code == "integration_revoked"
    assert [s.readiness for s in await status(db, store, task_id)] == ["revoked"]
