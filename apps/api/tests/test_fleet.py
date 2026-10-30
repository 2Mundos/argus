import json
from dataclasses import replace
from pathlib import Path

import pytest
from api_support import DONE, FakeClock, FakeTarget
from argus_api import model_policy
from argus_api.agents import service as agents
from argus_api.auth import Identity
from argus_api.db import Database
from argus_api.decisions import service as decisions
from argus_api.errors import UnauthorizedError
from argus_api.fleet import service as fleet
from argus_api.fleet.reconciler import FleetSettings, reconcile_pool
from argus_api.fleet.targets import TargetRegistry, container_name
from argus_api.reaper import reap
from argus_api.tasks import service as tasks
from argus_core.agents import AgentRegister
from argus_core.auth import Scope
from argus_core.decisions import DecisionAnswer
from argus_core.fleet import PoolUpsert
from argus_core.tasks import TaskCreate

SETTINGS = FleetSettings(argus_url="http://argus.test:8000", max_starts_in_flight=64)
ADMIN = Identity("t", Scope.ADMIN, None, "test")


@pytest.fixture
def target() -> FakeTarget:
    return FakeTarget()


@pytest.fixture
def registry(target: FakeTarget) -> TargetRegistry:
    r = TargetRegistry()
    r.override("docker", None, target)
    return r


async def make_pool(db: Database, desired: int, max_replicas: int = 5, **kw) -> None:
    async with db.write() as tx:
        await fleet.upsert_pool(
            tx,
            "web",
            PoolUpsert(
                image="worker:1",
                desired_replicas=desired,
                max_replicas=max_replicas,
                **kw,
            ),
            "admin:test",
        )


async def tick(db: Database, registry: TargetRegistry, settings=SETTINGS) -> None:
    await reconcile_pool(db, "web", registry, settings)


async def instances(db: Database, include_terminated: bool = False):
    async with db.read() as conn:
        return await fleet.list_instances(conn, "web", include_terminated)


async def register_from(db: Database, target: FakeTarget, name: str) -> str:
    """What the worker in the container does: redeem its bootstrap token."""
    token = target.specs[name].env["ARGUS_BOOTSTRAP_TOKENS"].split(",")[0]
    async with db.write() as tx:
        reg = await agents.register(tx, AgentRegister(bootstrap_token=token))
    return reg.agent.id


async def scale(db: Database, desired: int, scope: Scope = Scope.ADMIN):
    async with db.write() as tx:
        return await fleet.scale(tx, "web", desired, "test", scope)


# -- scaling up ---------------------------------------------------------------------


async def test_scale_up_starts_instances_with_working_bootstrap_tokens(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    await make_pool(db, desired=2, capabilities=["python"], env={"MODE": "x"})
    await tick(db, registry)
    assert [i.state for i in await instances(db)] == ["reserved", "reserved"]
    await tick(db, registry)  # the listing confirms both
    live = await instances(db)
    assert [i.state for i in live] == ["running", "running"]
    assert len(target.started) == 2
    spec = target.specs[live[0].provider_ref]
    assert spec.env["ARGUS_URL"] == "http://argus.test:8000"
    assert (spec.env["ARGUS_CAPABILITIES"], spec.env["MODE"]) == ("python", "x")
    agent_id = await register_from(db, target, live[0].provider_ref)
    async with db.read() as conn:
        assert (await agents.get_agent(conn, agent_id)).instance_id == live[0].id
    await tick(db, registry)
    assert len(target.started) == 2  # converged: nothing more to do


async def test_no_public_url_means_no_starts(
    db: Database, registry: TargetRegistry
) -> None:
    await make_pool(db, desired=1)
    await tick(db, registry, FleetSettings(argus_url=None, max_starts_in_flight=64))
    async with db.read() as conn:
        pool = await fleet.get_pool(conn, "web")
    assert (pool.status, pool.live_instances) == ("failing", 0)
    assert "ARGUS_PUBLIC_URL" in (pool.last_error or "")


async def test_ambiguous_start_never_creates_a_second_container(
    db: Database, registry: TargetRegistry, target: FakeTarget, clock: FakeClock
) -> None:
    await make_pool(db, desired=1)
    target.ambiguous_start = True
    await tick(db, registry)
    [inst] = await instances(db)
    assert inst.state == "reserved"  # unknown outcome: slot kept, not confirmed
    assert inst.last_error
    target.ambiguous_start = False
    clock.advance(5)
    await tick(db, registry)  # the lookup finds it
    [inst] = await instances(db)
    assert inst.state == "running"
    assert len(target.containers) == 1


async def test_failing_starts_back_off(
    db: Database, registry: TargetRegistry, target: FakeTarget, clock: FakeClock
) -> None:
    await make_pool(db, desired=1)
    target.fail_start = True
    await tick(db, registry)
    await tick(db, registry)  # too soon to retry
    [inst] = await instances(db)
    assert inst.start_attempts == 1
    clock.advance(5)
    await tick(db, registry)
    [inst] = await instances(db)
    assert inst.start_attempts == 2
    async with db.read() as conn:
        assert (await fleet.get_pool(conn, "web")).status == "failing"


# -- provider failures ------------------------------------------------------------------


async def test_unreachable_target_changes_nothing(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    await make_pool(db, desired=1)
    await tick(db, registry)
    await tick(db, registry)  # confirmed running
    target.reachable = False
    await scale(db, 3)
    await tick(db, registry)
    async with db.read() as conn:
        pool = await fleet.get_pool(conn, "web")
    assert (pool.status, pool.live_instances) == ("unreachable", 1)
    [inst] = await instances(db)
    assert inst.state == "running"  # not presumed dead
    target.reachable = True
    await tick(db, registry)
    assert len(await instances(db)) == 3


async def test_lost_agent_terminates_and_keeps_the_slot_until_confirmed(
    db: Database, registry: TargetRegistry, target: FakeTarget, clock: FakeClock
) -> None:
    await make_pool(db, desired=1)
    await tick(db, registry)
    await tick(db, registry)  # confirmed running
    [inst] = await instances(db)
    agent_id = await register_from(db, target, inst.provider_ref)
    clock.advance(91)
    async with db.write() as tx:
        assert (await reap(tx)).lost_agents == [agent_id]
    target.fail_stop = True
    await tick(db, registry)
    [inst] = await instances(db)
    assert inst.state == "terminating"
    assert len(await instances(db)) == 1  # no replacement while unconfirmed
    await tick(db, registry)  # stop retried, still failing
    assert len(await instances(db)) == 1
    target.fail_stop = False
    await tick(db, registry)  # stop succeeds; not yet confirmed
    [still] = await instances(db)
    assert still.id == inst.id
    await tick(db, registry)  # the listing confirms it is gone; replace it
    live = await instances(db)
    assert len(live) == 1
    assert live[0].id != inst.id


async def test_unexpected_exit_ends_agents_and_replaces(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    await make_pool(db, desired=1)
    await tick(db, registry)
    await tick(db, registry)  # confirmed running
    [inst] = await instances(db)
    agent_id = await register_from(db, target, inst.provider_ref)
    async with db.write() as tx:
        token = (await agents.create_bootstrap_token(tx, _boot(inst.id), "t")).token
    target.exit(inst.provider_ref, code=137)
    await tick(db, registry)
    [old] = [i for i in await instances(db, True) if i.id == inst.id]
    assert (old.state, old.exit_reason) == ("terminated", "exited (137)")
    async with db.read() as conn:
        assert (await agents.get_agent(conn, agent_id)).state == "lost"
    with pytest.raises(UnauthorizedError):  # the instance's spare bootstrap is revoked
        async with db.write() as tx:
            await agents.register(tx, AgentRegister(bootstrap_token=token))
    assert inst.provider_ref not in target.containers  # exited container removed
    assert len(await instances(db)) == 1  # replacement


async def test_restart_during_termination_continues(
    db: Database, target: FakeTarget
) -> None:
    await make_pool(db, desired=1)
    first = TargetRegistry()
    first.override("docker", None, target)
    await tick(db, first)
    [inst] = await instances(db)
    async with db.write() as tx:
        await fleet.request_termination(tx, inst.id, "test")
    target.fail_stop = True
    await tick(db, first)
    # Argus restarts: fresh registry and reconciler, same database and provider.
    target.fail_stop = False
    second = TargetRegistry()
    second.override("docker", None, target)
    await tick(db, second)
    await tick(db, second)
    assert [i.state for i in await instances(db, True) if i.id == inst.id] == [
        "terminated"
    ]


# -- scaling down -----------------------------------------------------------------------


async def test_scale_down_drains_idle_and_spares_busy(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    await make_pool(db, desired=2)
    await tick(db, registry)
    a, b = await instances(db)
    agent_a = await register_from(db, target, a.provider_ref)
    agent_b = await register_from(db, target, b.provider_ref)
    async with db.write() as tx:  # agent_b is busy
        await tasks.create_task(
            tx, TaskCreate(project="global", title="t"), "admin:test"
        )
        task = await tasks.claim(tx, agent_b, [])
    assert task is not None
    await scale(db, 1)
    await tick(db, registry)
    async with db.read() as conn:
        states = {
            i: (await agents.get_agent(conn, i)).state for i in (agent_a, agent_b)
        }
    assert states == {agent_a: "draining", agent_b: "active"}
    async with db.write() as tx:
        await agents.leave(tx, agent_a)  # the drained worker exits cleanly
    await tick(db, registry)  # all its agents left: stop the container
    await tick(db, registry)  # confirmed
    live = await instances(db)
    assert [i.id for i in live] == [b.id]
    async with db.write() as tx:  # busy work finished undisturbed
        await tasks.complete(tx, agent_b, task.id, task.attempt, DONE)


async def test_agent_never_registers(
    db: Database, registry: TargetRegistry, target: FakeTarget, clock: FakeClock
) -> None:
    await make_pool(db, desired=1)
    await tick(db, registry)
    await tick(db, registry)  # confirmed running
    clock.advance(301)
    await tick(db, registry)
    [inst] = await instances(db)
    assert (inst.state, inst.exit_reason) == (
        "terminating",
        "no agent registered in time",
    )


async def test_orphan_containers_are_stopped(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    await make_pool(db, desired=0)
    target.containers[container_name("ghost")] = {
        "instance_id": "ghost",
        "pool": "web",
        "status": "running",
        "exit_code": None,
    }
    await tick(db, registry)
    assert target.stopped == [container_name("ghost")]


# -- the cap and the policy gate -----------------------------------------------------------


async def test_orchestrator_above_cap_needs_a_human(
    db: Database, registry: TargetRegistry
) -> None:
    await make_pool(db, desired=1, max_replicas=2)
    pool, decision_id = await scale(db, 2, Scope.ORCHESTRATOR)
    assert (pool.desired_replicas, decision_id) == (2, None)
    pool, decision_id = await scale(db, 6, Scope.ORCHESTRATOR)
    assert decision_id is not None
    assert (pool.desired_replicas, pool.max_replicas) == (2, 2)
    async with db.write() as tx:
        await decisions.answer(tx, decision_id, DecisionAnswer(choice="approve"), ADMIN)
    async with db.read() as conn:
        pool = await fleet.get_pool(conn, "web")
    assert (pool.desired_replicas, pool.max_replicas) == (6, 6)


def _boot(instance_id: str):
    from argus_core.agents import BootstrapTokenCreate

    return BootstrapTokenCreate(instance_id=instance_id)


async def test_crash_looping_instances_back_off(
    db: Database, registry: TargetRegistry, target: FakeTarget, clock: FakeClock
) -> None:
    await make_pool(db, desired=1)

    async def crash_once() -> None:
        await tick(db, registry)  # start (or replace)
        [inst] = await instances(db)
        clock.advance(3)  # dies within seconds of starting
        target.exit(inst.provider_ref, code=1)
        await tick(db, registry)  # confirmed exited; maybe replaced

    for _ in range(3):
        await crash_once()
    started = len(target.started)
    await tick(db, registry)
    assert len(target.started) == started  # held: three quick crashes in a row
    async with db.read() as conn:
        pool = await fleet.get_pool(conn, "web")
    assert pool.status == "failing"
    assert "exited within a minute" in (pool.last_error or "")
    clock.advance(11)  # the first hold is 10 s
    await tick(db, registry)
    assert len(target.started) == started + 1


def test_nanoclaw_options_validation() -> None:
    from argus_core.fleet import NanoclawOptions
    from pydantic import ValidationError

    ok = {"install_dir": "/opt/nc", "agent_image": "img:1"}
    NanoclawOptions(instances_root="/srv/nc-i", **ok)
    NanoclawOptions(
        instances_root="/srv/nc-i",
        repositories={"argus": "https://git.example.test/team/argus"},
        **ok,
    )
    NanoclawOptions(
        instances_root="/srv/nc-i",
        repositories={"argus": "https://git.example.test/team/argus.git"},
        **ok,
    )
    with pytest.raises(ValidationError):
        NanoclawOptions(
            instances_root="/srv/nc-i",
            repositories={"argus": "http://git.example.test/team/argus"},
            **ok,
        )
    with pytest.raises(ValidationError):
        NanoclawOptions(
            instances_root="/srv/nc-i",
            repositories={"argus": "https://git.example.test/team/argus/extra"},
            **ok,
        )
    with pytest.raises(ValidationError):  # a socket path would exceed the OS limit
        NanoclawOptions(instances_root="/" + "x" * 60, **ok)
    with pytest.raises(ValidationError):  # must be absolute
        NanoclawOptions(instances_root="relative", **ok)
    with pytest.raises(ValidationError):  # one agent per host for now
        PoolUpsert(
            runtime="nanoclaw",
            image="h",
            max_replicas=1,
            agents_per_instance=2,
            nanoclaw=NanoclawOptions(instances_root="/srv/i", **ok),
        )
    with pytest.raises(ValidationError):  # needs its options
        PoolUpsert(runtime="nanoclaw", image="h", max_replicas=1)


async def test_nanoclaw_runtime_spec(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    from argus_core.fleet import NanoclawOptions

    async with db.write() as tx:
        await fleet.upsert_pool(
            tx,
            "web",
            PoolUpsert(
                runtime="nanoclaw",
                image="argus-nanoclaw-host:dev",
                desired_replicas=1,
                max_replicas=2,
                nanoclaw=NanoclawOptions(
                    install_dir="/opt/nanoclaw",
                    instances_root="/srv/nc-i",
                    agent_image="nanoclaw-agent:1",
                    user="1000:1000",
                    group_add=["999"],
                    repositories={"argus": "https://git.example.test/team/argus"},
                ),
            ),
            "admin:test",
        )
    await tick(db, registry)
    [spec] = target.specs.values()
    # Identical host and container paths for everything NanoClaw hands to Docker;
    # only this instance's own directory, created for the host's user.
    workdir = f"/srv/nc-i/{spec.instance_id}"
    assert [(m.host, m.container, m.mode) for m in spec.mounts] == [
        ("/opt/nanoclaw", "/opt/nanoclaw", "ro"),
        (workdir, workdir, "rw"),
        ("/var/run/docker.sock", "/var/run/docker.sock", "rw"),
    ]
    assert spec.mounts[1].create_owner == "1000:1000"
    assert spec.cap_drop == ["ALL"] and spec.read_only is True
    assert "no-new-privileges:true" in spec.security_opt
    assert spec.pids_limit == 4096
    assert spec.env["CONTAINER_IMAGE"] == "nanoclaw-agent:1"
    # Selection is global by account priority: no provider list reaches the worker.
    assert "ARGUS_ACCOUNT_PROVIDERS" not in spec.env
    assert "ARGUS_EXECUTOR_PROVIDERS" not in spec.env
    assert (
        spec.env["ARGUS_REPOSITORIES_JSON"]
        == '{"argus": "https://git.example.test/team/argus"}'
    )
    assert spec.env["ARGUS_NANOCLAW_INSTANCES_ROOT"] == "/srv/nc-i"
    assert spec.env["ARGUS_BOOTSTRAP_TOKENS"].startswith("argus_boot_")
    assert (spec.user, spec.group_add) == ("1000:1000", ["999"])


def test_retired_provider_lists_are_dropped_not_enforced() -> None:
    from argus_core.fleet import NanoclawOptions

    ok = {"install_dir": "/opt/nc", "instances_root": "/srv/i", "agent_image": "a:1"}
    opts = NanoclawOptions(
        account_providers=["openai"], executor_providers=["cursor"], **ok
    )  # type: ignore[call-arg]
    dumped = opts.model_dump()
    assert "account_providers" not in dumped
    assert "executor_providers" not in dumped
    # Even values the old validation rejected no longer matter: nothing filters.
    NanoclawOptions(account_providers=["cursor"], **ok)  # type: ignore[call-arg]


async def test_a_persisted_legacy_pool_cannot_keep_filtering_accounts(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    await _nanoclaw_pool(db)
    async with db.write() as tx:
        async with tx.conn.execute(
            "SELECT config FROM pools WHERE name = 'web'"
        ) as cur:
            config = json.loads((await cur.fetchone())["config"])
        config["nanoclaw"] |= {
            "account_providers": ["openai"],
            "executor_providers": ["cursor", "anthropic"],
        }
        await tx.conn.execute(
            "UPDATE pools SET config = ? WHERE name = 'web'", (json.dumps(config),)
        )
    await tick(db, registry)
    [spec] = target.specs.values()
    assert "ARGUS_ACCOUNT_PROVIDERS" not in spec.env
    assert "ARGUS_EXECUTOR_PROVIDERS" not in spec.env
    async with db.read() as conn:
        pool = await fleet.get_pool(conn, "web")
    assert pool.nanoclaw is not None
    assert "account_providers" not in pool.nanoclaw.model_dump()


async def test_nanoclaw_instance_gets_the_effective_model_policy(
    db: Database,
    registry: TargetRegistry,
    target: FakeTarget,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARGUS_MODEL_POLICY", raising=False)
    path = tmp_path / "model-policy.json"
    policy = {
        "implementation": {"codex": ["b-2", "b-1"]},
        "harness": {"claude": ["a-1"]},
    }
    path.write_text(json.dumps(policy, indent=2))
    settings = replace(SETTINGS, model_policy_path=path)
    await _nanoclaw_pool(db)
    await tick(db, registry, settings)
    [spec] = target.specs.values()
    env = spec.env["ARGUS_MODEL_POLICY"]
    assert env == (
        '{"harness":{"claude":["a-1"]},"implementation":{"codex":["b-2","b-1"]}}'
    )
    # The same policy and revision GET /api/models reports.
    served = model_policy._read(path)
    assert model_policy.revision(json.loads(env)) == served.revision
    assert (env, served.revision) == model_policy.effective_env(path)


async def test_invalid_model_policy_fails_the_start_with_a_named_error(
    db: Database,
    registry: TargetRegistry,
    target: FakeTarget,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARGUS_MODEL_POLICY", raising=False)
    path = tmp_path / "model-policy.json"
    path.write_text('{"harness":{"codex":[]},"implementation":{}}')
    await _nanoclaw_pool(db)
    await tick(db, registry, replace(SETTINGS, model_policy_path=path))
    assert target.specs == {}
    [inst] = await instances(db)
    assert inst.last_error is not None
    assert "model policy is invalid" in inst.last_error
    assert "harness.codex" in inst.last_error


async def test_plain_pool_gets_no_model_policy_env(
    db: Database, registry: TargetRegistry, target: FakeTarget, tmp_path: Path
) -> None:
    await make_pool(db, 1)
    settings = replace(SETTINGS, model_policy_path=tmp_path / "model-policy.json")
    await tick(db, registry, settings)
    [spec] = target.specs.values()
    assert "ARGUS_MODEL_POLICY" not in spec.env


async def _nanoclaw_pool(db: Database) -> None:
    from argus_core.fleet import NanoclawOptions

    async with db.write() as tx:
        await fleet.upsert_pool(
            tx,
            "web",
            PoolUpsert(
                runtime="nanoclaw",
                image="host:dev",
                desired_replicas=1,
                max_replicas=2,
                nanoclaw=NanoclawOptions(
                    install_dir="/opt/nc", instances_root="/srv/i", agent_image="a:1"
                ),
            ),
            "admin:test",
        )


def _child(target: FakeTarget, name: str, instance_id: str) -> None:
    # What NanoClaw labels its agent containers with: argus-<pool>-<instance8>.
    target.children[name] = {
        "label": "nanoclaw-group-folder",
        "value": f"argus-web-{instance_id[:8]}",
    }


async def test_descendants_must_be_gone_before_an_instance_is_confirmed(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    await _nanoclaw_pool(db)
    await tick(db, registry)
    [inst] = await instances(db)
    _child(target, "ncl-agent-1", inst.id)

    async with db.write() as tx:
        await fleet.request_termination(tx, inst.id, "test")
    target.fail_stop = True  # nothing can be stopped yet
    await tick(db, registry)
    target.containers.pop(inst.provider_ref)  # the host dies on its own
    await tick(db, registry)
    [still] = await instances(db)
    assert still.id == inst.id and still.state == "terminating"  # agent still alive
    assert "ncl-agent-1" in target.children

    target.fail_stop = False
    await tick(db, registry)  # stops the child
    await tick(db, registry)  # now nothing of it remains: confirmed
    assert [i.state for i in await instances(db, True) if i.id == inst.id] == [
        "terminated"
    ]
    assert target.children == {}


async def test_host_exit_with_live_agents_stops_them_before_confirming(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    await _nanoclaw_pool(db)
    await tick(db, registry)
    await tick(db, registry)  # confirmed running
    [inst] = await instances(db)
    _child(target, "ncl-agent-1", inst.id)
    target.exit(inst.provider_ref, code=1)
    await tick(db, registry)
    assert "ncl-agent-1" in target.stopped  # an unexpected host exit does not leak it
    await tick(db, registry)
    assert [i.state for i in await instances(db, True) if i.id == inst.id] == [
        "terminated"
    ]


async def test_orphaned_agents_of_unknown_instances_are_stopped(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    await _nanoclaw_pool(db)
    await tick(db, registry)
    _child(target, "ncl-orphan", "deadbeef-0000-0000-0000-000000000000")
    _child(target, "ncl-other-pool", "x")  # not ours: wrong shape, left alone
    target.children["ncl-other-pool"]["value"] = "argus-someone-else-12345678"
    await tick(db, registry)
    assert "ncl-orphan" in target.stopped
    assert "ncl-other-pool" in target.children


async def test_exits_after_a_drain_are_not_crashes(
    db: Database, registry: TargetRegistry, target: FakeTarget, clock: FakeClock
) -> None:
    """A drained host may exit non-zero (SIGTERM) soon after starting."""
    await make_pool(db, desired=1)
    for _ in range(4):
        await tick(db, registry)
        [inst] = await instances(db)
        async with db.write() as tx:
            await tx.conn.execute(
                "UPDATE instances SET draining = 1 WHERE id = ?", (inst.id,)
            )
        clock.advance(2)
        target.exit(inst.provider_ref, code=143)
        await tick(db, registry)
    # Four drained exits, each replaced at once: five starts, never held (three
    # real crashes would have held the fourth).
    assert len(target.started) == 5
    async with db.read() as conn:
        assert (await fleet.get_pool(conn, "web")).status == "ok"


def _sandboxed_pool(**nanoclaw: object) -> PoolUpsert:
    from argus_core.fleet import DockerOptions, NanoclawOptions

    return PoolUpsert(
        runtime="nanoclaw",
        image="argus-nanoclaw-host:dev",
        max_replicas=2,
        docker=DockerOptions(runtime="sysbox-runc"),
        nanoclaw=NanoclawOptions(
            install_dir="/opt/nanoclaw",
            instances_root="/srv/nc-i",
            agent_image="nanoclaw-agent:1",
            **nanoclaw,  # type: ignore[arg-type]
        ),
    )


def test_sandboxed_nanoclaw_has_no_docker_socket() -> None:
    from datetime import UTC, datetime

    from argus_api.fleet.runtimes import container_spec
    from argus_api.fleet.targets import SANDBOX_TMPFS
    from argus_core.fleet import Pool

    now = datetime.now(UTC)
    upsert = _sandboxed_pool(agent_image_archive="/srv/agent.tar")
    pool = Pool(
        name="sbx",
        status="ok",
        last_error=None,
        live_instances=0,
        created_at=now,
        updated_at=now,
        version="v1",
        **upsert.model_dump(),
    )
    spec = container_spec(pool, "i-1", ["argus_boot_x"], "http://argus")
    assert spec.runtime == "sysbox-runc"
    assert all("docker.sock" not in m.container for m in spec.mounts)
    assert ("/srv/agent.tar", "/srv/agent.tar", "ro") in [
        (m.host, m.container, m.mode) for m in spec.mounts
    ]
    assert spec.env["ARGUS_NANOCLAW_INNER_DOCKER"] == "1"
    assert spec.user is None and spec.group_add == []
    assert spec.cap_drop == ["ALL"]
    assert "no-new-privileges:true" in spec.security_opt
    assert spec.read_only is True
    assert spec.pids_limit == 4096
    assert spec.tmpfs == SANDBOX_TMPFS


def test_sandboxed_nanoclaw_runs_as_root_in_its_sandbox() -> None:
    with pytest.raises(ValueError, match=r"docker\.runtime"):
        _sandboxed_pool(user="1000:1000")


def test_sandboxed_nanoclaw_rejects_docker_socket() -> None:
    with pytest.raises(ValueError, match=r"docker\.runtime.*docker_socket"):
        _sandboxed_pool(docker_socket="/var/run/docker.sock")


async def test_nanoclaw_runtime_spec_is_hardened(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    from argus_api.fleet.targets import RUNTIME_TMPFS
    from argus_core.fleet import NanoclawOptions

    async with db.write() as tx:
        await fleet.upsert_pool(
            tx,
            "web",
            PoolUpsert(
                runtime="nanoclaw",
                image="argus-nanoclaw-host:dev",
                desired_replicas=1,
                max_replicas=2,
                nanoclaw=NanoclawOptions(
                    install_dir="/opt/nanoclaw",
                    instances_root="/srv/nc-i",
                    agent_image="nanoclaw-agent:1",
                    user="1000:1000",
                ),
            ),
            "admin:test",
        )
    await tick(db, registry)
    [spec] = target.specs.values()
    assert spec.cap_drop == ["ALL"]
    assert spec.security_opt == ["no-new-privileges:true"]
    assert spec.read_only is True
    assert spec.pids_limit == 4096
    assert spec.tmpfs == RUNTIME_TMPFS


# -- termination notifications (workspace-cleanup.spec.md 3.2) --------------------------


async def test_confirmed_termination_notifies_after_commit(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    await make_pool(db, desired=1)
    await tick(db, registry)
    [inst] = await instances(db)
    seen: list[str] = []

    def on_terminated(instance_id: str) -> None:
        assert db.write_held_for() == 0  # never inside the write transaction
        assert inst.provider_ref not in target.containers  # exited one removed
        seen.append(instance_id)

    await reconcile_pool(db, "web", registry, SETTINGS, on_terminated)
    assert seen == []  # running
    target.exit(inst.provider_ref, code=1)
    await reconcile_pool(db, "web", registry, SETTINGS, on_terminated)
    assert seen == [inst.id]
    [old] = [i for i in await instances(db, True) if i.id == inst.id]
    assert old.state == "terminated"


async def test_rolled_back_pass_does_not_notify(
    db: Database,
    registry: TargetRegistry,
    target: FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argus_api.fleet import reconciler

    await make_pool(db, desired=1)
    await tick(db, registry)
    [inst] = await instances(db)
    target.exit(inst.provider_ref, code=1)

    async def boom(*args: object) -> list:
        raise RuntimeError("after confirm_terminated, before commit")

    monkeypatch.setattr(reconciler, "_scale", boom)
    seen: list[str] = []
    with pytest.raises(RuntimeError):
        await reconcile_pool(db, "web", registry, SETTINGS, seen.append)
    assert seen == []
    assert [i.state for i in await instances(db, True) if i.id == inst.id] == [
        "running"
    ]


async def test_terminating_does_not_notify_until_confirmed(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    await make_pool(db, desired=1)
    await tick(db, registry)
    [inst] = await instances(db)
    async with db.write() as tx:
        await fleet.request_termination(tx, inst.id, "test")
    target.fail_stop = True
    seen: list[str] = []
    await reconcile_pool(db, "web", registry, SETTINGS, seen.append)
    await reconcile_pool(db, "web", registry, SETTINGS, seen.append)
    [cur] = [i for i in await instances(db, True) if i.id == inst.id]
    assert cur.state == "terminating"
    assert seen == []  # the container may still exist
    target.fail_stop = False
    await reconcile_pool(db, "web", registry, SETTINGS, seen.append)  # stops it
    await reconcile_pool(db, "web", registry, SETTINGS, seen.append)  # confirms
    assert seen == [inst.id]
