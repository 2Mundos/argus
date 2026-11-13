"""End to end, isolated: Argus delivers a Kubernetes integration to the attempt, the
NanoClaw channel's own installer writes the kubeconfig and opens the channel's
Kubernetes route, and a real kubectl uses it against a fake HTTPS Kubernetes API
whose CA this test mints (docs/specs/integrations.spec.md 3.6). No real cluster, no
real credential, and neither ~/.kube nor KUBECONFIG of the host is read.

The channel runs in a node subprocess with the real route broker; every call it
makes to Argus (status, delivery, and the authorization of each CONNECT) is answered
here by the API's own `delivery.status` and `delivery.deliver`. The fake API listens
on 127.0.0.1 behind a recording relay on this host's non-loopback IPv4, which is
what the route's pinned lookup resolves the cluster's name to; the relay records
every byte the route forwards. An intercepting CONNECT proxy stands in for the
OneCLI gateway: it terminates TLS with its own CA, as OneCLI v1.42.0 does for every
authenticated CONNECT.

kubectl comes from ARGUS_TEST_KUBECTL or PATH; without it, node, or a non-loopback
IPv4, the test is skipped with a reason.
"""

import asyncio
import base64
import contextlib
import json
import os
import shutil
import socket
import socketserver
import ssl
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
from argus_api.tasks import service as tasks
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from test_integration_delivery import TOKEN, account, deliver, make_task, start, status

ROOT = Path(__file__).resolve().parents[3]
CHANNEL = ROOT / "integrations" / "nanoclaw" / "argus-integrations.ts"
ROUTE = ROOT / "integrations" / "nanoclaw" / "argus-integrations-route.ts"
# The cluster's names; the certificate names HOST and HOST_B, never UNNAMED.
HOST = "k8s.example.test"
HOST_B = "k8s-b.example.test"
UNNAMED = "k8s-unnamed.example.test"
INTERNAL = "internal.example.test"  # resolves to loopback: the route refuses it
KUBECTL = os.environ.get("ARGUS_TEST_KUBECTL") or shutil.which("kubectl")
NODE = shutil.which("node")


def _host_ipv4() -> str | None:
    """This host's outbound IPv4 (no packet is sent), unless loopback or link-local."""
    with (
        contextlib.suppress(OSError),
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe,
    ):
        probe.connect(("192.0.2.1", 9))
        ip = IPv4Address(probe.getsockname()[0])
        if not (ip.is_loopback or ip.is_link_local or ip.is_unspecified):
            return str(ip)
    return None


HOST_IP = _host_ipv4()
pytestmark = [
    pytest.mark.skipif(
        KUBECTL is None, reason="no kubectl (set ARGUS_TEST_KUBECTL or put it on PATH)"
    ),
    pytest.mark.skipif(NODE is None, reason="node runs the channel and its route"),
    pytest.mark.skipif(
        HOST_IP is None, reason="the route refuses loopback; no other IPv4 to relay on"
    ),
]

NAMESPACE = "team-a"
OTHER_TOKEN = "k8s-sentinel-rotated-Zp7Lw2-never-shown"
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
                [
                    x509.IPAddress(IPv4Address("127.0.0.1")),
                    x509.DNSName(HOST),
                    x509.DNSName(HOST_B),
                ]
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


@dataclass
class Cluster:
    """The fake API server (`direct`, on loopback) and the relay in front of it on
    HOST_IP, which records every byte it is sent and counts its connections."""

    direct: str
    port: int
    ca_data: str
    relay: "Relay"

    def server(self, host: str = HOST, port: int | None = None) -> str:
        return f"https://{host}:{port or self.port}"


class Relay(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self, upstream: int) -> None:
        super().__init__((HOST_IP or "", 0), RelayHandler)
        self.upstream = upstream
        self.connections = 0
        self.sent = bytearray()  # route to cluster, as forwarded


class RelayHandler(socketserver.BaseRequestHandler):
    server: Relay

    def handle(self) -> None:
        self.server.connections += 1
        with socket.create_connection(("127.0.0.1", self.server.upstream)) as up:
            back = threading.Thread(target=_pump, args=(up, self.request, None))
            back.start()
            _pump(self.request, up, self.server.sent)
            back.join()


def _pump(src: socket.socket, dst: socket.socket, seen: bytearray | None) -> None:
    with contextlib.suppress(OSError):
        while data := src.recv(65536):
            if seen is not None:
                seen += data
            dst.sendall(data)
    with contextlib.suppress(OSError):
        dst.shutdown(socket.SHUT_WR)


def _serve(server: socketserver.BaseServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


@pytest.fixture
def cluster(tmp_path: Path) -> Iterator[Cluster]:
    context, ca = _server_tls(tmp_path / "tls", "cluster")
    FakeKubernetes.token = TOKEN
    FakeKubernetes.seen = []
    api = ThreadingHTTPServer(("127.0.0.1", 0), FakeKubernetes)
    api.socket = context.wrap_socket(api.socket, server_side=True)
    relay = Relay(api.server_address[1])
    threads = [_serve(api), _serve(relay)]
    try:
        yield Cluster(
            direct=f"https://127.0.0.1:{api.server_address[1]}",
            port=relay.server_address[1],
            ca_data=base64.b64encode(ca).decode(),
            relay=relay,
        )
    finally:
        for server in (api, relay):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()


@dataclass
class Tunnel:
    """One CONNECT the gateway answered, and what interception could read in it."""

    authority: str
    plaintext: bytearray = field(default_factory=bytearray)


class Gateway(socketserver.ThreadingTCPServer):
    """The session's gateway as OneCLI v1.42.0 behaves for an authenticated CONNECT:
    it answers 200 and terminates TLS with its own CA."""

    daemon_threads = True

    def __init__(self, mitm: ssl.SSLContext) -> None:
        super().__init__(("127.0.0.1", 0), GatewayHandler)
        self.mitm = mitm
        self.tunnels: list[Tunnel] = []

    @property
    def url(self) -> str:
        return f"http://x:onecli-agent-token@127.0.0.1:{self.server_address[1]}"


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
        tunnel = Tunnel(authority=head.split(b" ", 2)[1].decode())
        self.server.tunnels.append(tunnel)
        sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        # The client refuses our certificate: nothing is ever decrypted.
        with (
            contextlib.suppress(OSError),
            self.server.mitm.wrap_socket(sock, server_side=True) as tls,
        ):
            while data := tls.recv(65536):
                tunnel.plaintext += data


@pytest.fixture
def gateway(tmp_path: Path) -> Iterator[Gateway]:
    mitm, _ = _server_tls(tmp_path / "gateway-tls", "gateway")
    server = Gateway(mitm)
    thread = _serve(server)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


_HOST_SCRIPT = """
import * as m from %(channel)s;
import { createKubernetesRoute } from %(route)s;
import { createInterface } from "node:readline";
const hosts = %(hosts)s;
const send = (o) => process.stdout.write(JSON.stringify(o) + "\\n");
const pending = new Map();
let next = 0;
const call = (name, args) =>
  new Promise((ok, no) => {
    next += 1;
    pending.set(next, { ok, no });
    send({ rpc: next, call: name, args });
  });
const source = {
  status: (taskId, attempt) => call("status", [taskId, attempt]),
  credential: (taskId, id, attempt, version) =>
    call("credential", [taskId, id, attempt, version]),
};
const route = createKubernetesRoute({
  bindHost: async () => "127.0.0.1",
  lookup: async (host) => {
    if (hosts[host]) return hosts[host];
    throw Object.assign(new Error("not found"), { code: "ENOTFOUND" });
  },
});
async function run(msg) {
  if (msg.op === "install")
    return m.installTaskIntegrations({ ...msg.opts, source, transport: route });
  return m.recheckTaskIntegrations(msg.groupDir, msg.installed, source, { transport: route });
}
for await (const line of createInterface({ input: process.stdin })) {
  const msg = JSON.parse(line);
  if (msg.reply) {
    const p = pending.get(msg.reply);
    pending.delete(msg.reply);
    if (msg.error) p.no(new Error(msg.error));
    else p.ok(msg.result);
    continue;
  }
  run(msg).then(
    (result) => send({ cmd: msg.cmd, result }),
    (err) => send({ cmd: msg.cmd, crash: String(err) }),
  );
}
await route.close();
"""


class ChannelHost:
    """One worker's channel: the installer and the route in node, its Argus calls
    answered by the API as `agent`. `overrides` changes what delivery returns, as a
    tampered or misconfigured integration would."""

    def __init__(self, db: Database, store: MemorySecretStore, agent: str) -> None:
        self.db, self.store, self.agent = db, store, agent
        self.overrides: dict = {}
        self.output = bytearray()  # everything the channel printed
        self._waiting: dict[int, asyncio.Future] = {}
        self._answers: set[asyncio.Task] = set()
        self._seq = 0

    async def __aenter__(self) -> "ChannelHost":
        hosts = {HOST: [HOST_IP], HOST_B: [HOST_IP], UNNAMED: [HOST_IP]}
        hosts[INTERNAL] = ["127.0.0.1"]
        script = _HOST_SCRIPT % {
            "channel": json.dumps(CHANNEL.as_uri()),
            "route": json.dumps(ROUTE.as_uri()),
            "hosts": json.dumps(hosts),
        }
        self.proc = await asyncio.create_subprocess_exec(
            NODE or "node",
            "--experimental-strip-types",
            "--input-type=module",
            "-e",
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": os.environ["PATH"], "NODE_NO_WARNINGS": "1"},
        )
        self._reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc) -> None:
        assert self.proc.stdin and self.proc.stderr
        self.proc.stdin.close()
        self.output += await self.proc.stderr.read()
        await self.proc.wait()
        await self._reader
        assert TOKEN.encode() not in self.output
        assert OTHER_TOKEN.encode() not in self.output

    def _send(self, msg: dict) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(msg).encode() + b"\n")

    async def _read(self) -> None:
        assert self.proc.stdout
        async for raw in self.proc.stdout:
            self.output += raw
            msg = json.loads(raw)
            if "rpc" in msg:
                answer = asyncio.create_task(self._answer(msg))
                self._answers.add(answer)
                answer.add_done_callback(self._answers.discard)
            else:
                self._waiting.pop(msg["cmd"]).set_result(msg)

    async def _answer(self, msg: dict) -> None:
        try:
            if msg["call"] == "status":
                task_id, attempt = msg["args"]
                states = await status(self.db, self.store, task_id, attempt, self.agent)
                result = [_jsonable(s) for s in states]
            else:
                task_id, iid, attempt, version = msg["args"]
                cred = await deliver(
                    self.db,
                    self.store,
                    task_id,
                    iid,
                    attempt,
                    self.agent,
                    version=version,
                )
                result = {**_jsonable(cred), **self.overrides}
            self._send({"reply": msg["rpc"], "result": result})
        except Exception as err:  # what the API client would raise to the channel
            self._send({"reply": msg["rpc"], "error": getattr(err, "code", "error")})

    async def _run(self, **msg) -> dict:
        self._seq += 1
        done = asyncio.get_running_loop().create_future()
        self._waiting[self._seq] = done
        self._send({"cmd": self._seq, **msg})
        out = await done
        assert "crash" not in out, out
        return out["result"]

    async def install(self, group: Path, task_id: str, iid: str, attempt=1) -> dict:
        group.mkdir(exist_ok=True)
        opts = {
            "groupDir": str(group),
            "taskId": task_id,
            "attempt": attempt,
            "selected": [iid],
            "engineProvider": "anthropic",
            "executorProvider": None,
        }
        return await self._run(op="install", opts=opts)

    async def recheck(self, group: Path, installed: dict) -> dict:
        return await self._run(op="recheck", groupDir=str(group), installed=installed)


async def kubectl(
    tmp_path: Path, kubeconfig: Path, *args: str, proxy: str | None = None
) -> tuple[int, str, str]:
    home = tmp_path / "home"  # an empty HOME: the host's ~/.kube is never read
    home.mkdir(exist_ok=True)
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    if proxy:
        env["HTTPS_PROXY"] = proxy  # as the worker's session gets it from the gateway
    proc = await asyncio.create_subprocess_exec(
        KUBECTL or "kubectl",
        "--kubeconfig",
        str(kubeconfig),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    out, err = await proc.communicate()
    assert proc.returncode is not None
    return proc.returncode, out.decode(), err.decode()


async def refused_by_the_route(
    cluster: Cluster, tmp_path: Path, kubeconfig: Path, *args: str
) -> str:
    """kubectl fails and nothing reached the cluster: the route refused the CONNECT."""
    before = cluster.relay.connections
    code, _, err = await kubectl(tmp_path, kubeconfig, "get", "pods", *args)
    assert code != 0
    assert cluster.relay.connections == before, err
    return err


def _jsonable(model) -> dict:
    return json.loads(model.model_dump_json())


async def _kubernetes(
    db: Database,
    store: MemorySecretStore,
    clock: FakeClock,
    server: str,
    ca_data: str,
    project: str = "demo",
    name: str = "fake-cluster",
) -> str:
    """An active, validated integration at `server` (the API refuses non-HTTPS-443
    test hosts at create, so the server is set behind its back)."""
    body = service.IntegrationCreate.model_validate(
        {
            "kind": "kubernetes",
            "name": name,
            "config": {"server": f"https://{HOST}", "namespace": NAMESPACE},
            "secret": {"token": TOKEN},
            "expires_at": clock.now + timedelta(seconds=60),
        }
    )
    iid = (await service.create(db, store, project, body, "admin:test")).id
    await _validated(db, iid, server, ca_data)
    return iid


async def _validated(db: Database, iid: str, server=None, ca_data=None) -> None:
    async with db.write() as tx:  # validation itself is covered in test_integrations.py
        await tx.conn.execute(
            "UPDATE integrations SET validation = 'ok' WHERE id = ?", (iid,)
        )
        if server:
            await tx.conn.execute(
                "UPDATE integrations SET config = json_set(config, '$.server', ?,"
                " '$.ca_data', ?) WHERE id = ?",
                (server, ca_data, iid),
            )


async def _world(db: Database, store: MemorySecretStore, *agents: str) -> None:
    for agent in agents:
        await insert_agent(db, agent)
        await account(db, store, f"acc-{agent}", "anthropic")


def _kubeconfig(installed: dict) -> Path:
    return Path(installed["dir"]) / "kubeconfig"


def _without_route(kubeconfig: Path, target: Path) -> Path:
    """The kubeconfig as it was before the route: no proxy-url, so kubectl takes the
    session's HTTPS_PROXY. A test-owned copy under tmp_path."""
    config = json.loads(kubeconfig.read_text())
    for entry in config["clusters"]:
        entry["cluster"].pop("proxy-url")
    target.write_text(json.dumps(config))
    target.chmod(0o600)
    return target


def _copy(kubeconfig: Path, target: Path) -> Path:
    """What a session that copied its kubeconfig keeps after the channel removes it."""
    shutil.copyfile(kubeconfig, target)
    target.chmod(0o600)
    return target


def _with(kubeconfig: Path, target: Path, **cluster: str) -> Path:
    """A copy whose cluster entry the worker edited."""
    config = json.loads(kubeconfig.read_text())
    config["clusters"][0]["cluster"].update(cluster)
    target.write_text(json.dumps(config))
    target.chmod(0o600)
    return target


async def test_kubectl_reads_through_the_route_and_is_refused_elsewhere(
    db: Database, clock: FakeClock, tmp_path: Path, cluster: Cluster
) -> None:
    store = MemorySecretStore()
    await _world(db, store, "agent-a")
    await insert_project(db, "other")
    iid = await _kubernetes(db, store, clock, cluster.server(), cluster.ca_data)
    foreign = await _kubernetes(
        db, store, clock, cluster.server(HOST_B), cluster.ca_data, project="other"
    )
    # Negative control: another project's integration is never selected.
    with pytest.raises(ValidationFailedError):
        await make_task(db, foreign)
    task_id = await make_task(db, iid)
    await start(db, store, "agent-a")
    # Nor delivered, even by id, to a task that did not select it.
    with pytest.raises(ForbiddenError):
        await deliver(db, store, task_id, foreign, agent="agent-a")

    group = tmp_path / "group"
    async with ChannelHost(db, store, "agent-a") as host:
        out = await host.install(group, task_id, iid)
        assert out["ok"] is True, out
        assert TOKEN not in json.dumps(out)  # what the channel logs and prompts from
        installed = out["installed"]
        kubeconfig = _kubeconfig(installed)
        assert kubeconfig.stat().st_mode & 0o777 == 0o600
        assert Path(installed["dir"]).stat().st_mode & 0o777 == 0o700
        [entry] = json.loads(kubeconfig.read_text())["clusters"]
        assert entry["cluster"]["server"] == cluster.server()
        assert entry["cluster"]["proxy-url"].startswith("http://argus:")
        assert "insecure-skip-tls-verify" not in kubeconfig.read_text()

        # The context is pinned to the namespace: no -n, and the CA is the test's own.
        code, out_, err = await kubectl(
            tmp_path, kubeconfig, "get", "pods", "-o", "name"
        )
        assert code == 0, err
        assert out_.strip() == "pod/web-0"
        for args in (
            ("get", "pods", "-n", "kube-system"),
            ("delete", "pod", "web-0"),
            ("create", "configmap", "probe"),
        ):
            code, _, err = await kubectl(tmp_path, kubeconfig, *args)
            assert code != 0, args
            assert "forbidden" in err.lower(), args
        methods = {method for method, _, _ in FakeKubernetes.seen}
        assert {"GET", "POST", "DELETE"} <= methods
        assert {auth for _, _, auth in FakeKubernetes.seen} == {f"Bearer {TOKEN}"}
        # The route forwarded ciphertext only: neither the bearer nor a request line.
        assert cluster.relay.sent
        assert TOKEN.encode() not in cluster.relay.sent
        assert b"GET " not in cluster.relay.sent

        # The worker's flags and edits cannot widen the grant: another port, another
        # integration's host, a guessed grant, no grant, or a proxy from the env.
        await refused_by_the_route(
            cluster, tmp_path, kubeconfig, f"--server={cluster.server(port=443)}"
        )
        await refused_by_the_route(
            cluster, tmp_path, kubeconfig, f"--server={cluster.server(HOST_B)}"
        )
        port = entry["cluster"]["proxy-url"].rpartition(":")[2]
        for name, proxy in (
            ("guessed", f"http://argus:{'A' * 43}@127.0.0.1:{port}"),
            ("anonymous", f"http://127.0.0.1:{port}"),
        ):
            edited = _with(
                kubeconfig, tmp_path / f"{name}.kubeconfig", **{"proxy-url": proxy}
            )
            err = await refused_by_the_route(cluster, tmp_path, edited)
            assert "Proxy Authentication Required" in err, err

        # The bearer is required: the same request without it is 401.
        unauthenticated = ssl.create_default_context(
            cadata=base64.b64decode(cluster.ca_data).decode()
        )
        direct = urllib.request.build_opener(  # no proxy from the environment
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=unauthenticated),
        )
        with pytest.raises(urllib.error.HTTPError) as err_:
            direct.open(f"{cluster.direct}/api/v1/namespaces/{NAMESPACE}/pods")
        assert err_.value.code == 401

        # Once expired, Argus refuses it: the route refuses the next tunnel before
        # the channel's recheck, which then removes the file.
        kept = _copy(kubeconfig, tmp_path / "kept.kubeconfig")
        clock.advance(61)
        with pytest.raises(ConflictError) as expired:
            await deliver(db, store, task_id, iid, agent="agent-a")
        assert expired.value.code == "integration_expired"
        await refused_by_the_route(cluster, tmp_path, kept)
        out = await host.recheck(group, installed)
        assert out["state"] == "removed"
        assert not kubeconfig.exists()
        err = await refused_by_the_route(cluster, tmp_path, kept)
        assert "Proxy Authentication Required" in err, err


async def test_the_gateway_intercepts_and_the_route_does_not(
    db: Database, clock: FakeClock, tmp_path: Path, cluster: Cluster, gateway: Gateway
) -> None:
    """PR #355's refusal first: through the session's gateway kubectl fails x509 and
    the gateway reads nothing. Then the route, with the same HTTPS_PROXY set: kubectl
    reads end to end and the gateway sees no tunnel for the cluster."""
    store = MemorySecretStore()
    await _world(db, store, "agent-a")
    iid = await _kubernetes(db, store, clock, cluster.server(), cluster.ca_data)
    task_id = await make_task(db, iid)
    await start(db, store, "agent-a")
    async with ChannelHost(db, store, "agent-a") as host:
        out = await host.install(tmp_path / "group", task_id, iid)
        assert out["ok"] is True, out
        kubeconfig = _kubeconfig(out["installed"])

        legacy = _without_route(kubeconfig, tmp_path / "legacy.kubeconfig")
        code, _, err = await kubectl(tmp_path, legacy, "get", "pods", proxy=gateway.url)
        assert code != 0
        assert "x509" in err or "certificate" in err, err
        assert {t.authority for t in gateway.tunnels} == {f"{HOST}:{cluster.port}"}
        assert all(t.plaintext == b"" for t in gateway.tunnels)
        assert FakeKubernetes.seen == []
        gateway.tunnels.clear()

        code, out_, err = await kubectl(
            tmp_path, kubeconfig, "get", "pods", "-o", "name", proxy=gateway.url
        )
        assert code == 0, err
        assert out_.strip() == "pod/web-0"
        assert gateway.tunnels == []
        assert TOKEN.encode() not in cluster.relay.sent


async def test_grants_follow_rotation_attempts_and_revocation(
    db: Database, clock: FakeClock, tmp_path: Path, cluster: Cluster
) -> None:
    store = MemorySecretStore()
    await _world(db, store, "agent-a")
    iid = await _kubernetes(db, store, clock, cluster.server(), cluster.ca_data)
    task_id = await make_task(db, iid)
    await start(db, store, "agent-a")
    group = tmp_path / "group"
    async with ChannelHost(db, store, "agent-a") as host:
        out = await host.install(group, task_id, iid)
        assert out["ok"] is True, out
        first = _copy(_kubeconfig(out["installed"]), tmp_path / "first.kubeconfig")
        assert (await kubectl(tmp_path, first, "get", "pods"))[0] == 0

        # Rotation: the old version's grant is refused at once, the recheck re-grants
        # with the new token and the old grant is gone.
        rotate = service.IntegrationRotate.model_validate(
            {"secret": {"token": OTHER_TOKEN}}
        )
        await service.rotate(db, store, "demo", iid, rotate, "admin:test")
        await _validated(db, iid)
        FakeKubernetes.token = OTHER_TOKEN
        await refused_by_the_route(cluster, tmp_path, first)
        out = await host.recheck(group, out["installed"])
        assert out["state"] == "refreshed", out
        rotated = _kubeconfig(out["installed"])
        assert (await kubectl(tmp_path, rotated, "get", "pods"))[0] == 0
        err = await refused_by_the_route(cluster, tmp_path, first)
        assert "Proxy Authentication Required" in err, err

        # A new attempt: Argus fences the old one at once (its lease is gone), and
        # installing the new one drops the old grants.
        second = _copy(rotated, tmp_path / "second.kubeconfig")
        async with db.write() as tx:
            await tasks.release(tx, task_id, "admin:test", "moved")
        await start(db, store, "agent-a")
        await refused_by_the_route(cluster, tmp_path, second)
        out = await host.install(group, task_id, iid, attempt=2)
        assert out["ok"] is True, out
        current = _kubeconfig(out["installed"])
        assert (await kubectl(tmp_path, current, "get", "pods"))[0] == 0
        err = await refused_by_the_route(cluster, tmp_path, second)
        assert "Proxy Authentication Required" in err, err

        # Revocation: refused before the recheck, which removes the file.
        await service.revoke(db, store, "demo", iid, "admin:test")
        await refused_by_the_route(cluster, tmp_path, current)
        out = await host.recheck(group, out["installed"])
        assert out["state"] == "removed"
        assert not current.exists()
    assert TOKEN.encode() not in cluster.relay.sent
    assert OTHER_TOKEN.encode() not in cluster.relay.sent


async def test_one_workers_grant_never_reaches_another_tenants_cluster(
    db: Database, clock: FakeClock, tmp_path: Path, cluster: Cluster
) -> None:
    store = MemorySecretStore()
    await _world(db, store, "agent-a", "agent-b")
    await insert_project(db, "other")
    mine = await _kubernetes(db, store, clock, cluster.server(), cluster.ca_data)
    theirs = await _kubernetes(
        db, store, clock, cluster.server(HOST_B), cluster.ca_data, project="other"
    )
    task_a = await make_task(db, mine)
    await start(db, store, "agent-a")
    task_b = await make_task(db, theirs, project="other")
    await start(db, store, "agent-b")
    async with (
        ChannelHost(db, store, "agent-a") as host_a,
        ChannelHost(db, store, "agent-b") as host_b,
    ):
        out_a = await host_a.install(tmp_path / "a", task_a, mine)
        out_b = await host_b.install(tmp_path / "b", task_b, theirs)
        assert out_a["ok"] and out_b["ok"], (out_a, out_b)
        a, b = _kubeconfig(out_a["installed"]), _kubeconfig(out_b["installed"])
        assert (await kubectl(tmp_path, a, "get", "pods"))[0] == 0
        assert (await kubectl(tmp_path, b, "get", "pods"))[0] == 0
        # A's grant does not open B's cluster, nor does B's route take A's grant.
        await refused_by_the_route(
            cluster, tmp_path, a, f"--server={cluster.server(HOST_B)}"
        )
        proxy_b = json.loads(b.read_text())["clusters"][0]["cluster"]["proxy-url"]
        grant_a = json.loads(a.read_text())["clusters"][0]["cluster"]["proxy-url"]
        crossed = proxy_b.replace(proxy_b.split("@")[0], grant_a.split("@")[0])
        edited = _with(a, tmp_path / "crossed.kubeconfig", **{"proxy-url": crossed})
        err = await refused_by_the_route(cluster, tmp_path, edited)
        assert "Proxy Authentication Required" in err, err


@pytest.mark.parametrize(
    ("change", "reason", "detail"),
    [
        ("ca", "env_integration_gateway_intercepts", "not trusted"),
        ("host", "env_integration_gateway_intercepts", "does not name the server"),
        ("internal", "env_integration_route_unavailable", "loopback or link-local"),
        ("unknown", "env_integration_route_unavailable", "does not resolve"),
        ("closed_port", "env_integration_route_unavailable", "refused the CONNECT"),
    ],
)
async def test_the_route_refuses_before_any_kubeconfig(
    db: Database,
    clock: FakeClock,
    tmp_path: Path,
    cluster: Cluster,
    change: str,
    reason: str,
    detail: str,
) -> None:
    store = MemorySecretStore()
    await _world(db, store, "agent-a")
    iid = await _kubernetes(db, store, clock, cluster.server(), cluster.ca_data)
    task_id = await make_task(db, iid)
    await start(db, store, "agent-a")
    with socket.socket() as closed:  # a port nothing listens on
        closed.bind((HOST_IP or "", 0))
        closed_port = closed.getsockname()[1]
    other_ca = base64.b64encode(_server_tls(tmp_path / "other", "other")[1]).decode()
    override = {
        "ca": {"ca_data": other_ca},
        "host": {"server": cluster.server(UNNAMED)},
        "internal": {"server": cluster.server(INTERNAL)},
        "unknown": {"server": cluster.server("nowhere.example.test")},
        "closed_port": {"server": cluster.server(port=closed_port)},
    }[change]
    group = tmp_path / "group"
    async with ChannelHost(db, store, "agent-a") as host:
        host.overrides = override
        out = await host.install(group, task_id, iid)
    assert (out["ok"], out["reason"]) == (False, reason)
    assert detail in out["error"], out["error"]
    assert TOKEN not in json.dumps(out)
    assert not any(group.rglob("kubeconfig"))
    assert FakeKubernetes.seen == []
    assert TOKEN.encode() not in cluster.relay.sent
