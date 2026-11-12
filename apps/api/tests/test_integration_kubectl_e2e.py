"""End to end, isolated: Argus delivers a Kubernetes integration to the attempt, the
NanoClaw channel's own installer writes the kubeconfig, and a real kubectl uses it
against a fake HTTPS Kubernetes API whose CA this test mints
(docs/specs/integrations.spec.md 3.6, 3.7). No real cluster, no real credential, no
network beyond 127.0.0.1, and neither ~/.kube nor KUBECONFIG of the host is read.

The gateway path (3.6 "Gateway path") runs through a local CONNECT proxy in two modes.
"intercept" terminates TLS with its own CA, as OneCLI v1.42.0 does for every
authenticated CONNECT: kubectl must refuse it and the channel's route probe must block.
"splice" passes the authenticated tunnel through untouched. It is a fixture that
models the upstream prerequisite, not OneCLI 1.42 behaviour.

kubectl comes from ARGUS_TEST_KUBECTL or PATH; without it (or without node) the test
is skipped with a reason.
"""

import base64
import contextlib
import json
import os
import shutil
import socket
import socketserver
import ssl
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
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
ROUTE = ROOT / "integrations" / "nanoclaw" / "argus-integrations-route.ts"
# What kubectl dials through a proxy: Go never proxies a loopback host.
HOST = "k8s.example.test"
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
            x509.SubjectAlternativeName(
                [x509.IPAddress(IPv4Address("127.0.0.1")), x509.DNSName(HOST)]
            ),
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


def _server_tls(directory: Path, name: str) -> tuple[ssl.SSLContext, bytes]:
    """A CA and a leaf it signs for 127.0.0.1 and HOST; the leaf's server context and
    the CA's PEM."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _cert(f"{name}-test-ca", ca_key, ca=True)
    key = ec.generate_private_key(ec.SECP256R1())
    cert = _cert(HOST, key, ca, ca_key, ca=False)
    directory.mkdir(exist_ok=True)
    (directory / f"{name}.pem").write_bytes(_pem(cert))
    (directory / f"{name}.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(directory / f"{name}.pem", directory / f"{name}.key")
    return context, _pem(ca)


@pytest.fixture
def cluster(tmp_path: Path) -> Iterator[tuple[str, str]]:
    """A fake API server on 127.0.0.1; yields its URL and the base64 CA bundle."""
    context, ca = _server_tls(tmp_path / "tls", "cluster")
    FakeKubernetes.token = TOKEN
    FakeKubernetes.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeKubernetes)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"https://127.0.0.1:{server.server_address[1]}",
            base64.b64encode(ca).decode(),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


AGENTS = {"agent-a": "onecli-agent-token-a", "agent-b": "onecli-agent-token-b"}


@dataclass
class Tunnel:
    """One CONNECT the gateway answered, and every byte it saw inside the tunnel."""

    authority: str
    agent: str | None
    status: int = 0
    sent: bytearray = field(default_factory=bytearray)  # client to cluster, as relayed
    plaintext: bytearray = field(default_factory=bytearray)  # what interception read


class Gateway(socketserver.ThreadingTCPServer):
    """A CONNECT proxy that authenticates the agent token in Proxy-Authorization (407
    otherwise), refuses any host but the cluster (403) and then either splices the
    tunnel or terminates TLS with its own CA."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, upstream: int, mitm: ssl.SSLContext) -> None:
        super().__init__(("127.0.0.1", 0), GatewayHandler)
        self.upstream = upstream
        self.mitm = mitm
        self.mode = "intercept"
        self.tunnels: list[Tunnel] = []

    def url(self, agent: str) -> str:
        return f"http://x:{AGENTS.get(agent, 'unknown')}@127.0.0.1:{self.server_address[1]}"


class GatewayHandler(socketserver.BaseRequestHandler):
    server: Gateway

    def handle(self) -> None:
        sock: socket.socket = self.request
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                return
            head += chunk
        header, early = head.split(b"\r\n\r\n", 1)
        lines = header.decode("latin1").split("\r\n")
        auth = next(
            (
                v.strip()
                for k, _, v in (h.partition(":") for h in lines[1:])
                if k.strip().lower() == "proxy-authorization"
            ),
            "",
        )
        token = (
            base64.b64decode(auth.removeprefix("Basic ") or "")
            .decode()
            .partition(":")[2]
        )
        agent = next((a for a, t in AGENTS.items() if t == token), None)
        tunnel = Tunnel(authority=lines[0].split(" ")[1], agent=agent)
        self.server.tunnels.append(tunnel)
        tunnel.sent += early
        if agent is None:
            tunnel.status = 407
            sock.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
            return
        if tunnel.authority.rpartition(":")[0] != HOST:
            tunnel.status = 403
            sock.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        tunnel.status = 200
        sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        if self.server.mode == "intercept":
            # The client refuses our certificate: nothing is ever decrypted.
            with (
                contextlib.suppress(OSError),
                self.server.mitm.wrap_socket(sock, server_side=True) as tls,
            ):
                while data := tls.recv(65536):
                    tunnel.plaintext += data
            return
        with socket.create_connection(("127.0.0.1", self.server.upstream)) as up:
            up.sendall(early)
            back = threading.Thread(target=self._pump, args=(up, sock, None))
            back.start()
            self._pump(sock, up, tunnel.sent)
            back.join()

    @staticmethod
    def _pump(src: socket.socket, dst: socket.socket, seen: bytearray | None) -> None:
        with contextlib.suppress(OSError):
            while data := src.recv(65536):
                if seen is not None:
                    seen += data
                dst.sendall(data)
        with contextlib.suppress(OSError):
            dst.shutdown(socket.SHUT_WR)


@pytest.fixture
def gateway(tmp_path: Path, cluster: tuple[str, str]) -> Iterator[Gateway]:
    """The worker's gateway in front of `cluster`, intercepting until told to splice."""
    mitm, _ = _server_tls(tmp_path / "gateway-tls", "gateway")
    server = Gateway(int(cluster[0].rpartition(":")[2]), mitm)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def channel(op: str, payload: dict) -> dict:
    """Run the channel's installer (install or recheck) under node with a source that
    answers from `payload`, the way it would answer from Argus. With `proxy`, the
    route probe is the channel's own, through that proxy; without, the probe passes
    (no gateway in the path)."""
    script = f"""
      import * as m from {json.dumps(CHANNEL.as_uri())};
      import {{ gatewayRouteProbe }} from {json.dumps(ROUTE.as_uri())};
      const p = JSON.parse(await new Promise((ok) => {{
        let s = ""; process.stdin.on("data", (d) => (s += d)); process.stdin.on("end", () => ok(s));
      }}));
      const source = {{
        status: async () => p.states,
        credential: async (_t, id) => p.credentials[id],
      }};
      const routeProbe = p.proxy
        ? gatewayRouteProbe(async () => ({{ proxy: p.proxy, release: async () => {{}} }}))
        : async () => null;
      const out = p.op === "install"
        ? await m.installTaskIntegrations({{ ...p.opts, source, routeProbe }})
        : await m.recheckTaskIntegrations(p.opts.groupDir, p.installed, source, routeProbe);
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
    tmp_path: Path, kubeconfig: Path, *args: str, proxy: str | None = None
) -> subprocess.CompletedProcess:
    home = tmp_path / "home"  # an empty HOME: the host's ~/.kube is never read
    home.mkdir(exist_ok=True)
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    if proxy:
        env["HTTPS_PROXY"] = proxy  # as the worker's session gets it from the gateway
    return subprocess.run(
        [KUBECTL or "kubectl", "--kubeconfig", str(kubeconfig), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
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


async def _delivered(
    db: Database, store: MemorySecretStore, clock: FakeClock, server: str, ca_data: str
) -> tuple[str, list[tuple[str, dict]]]:
    """One integration at `server`, two tasks selecting it, each claimed by its own
    agent; yields the integration id and, per agent, the channel's install payload."""
    for agent in AGENTS:
        await insert_agent(db, agent)
        await account(db, store, f"acc-{agent}", "anthropic")
    body = service.IntegrationCreate.model_validate(
        {
            "kind": "kubernetes",
            "name": "fake-cluster",
            "config": {
                "server": f"https://{HOST}",
                "namespace": NAMESPACE,
                "ca_data": ca_data,
            },
            "secret": {"token": TOKEN},
            "expires_at": clock.now + timedelta(seconds=60),
        }
    )
    iid = (await service.create(db, store, "demo", body, "admin:test")).id
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE integrations SET validation = 'ok',"
            " config = json_set(config, '$.server', ?) WHERE id = ?",
            (server, iid),
        )
    payloads = []
    for agent in AGENTS:
        task_id = await make_task(db, iid)
        await start(db, store, agent)
        states = await status(db, store, task_id, agent=agent)
        credential = await deliver(
            db, store, task_id, iid, agent=agent, version=states[0].secret_version
        )
        payloads.append(
            (
                agent,
                {
                    "opts": {
                        "taskId": task_id,
                        "attempt": 1,
                        "selected": [iid],
                        "engineProvider": "anthropic",
                        "executorProvider": None,
                    },
                    "states": [_jsonable(s) for s in states],
                    "credentials": {iid: _jsonable(credential)},
                },
            )
        )
    return iid, payloads


async def test_an_intercepting_gateway_is_refused_by_kubectl_and_blocked_by_the_channel(
    db: Database,
    clock: FakeClock,
    tmp_path: Path,
    cluster: tuple[str, str],
    gateway: Gateway,
) -> None:
    port = cluster[0].rpartition(":")[2]
    store = MemorySecretStore()
    _, [(agent, payload), _] = await _delivered(
        db, store, clock, f"https://{HOST}:{port}", cluster[1]
    )
    group = tmp_path / "group"
    group.mkdir()
    payload["opts"]["groupDir"] = str(group)

    # The channel's probe sees the gateway's certificate and blocks before any file.
    out = channel("install", {**payload, "proxy": gateway.url(agent)})
    assert out["ok"] is False
    assert out["reason"] == "env_integration_gateway_intercepts"
    assert "the gateway intercepts TLS" in out["error"]
    assert TOKEN not in json.dumps(out)
    assert AGENTS[agent] not in json.dumps(out)
    assert not any(group.rglob("kubeconfig"))

    # Had a kubeconfig been delivered anyway, kubectl with only the cluster's CA
    # refuses the gateway's certificate: x509, and no request leaves kubectl.
    installed = channel("install", payload)["installed"]  # no gateway in this path
    kubeconfig = Path(installed["dir"]) / "kubeconfig"
    got = kubectl(tmp_path, kubeconfig, "get", "pods", proxy=gateway.url(agent))
    assert got.returncode != 0
    assert "x509" in got.stderr or "certificate" in got.stderr, got.stderr
    assert FakeKubernetes.seen == []
    assert {t.status for t in gateway.tunnels} == {200}
    for tunnel in gateway.tunnels:
        assert tunnel.plaintext == b""
        assert TOKEN.encode() not in tunnel.sent


async def test_a_spliced_tunnel_carries_kubectl_end_to_end_per_task_identity(
    db: Database,
    clock: FakeClock,
    tmp_path: Path,
    cluster: tuple[str, str],
    gateway: Gateway,
) -> None:
    """Fixture-only: the splice models the upstream prerequisite (an authenticated
    per-agent CONNECT passed through), not what OneCLI v1.42.0 does."""
    gateway.mode = "splice"
    port = cluster[0].rpartition(":")[2]
    store = MemorySecretStore()
    _, payloads = await _delivered(
        db, store, clock, f"https://{HOST}:{port}", cluster[1]
    )

    kubeconfigs = {}
    for agent, payload in payloads:
        group = tmp_path / agent
        group.mkdir()
        payload["opts"]["groupDir"] = str(group)
        out = channel("install", {**payload, "proxy": gateway.url(agent)})
        assert out["ok"] is True, out
        kubeconfigs[agent] = Path(out["installed"]["dir"]) / "kubeconfig"
    probes = list(gateway.tunnels)
    assert [(t.agent, t.status) for t in probes] == [(a, 200) for a in AGENTS]
    gateway.tunnels.clear()

    for agent, kubeconfig in kubeconfigs.items():
        got = kubectl(
            tmp_path, kubeconfig, "get", "pods", "-o", "name", proxy=gateway.url(agent)
        )
        assert got.returncode == 0, got.stderr
        assert got.stdout.strip() == "pod/web-0"
        # Each task's tunnels carry its own agent identity, never the other's.
        assert {t.agent for t in gateway.tunnels} == {agent}
        gateway.tunnels.clear()
    assert {auth for _, _, auth in FakeKubernetes.seen} == {f"Bearer {TOKEN}"}
    # The gateway relayed ciphertext only: neither the bearer nor the request line.
    for tunnel in probes:
        assert TOKEN.encode() not in tunnel.sent
        assert b"GET " not in tunnel.sent


async def test_the_route_probe_refuses_a_ca_mismatch_and_a_refused_connect(
    db: Database,
    clock: FakeClock,
    tmp_path: Path,
    cluster: tuple[str, str],
    gateway: Gateway,
) -> None:
    gateway.mode = "splice"
    port = cluster[0].rpartition(":")[2]
    store = MemorySecretStore()
    other_ca = base64.b64encode(_server_tls(tmp_path / "other", "other")[1]).decode()
    _, [(agent, payload), _] = await _delivered(
        db, store, clock, f"https://{HOST}:{port}", cluster[1]
    )
    group = tmp_path / "group"
    group.mkdir()
    payload["opts"]["groupDir"] = str(group)
    [credential] = payload["credentials"].values()

    def install(proxy: str, **over) -> dict:
        cred = {**credential, **over}
        return channel(
            "install",
            {**payload, "credentials": {cred["integration_id"]: cred}, "proxy": proxy},
        )

    # The cluster's certificate under another CA: refused even through a clean splice.
    out = install(gateway.url(agent), ca_data=other_ca)
    assert (out["ok"], out["reason"]) == (False, "env_integration_gateway_intercepts")
    assert "not trusted" in out["error"]
    # Deny controls: an unknown agent token (407) and a host the gateway refuses (403).
    out = install(gateway.url("nobody"))
    assert out["error"].endswith("the gateway refused the CONNECT (HTTP 407)")
    out = install(gateway.url(agent), server="https://elsewhere.example.test:6443")
    assert out["error"].endswith("the gateway refused the CONNECT (HTTP 403)")
    assert not any(group.rglob("kubeconfig"))
    for tunnel in gateway.tunnels:
        assert TOKEN.encode() not in tunnel.sent
