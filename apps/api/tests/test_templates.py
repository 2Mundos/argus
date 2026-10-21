# ruff: noqa: F811  (fixtures imported from test_launch are redefined as arguments)
"""Guided worker templates (docs/specs/worker-launch.spec.md)."""

import os
import tempfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from api_support import FakeTarget
from fastapi.testclient import TestClient
from test_launch import (  # noqa: F401  (fixtures)
    ADMIN,
    agent_token,
    bearer,
    client,
    public_url,
    rpc,
    target,
    tool,
    wait_for,
)

ECHO = {"argus.worker": "plain", "argus.worker.capabilities": "python,git"}


def templates(client: TestClient, headers: dict = ADMIN) -> dict:
    res = client.get("/api/fleet/templates", headers=headers)
    assert res.status_code == 200, res.text
    return {t["id"]: t for t in res.json()["templates"]} | {"_": res.json()}


def create(client: TestClient, headers: dict = ADMIN, **body):
    return client.post("/api/fleet/workers", json=body, headers=headers)


def pools(client: TestClient) -> list[dict]:
    return client.get("/api/pools", headers=ADMIN).json()


@pytest.fixture
def short_dir() -> Iterator[Path]:
    # NanoClaw's instances_root is length-limited, and tmp_path is too long.
    with tempfile.TemporaryDirectory(prefix="a") as d:
        yield Path(d)


def nanoclaw_env(
    short_dir: Path,
    *,
    sandbox: bool = True,
    socket: bool = True,
    user: str | None = None,
    group_add: str | None = None,
) -> dict[str, str]:
    """Artifacts for both NanoClaw templates; `sandbox`/`socket` choose which
    template's own settings are present (the shared artifacts always are)."""
    install = short_dir / "nc"
    (install / "dist" / "channels").mkdir(parents=True)
    (install / "dist" / "index.js").write_text("")
    (install / "dist" / "channels" / "argus.js").write_text("")
    (install / "dist" / "gateway-providers").mkdir()
    (install / "dist" / "gateway-providers" / "onecli.js").write_text("")
    (install / "dist" / "gateway-providers" / "installed.js").write_text(
        "import './onecli.js';\n"
    )
    root = short_dir / "i"
    root.mkdir()
    env = {
        "ARGUS_NANOCLAW_INSTALL_DIR": str(install),
        "ARGUS_NANOCLAW_INSTANCES_ROOT": str(root),
        "ARGUS_NANOCLAW_AGENT_IMAGE": "nc-agent:1",
    }
    if sandbox:
        archive = short_dir / "agent.tar"
        archive.write_text("")
        env |= {
            "ARGUS_NANOCLAW_DOCKER_RUNTIME": "sysbox-runc",
            "ARGUS_NANOCLAW_AGENT_IMAGE_ARCHIVE": str(archive),
        }
    if socket:
        sock = short_dir / "docker.sock"  # a stand-in file: only mode/owner are read
        sock.write_text("")
        sock.chmod(0o660)
        env |= {
            "ARGUS_NANOCLAW_ALLOW_DOCKER_SOCKET": "true",
            "ARGUS_NANOCLAW_DOCKER_SOCKET": str(sock),
            "ARGUS_NANOCLAW_USER": user or f"{os.getuid()}:{os.getgid()}",
            "ARGUS_NANOCLAW_GROUP_ADD": group_add or str(os.getgid()),
        }
    return env


@pytest.fixture
def extra_env(request: pytest.FixtureRequest, short_dir: Path) -> dict[str, str]:
    """Per test: a dict, or a callable given a short temp dir (indirect param)."""
    param = getattr(request, "param", {})
    return param(short_dir) if callable(param) else param


def env_param(**kw):
    return pytest.mark.parametrize(
        "extra_env", [lambda d: nanoclaw_env(d, **kw)], indirect=True
    )


BOTH = env_param()
SANDBOX_ONLY = env_param(socket=False)
SOCKET_ONLY = env_param(sandbox=False)


# -- fresh install and discovery ------------------------------------------------------


def test_fresh_install_lists_templates_with_explicit_missing_prerequisites(
    client: TestClient, target: FakeTarget
) -> None:
    cat = templates(client)
    assert cat["_"]["pools"] == 0 and cat["_"]["default_node"] == "local"
    assert [n["id"] for n in cat["_"]["nodes"]] == ["local"]
    for t in ("nanoclaw-socket", "nanoclaw-sandbox", "argus-worker"):
        assert cat[t]["ready"] is False
    assert "nanoclaw" not in cat  # the ambiguous pre-split id is gone
    [nc] = cat["nanoclaw-socket"]["readiness"]
    assert any("ARGUS_NANOCLAW_INSTALL_DIR" in m for m in nc["missing"])
    assert any("ARGUS_NANOCLAW_ALLOW_DOCKER_SOCKET" in m for m in nc["missing"])
    [w] = cat["argus-worker"]["readiness"]
    assert any("argus.worker=plain" in m for m in w["missing"]) and w["images"] == []
    assert cat["nanoclaw-socket"]["defaults"]["name"] == "nanoclaw-socket"
    assert cat["nanoclaw-sandbox"]["defaults"]["name"] == "nanoclaw-sandbox"
    assert cat["nanoclaw-sandbox"]["setup"] and cat["argus-worker"]["setup"]
    # Looking creates nothing and starts nothing.
    assert pools(client) == [] and target.started == []


def test_no_reachable_node_makes_everything_unavailable(
    client: TestClient, target: FakeTarget
) -> None:
    target.labeled = {"echo:1": ECHO}
    target.reachable = False
    cat = templates(client)
    assert not cat["_"]["nodes"][0]["available"]
    for t in ("nanoclaw-socket", "nanoclaw-sandbox", "argus-worker"):
        assert cat[t]["ready"] is False
        assert any("Docker daemon" in m for m in cat[t]["readiness"][0]["missing"])
    res = create(client, template="argus-worker", name="w", image="echo:1")
    assert (
        res.status_code == 409 and res.json()["detail"]["code"] == "template_not_ready"
    )
    assert pools(client) == []


def test_only_labelled_images_are_offered(
    client: TestClient, target: FakeTarget
) -> None:
    target.labeled = {
        "echo:1": ECHO,
        "random:1": {"maintainer": "x"},
        "other:1": {"argus.worker": "something-else"},
    }
    [w] = templates(client)["argus-worker"]["readiness"]
    assert w["ready"] and w["images"] == [
        {"image": "echo:1", "capabilities": ["python", "git"], "source": "label"}
    ]


@pytest.mark.parametrize(
    "extra_env", [{"ARGUS_WORKER_IMAGES": "trusted:1,absent:1"}], indirect=True
)
def test_configured_images_must_be_present_on_the_node(
    client: TestClient, target: FakeTarget, extra_env: dict
) -> None:
    target.images = {"trusted:1"}
    [w] = templates(client)["argus-worker"]["readiness"]
    assert [i["image"] for i in w["images"]] == ["trusted:1"]
    assert w["images"][0]["source"] == "config"


# -- creating -------------------------------------------------------------------------


def test_create_from_scratch_starts_a_real_launch(
    client: TestClient, target: FakeTarget
) -> None:
    target.labeled = {"echo:1": ECHO}
    res = create(
        client, template="argus-worker", name="worker"
    )  # one image: chosen for the user
    assert res.status_code == 201, res.text
    body = res.json()
    assert (body["requested"], body["desired_replicas"]) == (1, 1)
    pool = body["pool"]
    assert (pool["name"], pool["template"], pool["runtime"]) == (
        "worker",
        "argus-worker",
        "plain",
    )
    assert (pool["image"], pool["capabilities"], pool["max_replicas"]) == (
        "echo:1",
        ["git", "python"],  # stored sorted
        3,
    )
    assert pool["docker"]["extra_hosts"] == ["host.docker.internal:host-gateway"]
    wait_for(
        lambda: any(
            i["state"] == "running"
            for i in client.get("/api/instances", headers=ADMIN).json()
        )
    )
    assert len(target.started) == 1
    # The suggested name moves on once taken.
    assert templates(client)["argus-worker"]["defaults"]["name"] == "worker-2"


def test_quantity_and_name_are_honoured(client: TestClient, target: FakeTarget) -> None:
    target.labeled = {"echo:1": ECHO}
    res = create(client, template="argus-worker", name="crew", quantity=2)
    pool = res.json()["pool"]
    assert (pool["name"], pool["desired_replicas"], pool["max_replicas"]) == (
        "crew",
        2,
        3,
    )
    assert create(client, template="argus-worker", name="Bad Name").status_code == 422
    assert (
        create(client, template="argus-worker", name="x", quantity=0).status_code == 422
    )


def test_server_rejects_forged_requests(client: TestClient, target: FakeTarget) -> None:
    target.labeled = {"echo:1": ECHO}
    bad = [
        ({"template": "ghost", "name": "w"}, "unknown_template"),
        ({"template": "argus-worker", "name": "w", "image": "evil:1"}, "unknown_image"),
        (
            {"template": "argus-worker", "name": "w", "node": "ssh://attacker"},
            "unknown_node",
        ),
        (
            {"template": "nanoclaw-socket", "name": "w", "image": "evil:1"},
            "unknown_image",
        ),
        ({"template": "nanoclaw", "name": "w"}, "unknown_template"),
    ]
    for body, code in bad:
        res = create(client, **body)
        assert res.status_code in (409, 422), (body, res.text)
        assert res.json()["detail"]["code"] in (code, "template_not_ready"), body
    # Fields the client must not control are ignored, not honoured.
    res = create(
        client,
        template="argus-worker",
        name="w",
        runtime="nanoclaw",
        target="kubernetes",
        nanoclaw={"install_dir": "/etc"},
        max_replicas=1000,
    )
    pool = res.json()["pool"]
    assert (
        pool["runtime"],
        pool["target"],
        pool["nanoclaw"],
        pool["max_replicas"],
    ) == (
        "plain",
        "docker",
        None,
        3,
    )


def test_image_choice_is_required_when_ambiguous(
    client: TestClient, target: FakeTarget
) -> None:
    target.labeled = {"a:1": ECHO, "b:1": ECHO}
    res = create(client, template="argus-worker", name="w")
    assert res.json()["detail"]["code"] == "image_required" and pools(client) == []
    assert (
        create(client, template="argus-worker", name="w", image="b:1").status_code
        == 201
    )


def test_advanced_options_for_admin_only_and_validated(
    client: TestClient, target: FakeTarget
) -> None:
    target.labeled = {"echo:1": ECHO}
    orch = bearer(agent_token(client, "orchestrator"))
    adv = {
        "capabilities": ["gpu"],
        "env": {"MODE": "x"},
        "extra_hosts": ["db:10.0.0.2"],
    }
    denied = create(client, orch, template="argus-worker", name="w", advanced=adv)
    assert denied.status_code == 403 and pools(client) == []
    assert (
        create(
            client, template="argus-worker", advanced={"env": {"1 bad": "x"}}
        ).status_code
        == 422
    )
    ok = create(client, template="argus-worker", name="w", advanced=adv).json()["pool"]
    assert (ok["capabilities"], ok["env"], ok["docker"]["extra_hosts"]) == (
        ["gpu"],
        {"MODE": "***"},  # values are write-only on every response
        ["db:10.0.0.2"],
    )


# -- NanoClaw: two explicit templates --------------------------------------------------


def ready_node(target: FakeTarget, *, sysbox: bool = True, images: bool = True) -> None:
    target.shares_filesystem = True
    target.runtimes = ["runc", "sysbox-runc"] if sysbox else ["runc"]
    target.images = {"argus-nanoclaw-host:dev", "nc-agent:1"} if images else set()


def readiness(client: TestClient, template: str) -> dict:
    [r] = templates(client)[template]["readiness"]
    return r


@BOTH
def test_both_choices_listed_with_independent_readiness_and_visible_warning(
    client: TestClient, target: FakeTarget, extra_env: dict
) -> None:
    ready_node(target)
    cat = templates(client)
    assert cat["nanoclaw-socket"]["ready"] and cat["nanoclaw-sandbox"]["ready"]
    assert cat["nanoclaw-socket"]["name"] != cat["nanoclaw-sandbox"]["name"]
    assert "root-equivalent" in cat["nanoclaw-socket"]["warning"]
    assert cat["nanoclaw-sandbox"]["warning"] is None


@BOTH
def test_each_selection_binds_its_own_isolation(
    client: TestClient, target: FakeTarget, extra_env: dict
) -> None:
    ready_node(target)
    box = create(client, template="nanoclaw-sandbox", name="boxed").json()["pool"]
    sock = create(client, template="nanoclaw-socket", name="socketed").json()["pool"]
    assert (box["template"], box["docker"]["runtime"]) == (
        "nanoclaw-sandbox",
        "sysbox-runc",
    )
    assert (
        box["nanoclaw"]["agent_image_archive"]
        == extra_env["ARGUS_NANOCLAW_AGENT_IMAGE_ARCHIVE"]
    )
    assert box["nanoclaw"]["user"] is None and box["nanoclaw"]["group_add"] == []
    assert box["nanoclaw"]["docker_socket"] is None
    uid_gid = f"{os.getuid()}:{os.getgid()}"
    assert (sock["template"], sock["docker"]["runtime"]) == ("nanoclaw-socket", None)
    assert sock["nanoclaw"]["agent_image_archive"] is None
    assert (sock["nanoclaw"]["user"], sock["nanoclaw"]["group_add"]) == (
        uid_gid,
        [str(os.getgid())],
    )
    assert (
        sock["nanoclaw"]["docker_socket"] == extra_env["ARGUS_NANOCLAW_DOCKER_SOCKET"]
    )
    for pool in (box, sock):
        assert (pool["runtime"], pool["agents_per_instance"]) == ("nanoclaw", 1)
    # What was stored is what is read back (editing preserves it, see the dashboard test).
    stored = {p["name"]: p for p in pools(client)}
    assert stored["boxed"]["docker"]["runtime"] == "sysbox-runc"
    assert stored["socketed"]["docker"]["runtime"] is None


GATEWAY = pytest.mark.parametrize(
    "extra_env",
    [
        lambda d: (
            nanoclaw_env(d)
            | {
                "ARGUS_NANOCLAW_ONECLI_URL": "http://onecli:10254",
                "ARGUS_NANOCLAW_ACCOUNT_PROVIDERS": "openai, anthropic",
                "ARGUS_NANOCLAW_EXECUTOR_PROVIDERS": "cursor",
                "ARGUS_NANOCLAW_NETWORK": "onecli-control",
            }
        )
    ],
    indirect=True,
)


@GATEWAY
def test_guided_nanoclaw_worker_inherits_the_gateway_and_ignores_retired_provider_env(
    client: TestClient, target: FakeTarget, extra_env: dict
) -> None:
    ready_node(target)
    pool = create(client, template="nanoclaw-socket", name="inherits").json()["pool"]
    assert pool["nanoclaw"]["onecli_url"] == "http://onecli:10254"
    # The retired provider env never becomes pool configuration.
    assert "account_providers" not in pool["nanoclaw"]
    assert "executor_providers" not in pool["nanoclaw"]
    assert pool["nanoclaw"]["agent_image"] == extra_env["ARGUS_NANOCLAW_AGENT_IMAGE"]
    assert pool["docker"]["network"] == "onecli-control"
    # The isolation is still the template's own.
    assert pool["docker"]["runtime"] is None


@BOTH
def test_without_operator_gateway_settings_the_pool_keeps_its_defaults(
    client: TestClient, target: FakeTarget, extra_env: dict
) -> None:
    ready_node(target)
    pool = create(client, template="nanoclaw-socket", name="plain-defaults").json()[
        "pool"
    ]
    assert pool["nanoclaw"]["onecli_url"] == "http://host.docker.internal:10254"
    assert "account_providers" not in pool["nanoclaw"]
    assert "executor_providers" not in pool["nanoclaw"]
    assert pool["docker"]["network"] is None


@BOTH
def test_missing_sandbox_runtime_does_not_block_the_approved_socket_choice(
    client: TestClient, target: FakeTarget, extra_env: dict
) -> None:
    ready_node(target, sysbox=False)
    cat = templates(client)
    assert cat["nanoclaw-socket"]["ready"] is True
    assert cat["nanoclaw-sandbox"]["ready"] is False
    missing = " | ".join(readiness(client, "nanoclaw-sandbox")["missing"])
    assert "sysbox-runc" in missing and "ARGUS_NANOCLAW_DOCKER_RUNTIME" in missing
    assert create(client, template="nanoclaw-socket", name="s").status_code == 201
    assert wait_for(lambda: target.started)
    spec = next(iter(target.specs.values()))
    assert spec.runtime is None
    mounts = {m.container: m for m in spec.mounts}
    assert (
        "/var/run/docker.sock" in mounts and mounts["/var/run/docker.sock"].mode == "rw"
    )
    assert spec.user and spec.group_add


@BOTH
def test_a_checkout_without_the_gateway_provider_is_not_ready_for_either_mode(
    client: TestClient, target: FakeTarget, extra_env: dict
) -> None:
    ready_node(target)
    Path(
        extra_env["ARGUS_NANOCLAW_INSTALL_DIR"], "dist/gateway-providers/onecli.js"
    ).unlink()
    for t in ("nanoclaw-socket", "nanoclaw-sandbox"):
        r = readiness(client, t)
        assert not r["ready"] and any("OneCLI gateway" in m for m in r["missing"])
    assert create(client, template="nanoclaw-socket", name="s").status_code == 409


@BOTH
def test_a_provider_file_that_nothing_imports_is_not_a_registered_gateway(
    client: TestClient, target: FakeTarget, extra_env: dict
) -> None:
    ready_node(target)
    barrel = Path(
        extra_env["ARGUS_NANOCLAW_INSTALL_DIR"], "dist/gateway-providers/installed.js"
    )
    barrel.write_text("export {};\n")  # onecli.js is still there, but never imported
    for t in ("nanoclaw-socket", "nanoclaw-sandbox"):
        assert not readiness(client, t)["ready"]
    barrel.write_text(
        "// import './onecli.js';\nexport {};\n"
    )  # a comment does not count
    assert not readiness(client, "nanoclaw-socket")["ready"]
    barrel.write_text("import './onecli.js';\n")
    assert readiness(client, "nanoclaw-socket")["ready"]


@BOTH
def test_a_missing_runtime_never_falls_back_to_the_other_mode(
    client: TestClient, target: FakeTarget, extra_env: dict
) -> None:
    ready_node(target, sysbox=False)
    res = create(client, template="nanoclaw-sandbox", name="boxed")
    assert (
        res.status_code == 409 and res.json()["detail"]["code"] == "template_not_ready"
    )
    assert pools(client) == [] and target.started == []


@SANDBOX_ONLY
def test_artifacts_without_the_operator_opt_in_never_enable_the_socket(
    client: TestClient, target: FakeTarget, extra_env: dict
) -> None:
    ready_node(target)
    assert templates(client)["nanoclaw-sandbox"]["ready"] is True
    sock = readiness(client, "nanoclaw-socket")
    assert not sock["ready"]
    assert any("ARGUS_NANOCLAW_ALLOW_DOCKER_SOCKET" in m for m in sock["missing"])
    res = create(client, template="nanoclaw-socket", name="s")
    assert res.status_code == 409 and pools(client) == []


@BOTH
def test_request_cannot_override_isolation(
    client: TestClient, target: FakeTarget, extra_env: dict
) -> None:
    ready_node(target)
    sock = create(
        client,
        template="nanoclaw-socket",
        name="s",
        docker={"runtime": "sysbox-runc", "host": "ssh://x"},
        nanoclaw={"docker_socket": "/etc", "agent_image_archive": "/etc/passwd"},
        advanced={"env": {"A": "b"}},
    ).json()["pool"]
    assert sock["docker"]["runtime"] is None and sock["docker"]["host"] is None
    assert sock["nanoclaw"]["agent_image_archive"] is None
    assert (
        sock["nanoclaw"]["docker_socket"] == extra_env["ARGUS_NANOCLAW_DOCKER_SOCKET"]
    )
    box = create(
        client, template="nanoclaw-sandbox", name="b", docker={"runtime": None}
    ).json()["pool"]
    assert box["docker"]["runtime"] == "sysbox-runc"
    assert box["nanoclaw"]["docker_socket"] is None
    assert create(client, template="nanoclaw", name="old").status_code == 422


@pytest.mark.parametrize(
    "extra_env",
    [lambda d: nanoclaw_env(d, user="99998:99998", group_add="99999")],
    indirect=True,
)
def test_socket_template_checks_that_the_user_can_reach_the_socket(
    client: TestClient, target: FakeTarget, extra_env: dict
) -> None:
    ready_node(target)
    sock = readiness(client, "nanoclaw-socket")
    access = next(c for c in sock["checks"] if c["name"] == "docker_socket_access")
    assert access["ok"] is False and not sock["ready"]
    assert any("ARGUS_NANOCLAW_GROUP_ADD" in m for m in sock["missing"])
    assert readiness(client, "nanoclaw-sandbox")["ready"]  # independent


@BOTH
def test_socket_template_needs_the_agent_image_on_the_node(
    client: TestClient, target: FakeTarget, extra_env: dict
) -> None:
    ready_node(target, images=False)
    target.images = {"argus-nanoclaw-host:dev"}  # host image only
    sock = readiness(client, "nanoclaw-socket")
    assert not sock["ready"] and any("nc-agent:1" in m for m in sock["missing"])
    # The sandbox loads its image from the archive, so the node need not have it.
    assert readiness(client, "nanoclaw-sandbox")["ready"]


@BOTH
def test_nothing_is_verified_on_a_remote_or_vm_daemon(
    client: TestClient, target: FakeTarget, extra_env: dict
) -> None:
    ready_node(target)
    target.shares_filesystem = False
    for t in ("nanoclaw-socket", "nanoclaw-sandbox"):
        r = readiness(client, t)
        assert not r["ready"] and any(
            "cannot be verified" in m or "not verifiable" in m for m in r["missing"]
        )
    assert create(client, template="nanoclaw-socket", name="s").status_code == 409


@BOTH
def test_orchestrator_gets_the_same_catalog_and_policy_governs_the_socket(
    client: TestClient, target: FakeTarget, extra_env: dict
) -> None:
    ready_node(target)
    orch_token = agent_token(client, "orchestrator")
    via_mcp = tool(client, orch_token, "list_worker_templates")
    via_rest = client.get("/api/fleet/templates", headers=ADMIN).json()
    assert via_mcp == via_rest
    socket_tpl = next(t for t in via_mcp["templates"] if t["id"] == "nanoclaw-socket")
    assert "root-equivalent" in socket_tpl["warning"]  # visible to the orchestrator
    made = tool(
        client, orch_token, "create_worker", template="nanoclaw-socket", name="o"
    )
    assert made["pool"]["template"] == "nanoclaw-socket"
    worker = agent_token(client, "worker")
    denied = tool(client, worker, "create_worker", template="nanoclaw-socket", name="w")
    assert denied["error"].startswith("403 forbidden")


@SANDBOX_ONLY
def test_orchestrator_cannot_use_the_socket_without_operator_opt_in(
    client: TestClient, target: FakeTarget, extra_env: dict
) -> None:
    ready_node(target)
    orch_token = agent_token(client, "orchestrator")
    res = tool(
        client, orch_token, "create_worker", template="nanoclaw-socket", name="o"
    )
    assert res["error"].startswith("409 template_not_ready") and pools(client) == []


# -- scopes ---------------------------------------------------------------------------


def test_workers_cannot_see_or_provision(
    client: TestClient, target: FakeTarget
) -> None:
    target.labeled = {"echo:1": ECHO}
    worker = bearer(agent_token(client, "worker"))
    assert client.get("/api/fleet/templates", headers=worker).status_code == 403
    assert create(client, worker, template="argus-worker", name="w").status_code == 403
    assert (
        client.post(
            "/api/fleet/workers", json={"template": "argus-worker", "name": "w"}
        ).status_code
        == 401
    )
    assert pools(client) == []


def test_orchestrator_creates_from_templates_within_bounds(
    client: TestClient, target: FakeTarget
) -> None:
    target.labeled = {"echo:1": ECHO}
    orch = bearer(agent_token(client, "orchestrator"))
    assert templates(client, orch)["argus-worker"]["ready"]
    assert (
        create(client, orch, template="argus-worker", name="w", quantity=4).status_code
        == 403
    )
    assert (
        create(client, orch, template="argus-worker", name="w", quantity=2).status_code
        == 201
    )


# -- no double launches ---------------------------------------------------------------


def test_retry_creates_neither_a_second_pool_nor_a_second_worker(
    client: TestClient, target: FakeTarget
) -> None:
    target.labeled = {"echo:1": ECHO}
    assert create(client, template="argus-worker", name="crew").status_code == 201
    again = create(client, template="argus-worker", name="crew")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "pool_exists"
    [pool] = pools(client)
    assert pool["desired_replicas"] == 1
    wait_for(lambda: target.started)
    assert len(target.started) == 1


def test_concurrent_submissions_create_exactly_one(
    client: TestClient, target: FakeTarget
) -> None:
    target.labeled = {"echo:1": ECHO}
    with ThreadPoolExecutor(6) as ex:
        results = list(
            ex.map(
                lambda _: create(client, template="argus-worker", name="crew"), range(6)
            )
        )
    codes = sorted(r.status_code for r in results)
    assert codes == [201] + [409] * 5
    assert len(pools(client)) == 1 and pools(client)[0]["desired_replicas"] == 1


def test_failed_write_leaves_no_pool_or_desired_state(
    client: TestClient, target: FakeTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    from argus_api.fleet import service

    target.labeled = {"echo:1": ECHO}
    real = service.record

    async def boom(tx, actor, subject_type, subject_id, kind, data=None):
        if kind == "pool.created":
            raise RuntimeError("disk full")
        return await real(tx, actor, subject_type, subject_id, kind, data)

    monkeypatch.setattr(service, "record", boom)
    with pytest.raises(RuntimeError):
        create(client, template="argus-worker", name="crew")
    monkeypatch.setattr(service, "record", real)
    assert pools(client) == [] and target.started == []
    assert create(client, template="argus-worker", name="crew").status_code == 201


# -- MCP parity -----------------------------------------------------------------------


def test_mcp_catalog_and_create_match_rest(
    client: TestClient, target: FakeTarget
) -> None:
    target.labeled = {"echo:1": ECHO}
    orch = agent_token(client, "orchestrator")
    worker = agent_token(client, "worker")
    via_mcp = tool(client, orch, "list_worker_templates")
    via_rest = client.get("/api/fleet/templates", headers=ADMIN).json()
    assert via_mcp == via_rest
    assert tool(client, worker, "list_worker_templates")["error"].startswith(
        "403 forbidden"
    )
    assert tool(client, worker, "create_worker", template="argus-worker", name="w")[
        "error"
    ].startswith("403 forbidden")
    ghost = tool(client, orch, "create_worker", template="ghost", name="w")
    assert ghost["error"].startswith("422 unknown_template")
    adv = tool(
        client,
        orch,
        "create_worker",
        template="argus-worker",
        name="w",
        advanced={"env": {"A": "b"}},
    )
    assert adv["error"].startswith("403 forbidden")
    made = tool(client, orch, "create_worker", template="argus-worker", name="mcp-crew")
    assert made["requested"] == 1 and made["pool"]["template"] == "argus-worker"
    dup = tool(client, orch, "create_worker", template="argus-worker", name="mcp-crew")
    assert dup["error"].startswith("409 pool_exists")
    assert [p["name"] for p in pools(client)] == ["mcp-crew"]


# -- retries, names, caps ---------------------------------------------------------------


def test_omitted_name_is_rejected_so_a_retry_cannot_silently_duplicate(
    client: TestClient, target: FakeTarget
) -> None:
    target.labeled = {"echo:1": ECHO}
    suggested = templates(client)["argus-worker"]["defaults"]["name"]
    first = create(client, template="argus-worker", name=suggested)
    assert first.status_code == 201
    for _ in range(2):  # the same public call, retried, with and without the name
        assert create(client, template="argus-worker").status_code == 422
        assert (
            create(client, template="argus-worker", name=suggested).status_code == 409
        )
    assert [p["name"] for p in pools(client)] == [suggested]
    assert pools(client)[0]["desired_replicas"] == 1
    # The catalog moves on to a new suggestion; using it is a new, explicit choice.
    assert templates(client)["argus-worker"]["defaults"]["name"] == f"{suggested}-2"


def test_mcp_create_worker_requires_a_name(
    client: TestClient, target: FakeTarget
) -> None:
    target.labeled = {"echo:1": ECHO}
    orch = agent_token(client, "orchestrator")
    schema = next(
        t
        for t in rpc(client, orch, "tools/list")["tools"]
        if t["name"] == "create_worker"
    )["inputSchema"]
    assert "name" in schema["required"]
    missing = tool(client, orch, "create_worker", template="argus-worker")
    assert "name" in missing["error"] and pools(client) == []


def test_cap_defaults_and_explicit_cap(client: TestClient, target: FakeTarget) -> None:
    target.labeled = {"echo:1": ECHO}
    by_default = create(client, template="argus-worker", name="a", quantity=5).json()
    assert by_default["pool"]["max_replicas"] == 5  # max(quantity, 3)
    plain = create(client, template="argus-worker", name="b").json()
    assert plain["pool"]["max_replicas"] == 3
    explicit = create(
        client,
        template="argus-worker",
        name="c",
        quantity=2,
        advanced={"max_replicas": 8},
    ).json()
    assert (explicit["pool"]["max_replicas"], explicit["desired_replicas"]) == (8, 2)
    # An advanced block that leaves the cap alone does not change the default.
    kept = create(
        client, template="argus-worker", name="d", advanced={"capabilities": ["x"]}
    ).json()
    assert kept["pool"]["max_replicas"] == 3


def test_invalid_cap_is_rejected_before_anything_is_written(
    client: TestClient, target: FakeTarget
) -> None:
    target.labeled = {"echo:1": ECHO}
    low = create(
        client,
        template="argus-worker",
        name="a",
        quantity=3,
        advanced={"max_replicas": 2},
    )
    assert low.status_code == 422 and low.json()["detail"]["code"] == "invalid_cap"
    for bad in (0, -1, 1001):
        res = create(
            client, template="argus-worker", name="a", advanced={"max_replicas": bad}
        )
        assert res.status_code == 422
    assert pools(client) == []


def test_only_admin_can_set_the_cap(client: TestClient, target: FakeTarget) -> None:
    target.labeled = {"echo:1": ECHO}
    orch = bearer(agent_token(client, "orchestrator"))
    res = create(
        client, orch, template="argus-worker", name="a", advanced={"max_replicas": 9}
    )
    assert res.status_code == 403 and pools(client) == []
    orch_token = agent_token(client, "orchestrator")
    via_mcp = tool(
        client,
        orch_token,
        "create_worker",
        template="argus-worker",
        name="a",
        advanced={"max_replicas": 9},
    )
    assert via_mcp["error"].startswith("403 forbidden") and pools(client) == []
