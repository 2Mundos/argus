"""Project runtime images: declaration, preparation, claim gate, build reuse and
invalidation, durable failures that spend no attempt, and the used-image check.
The Docker daemon is a fake; the real build and probes are in
test_runtime_docker.py (opt-in)."""

import asyncio
import hashlib
from pathlib import Path

import pytest
from api_support import FakeClock, insert_agent
from argus_api.config import Settings
from argus_api.db import Database
from argus_api.errors import ConflictError, ValidationFailedError
from argus_api.runtime import service
from argus_api.runtime.docker import (
    KEY_LABEL,
    BuildResult,
    Daemon,
    DockerError,
    ProbeResult,
)
from argus_api.runtime.service import RuntimeManager
from argus_api.tasks import service as tasks
from argus_core.runtime import (
    ComponentRef,
    PrepareRequest,
    RuntimeSpec,
    ServiceRef,
    UsedRequest,
)
from argus_core.tasks import TaskCreate

BASE_REF = "nanoclaw-agent:test"
BASE_ID = "sha256:" + "1" * 64
A, B = "agent-a", "agent-b"

OUTPUT = {
    "git": "git version 2.39.5", "ssh": "OpenSSH_9.2p1", "python": "Python 3.13.1",
    "node": "v22.11.0", "chromium": "/usr/bin/chromium", "pango": "pango ok",
    "graft": "0.21.1", "psql": "psql (PostgreSQL) 16.15", "postgres-server": "postgres (PostgreSQL) 16.15",
    "valkey-server": "Valkey server v=8.0.11 sha=0", "valkey-cli": "valkey-cli 8.0.11",
    "test-services": '{"probe": "postgres.cleanup", "ok": true, "detail": "ok"}',
}  # fmt: skip


class FakeDocker:
    def __init__(self) -> None:
        self.daemon_id = "daemon-1"
        self.platform = "linux/amd64"
        self.images: dict[str, str] = {BASE_REF: BASE_ID}
        self.labels: dict[str, dict[str, str]] = {}
        self.builds: list[dict] = []
        self.probed: list[tuple[str, str, str]] = []
        self.fail_build = False
        self.failing_probes: set[str] = set()
        self.unreachable = False
        self.gate: asyncio.Event | None = None

    def _check(self) -> None:
        if self.unreachable:
            raise DockerError("docker is not available")

    async def daemon(self) -> Daemon:
        self._check()
        return Daemon(self.daemon_id, self.platform)

    async def image_id(self, ref: str) -> str | None:
        self._check()
        return self.images.get(ref)

    async def image_label(self, ref: str, label: str) -> str | None:
        return self.labels.get(ref, {}).get(label)

    async def tag(self, image_id: str, ref: str) -> None:
        self.images[ref] = image_id

    async def build(
        self, *, dockerfile, files, base, ref, key, timeout_s
    ) -> BuildResult:
        self._check()
        if self.gate is not None:
            await self.gate.wait()
        self.builds.append(
            {
                "dockerfile": dockerfile,
                "files": sorted(files),
                "base": base,
                "ref": ref,
                "key": key,
            }
        )
        if self.fail_build:
            return BuildResult(False, "E: Unable to locate package postgresql-16")
        self.images[ref] = (
            "sha256:" + hashlib.sha256((dockerfile + base + ref).encode()).hexdigest()
        )
        self.labels[ref] = {KEY_LABEL: key}
        return BuildResult(True, "")

    async def probe(self, ref, probe, run_as) -> ProbeResult:
        self.probed.append((ref, probe.name, run_as))
        if probe.name in self.failing_probes:
            return ProbeResult(False, f"{probe.name}: not found")
        if probe.name == "python":
            minor = "3.12" if "python find 3.12" in probe.argv[-1] else "3.13"
            return ProbeResult(True, f"Python {minor}.1")
        return ProbeResult(True, OUTPUT[probe.name])

    async def remove_owned_containers(self) -> int:
        return 0


RADAR = RuntimeSpec(
    components=[
        ComponentRef(id=i)
        for i in ("python", "psql", "postgres-server", "valkey", "git")
    ]
    + [ComponentRef(id="minio", required=False)],
    services=[ServiceRef(kind="postgres"), ServiceRef(kind="valkey")],
)
ARGUS = RuntimeSpec(components=[ComponentRef(id="python", version="3.12")])


@pytest.fixture
def docker() -> FakeDocker:
    return FakeDocker()


@pytest.fixture
def manager(db: Database, docker: FakeDocker, tmp_path: Path) -> RuntimeManager:
    return RuntimeManager(
        db, docker, Settings(data_dir=tmp_path, nanoclaw_agent_image=BASE_REF)
    )


@pytest.fixture(autouse=True)
async def world(db: Database) -> None:
    await insert_agent(db, A)
    await insert_agent(db, B)
    async with db.write() as tx:
        for slug in ("radar", "argus"):
            await tx.conn.execute(
                "INSERT INTO projects (slug, name, description, created_at, updated_at) VALUES (?, ?, '', ?, ?)",
                (slug, slug, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
        await service.put_spec(tx, "radar", RADAR, "human:test")
        await service.put_spec(tx, "argus", ARGUS, "human:test")


def req(task, **kw) -> PrepareRequest:
    base = {
        "task_id": task.id,
        "attempt": task.attempt,
        "host_id": "daemon-1",
        "run_as": "1000:1000",
        "base_ref": BASE_REF,
        "base_id": BASE_ID,
        "platform": "linux/amd64",
    }
    return PrepareRequest(**{**base, **kw})


async def create(db: Database, project: str, **kw):
    async with db.write() as tx:
        return await tasks.create_task(
            tx, TaskCreate(project=project, title=f"t-{project}", **kw), "human:test"
        )


async def claim(db: Database, agent: str = A):
    async with db.write() as tx:
        return await tasks.claim(tx, agent, [])


async def task_row(db: Database, task_id: str):
    async with (
        db.read() as conn,
        conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur,
    ):
        return await cur.fetchone()


async def builds(db: Database, project: str | None = None) -> list:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT * FROM runtime_builds WHERE ? IS NULL OR project = ? ORDER BY created_at, project",
            (project, project),
        ) as cur,
    ):
        return await cur.fetchall()


async def ready_for(db, manager, project: str = "radar", agent: str = A, **kw):
    """Claim a task of `project`, prepare it (held), build, claim again, prepare (ready)."""
    await create(db, project)
    first = await claim(db, agent)
    held = await manager.prepare(agent, req(first, **kw))
    assert held.task_held and held.state == "preparing"
    assert await manager.work_once()
    second = await claim(db, agent)
    assert second is not None and second.id == first.id
    return second, await manager.prepare(agent, req(second, **kw))


# -- declaration -----------------------------------------------------------------------


async def test_declaration_is_refused_with_every_problem(db: Database) -> None:
    bad = RuntimeSpec(
        components=[
            ComponentRef(id="bogus"),
            ComponentRef(id="psql", version="9"),
            ComponentRef(id="minio"),
        ],
        services=[ServiceRef(kind="valkey")],
    )
    with pytest.raises(ValidationFailedError) as err:
        async with db.write() as tx:
            await service.put_spec(tx, "radar", bad, "human:test")
    assert err.value.code == "runtime_invalid"
    text = " ".join(err.value.details)
    assert "unknown component 'bogus'" in text and "version '9'" in text
    assert "'minio' is required" in text and "needs component 'valkey'" in text
    async with db.read() as conn:
        assert (await service.view(conn, "radar")).spec == RADAR  # unchanged


async def test_view_shows_requirement_gaps_and_services(db: Database) -> None:
    async with db.read() as conn:
        v = await service.view(conn, "radar")
    assert {c.id for c in v.requirement} >= {"psql", "valkey", "postgres-server"}
    assert [g.component for g in v.gaps] == ["minio"]
    assert [s.kind for s in v.services] == ["postgres", "valkey"]
    assert v.declared and v.builds == []


# -- preparation ----------------------------------------------------------------------------


async def test_first_claim_is_held_without_spending_an_attempt(
    db, manager, docker
) -> None:
    await create(db, "radar", max_attempts=1)
    t = await claim(db)
    sel = await manager.prepare(A, req(t))
    assert sel.state == "preparing" and sel.task_held and sel.image is None
    row = await task_row(db, t.id)
    assert row["state"] == "blocked" and row["block_reason"] == "runtime_preparing"
    assert row["failures"] == 0 and row["max_attempts"] == 1
    assert docker.builds == []  # preparation never builds inside the request


async def test_build_verifies_with_probes_then_task_is_released_and_ready(
    db, manager, docker
) -> None:
    task, sel = await ready_for(db, manager)
    assert sel.state == "ready" and not sel.task_held
    assert (
        sel.image.ref.startswith("argus-project-runtime:radar-")
        and sel.image.id == docker.images[sel.image.ref]
    )
    assert sel.image.base_id == BASE_ID
    assert {t.name for t in sel.tools} >= {
        "psql",
        "valkey-server",
        "valkey-cli",
        "postgres-server",
        "test-services",
    }
    assert all(t.ok for t in sel.tools)
    assert [s.kind for s in sel.services] == ["postgres", "valkey"]
    assert [g.component for g in sel.gaps] == ["minio"]  # reported, never faked green
    assert {p[2] for p in docker.probed} == {"1000:1000"}  # the task's uid
    assert len(docker.builds) == 1 and docker.builds[0]["files"] == [
        "argus-test-services"
    ]
    assert docker.builds[0]["base"].startswith("argus-project-runtime-base:")
    row = await task_row(db, task.id)
    assert row["state"] == "claimed" and row["failures"] == 0
    async with db.read() as conn:
        selected = await service.task_runtime(conn, task.id)
    assert (
        selected[0]["status"] == "selected" and selected[0]["image_id"] == sel.image.id
    )


async def test_failed_probe_is_never_ready(db, manager, docker) -> None:
    docker.failing_probes = {"valkey-server"}
    await create(db, "radar")
    t = await claim(db)
    await manager.prepare(A, req(t))
    await manager.work_once()
    (b,) = await builds(db, "radar")
    assert b["state"] == "failed" and "valkey-server" in b["reason"]
    assert b["image_id"] is not None  # built, but not verified


async def test_missing_probes_cannot_produce_ready(
    db, manager, docker, monkeypatch
) -> None:
    import argus_api.runtime.service as svc

    real = svc.probes
    monkeypatch.setattr(
        svc, "probes", lambda r: real(r)[:2]
    )  # a probe set that skips the services
    await create(db, "radar")
    t = await claim(db)
    await manager.prepare(A, req(t))
    await manager.work_once()
    (b,) = await builds(db, "radar")
    assert b["state"] == "failed" and "not run" in b["reason"]


async def test_two_projects_in_one_pool_get_their_own_image(
    db, manager, docker
) -> None:
    _, radar = await ready_for(db, manager, "radar", A)
    _, argus = await ready_for(db, manager, "argus", B)
    assert radar.image.ref != argus.image.ref
    assert "radar" in radar.image.ref and "argus" in argus.image.ref
    assert radar.image.id != argus.image.id
    assert {c.id for c in radar.requirement} != {c.id for c in argus.requirement}
    assert (
        radar.image.base_id == argus.image.base_id == BASE_ID
    )  # one pool, one base, two images
    assert not any(
        t.name == "psql" for t in argus.tools
    )  # argus did not get radar's toolset


async def test_same_project_tasks_share_one_build(db, manager, docker) -> None:
    await create(db, "radar")
    await create(db, "radar")
    t1, t2 = await claim(db, A), await claim(db, B)
    sels = await asyncio.gather(
        manager.prepare(A, req(t1)), manager.prepare(B, req(t2))
    )
    assert all(s.state == "preparing" for s in sels)
    assert len(await builds(db, "radar")) == 1  # deduplicated before any work
    while await manager.work_once():
        pass
    assert len(docker.builds) == 1
    # a later attempt on the same recipe reuses the image: no new build, no new probe run
    probes_before = len(docker.probed)
    t1, t2 = await claim(db, A), await claim(db, B)
    assert (await manager.prepare(A, req(t1))).state == "ready"
    assert (await manager.prepare(B, req(t2))).state == "ready"
    assert len(docker.builds) == 1 and len(docker.probed) == probes_before


async def test_concurrent_builders_do_not_build_twice(db, manager, docker) -> None:
    await create(db, "radar")
    t = await claim(db)
    await manager.prepare(A, req(t))
    docker.gate = asyncio.Event()
    first = asyncio.create_task(manager.work_once())
    await asyncio.sleep(0.05)
    assert (
        await manager.work_once() is False
    )  # the row is `building`, nothing else is queued
    docker.gate.set()
    await first
    assert len(docker.builds) == 1


async def test_build_key_follows_the_base_and_the_recipe_not_the_noise(
    db, manager, docker
) -> None:
    await ready_for(db, manager)
    assert len(docker.builds) == 1
    # irrelevant: the service env mapping and an updated-but-equal declaration
    async with db.write() as tx:
        noisy = replace_services(RADAR, {"RADAR_TEST_DATABASE_URL": "{url_asyncpg}"})
        await service.put_spec(tx, "radar", noisy, "human:test")
    t = await claim_new(db)
    assert (await manager.prepare(A, req(t))).state == "ready"
    assert len(docker.builds) == 1
    # relevant: a new component means a new image
    async with db.write() as tx:
        await service.put_spec(
            tx,
            "radar",
            noisy.model_copy(
                update={"components": [*noisy.components, ComponentRef(id="pango")]}
            ),
            "human:test",
        )
    t = await claim_new(db)
    assert (await manager.prepare(A, req(t))).state == "preparing"
    await manager.work_once()
    assert len(docker.builds) == 2
    # relevant: the base image moved (new id for the same ref)
    new_base = "sha256:" + "2" * 64
    docker.images[BASE_REF] = new_base
    t = await claim_new(db)
    assert (await manager.prepare(A, req(t, base_id=new_base))).state == "preparing"
    await manager.work_once()
    assert len(docker.builds) == 3
    assert len({b["key"] for b in docker.builds}) == 3


def replace_services(spec: RuntimeSpec, env: dict[str, str]) -> RuntimeSpec:
    return spec.model_copy(
        update={
            "services": [
                ServiceRef(kind="postgres", env=env),
                ServiceRef(kind="valkey"),
            ]
        }
    )


async def claim_new(db: Database):
    await create(db, "radar")
    t = await claim(db)
    assert t is not None
    return t


# -- failures are durable, visible and free ---------------------------------------------------


async def test_failed_build_is_durable_and_spends_no_attempt(
    db, manager, docker
) -> None:
    docker.fail_build = True
    await create(db, "radar", max_attempts=1)
    t = await claim(db)
    await manager.prepare(A, req(t))
    await manager.work_once()
    row = await task_row(db, t.id)
    assert row["state"] == "blocked" and row["block_reason"] == "runtime_failed"
    assert "Unable to locate package" in row["error"]
    assert row["failures"] == 0 and row["state"] != "failed"
    # no storm: the next ready task of the project is blocked at claim, not handed out
    await create(db, "radar")
    assert await claim(db, A) is None
    async with (
        db.read() as conn,
        conn.execute("SELECT count(*) c FROM events WHERE kind = 'task.claim'") as cur,
    ):
        assert (await cur.fetchone())["c"] == 1
    other = await create(db, "argus")  # an unrelated project is still served
    assert (await claim(db, A)).id == other.id


async def test_rebuild_after_fix_returns_tasks_and_succeeds(
    db, manager, docker
) -> None:
    docker.fail_build = True
    await create(db, "radar")
    t = await claim(db)
    await manager.prepare(A, req(t))
    await manager.work_once()
    docker.fail_build = False
    async with db.write() as tx:
        assert await service.rebuild(tx, "radar", "human:test") == 1
    assert (await task_row(db, t.id))["state"] == "ready"
    await manager.work_once()
    t2 = await claim(db)
    assert (await manager.prepare(A, req(t2))).state == "ready"


async def test_changed_declaration_wakes_failed_holds(db, manager, docker) -> None:
    docker.fail_build = True
    await create(db, "radar")
    t = await claim(db)
    await manager.prepare(A, req(t))
    await manager.work_once()
    async with db.write() as tx:
        await service.put_spec(
            tx, "radar", RuntimeSpec(components=[ComponentRef(id="git")]), "human:test"
        )
    assert (await task_row(db, t.id))["state"] == "ready"


async def test_daemon_mismatch_is_an_actionable_failure(db, manager, docker) -> None:
    await create(db, "radar")
    t = await claim(db)
    sel = await manager.prepare(A, req(t, host_id="other-daemon"))
    assert sel.state == "failed" and sel.task_held
    assert "not the one Argus builds on" in sel.reason
    assert docker.builds == []


async def test_base_image_must_be_a_pool_agent_image(db, manager) -> None:
    await create(db, "radar")
    t = await claim(db)
    with pytest.raises(ValidationFailedError) as err:
        await manager.prepare(A, req(t, base_ref="postgres:16"))
    assert err.value.code == "runtime_base_not_allowed"
    assert (await task_row(db, t.id))["state"] == "claimed"


async def test_base_id_the_worker_reports_must_match_argus(db, manager) -> None:
    await create(db, "radar")
    t = await claim(db)
    sel = await manager.prepare(A, req(t, base_id="sha256:" + "9" * 64))
    assert sel.state == "failed" and "on the worker" in sel.reason


async def test_docker_outage_waits_then_builds(db, manager, docker) -> None:
    docker.unreachable = True
    await create(db, "radar")
    t = await claim(db)
    sel = await manager.prepare(A, req(t))
    assert sel.state == "preparing"  # not failed: Argus could not look
    docker.unreachable = False
    await manager.work_once()
    assert (await builds(db, "radar"))[0]["state"] == "ready"


async def test_prepare_requires_the_claim(db, manager) -> None:
    await create(db, "radar")
    t = await claim(db)
    with pytest.raises(ConflictError):
        await manager.prepare(B, req(t))  # someone else's task
    with pytest.raises(ConflictError):
        await manager.prepare(A, req(t, attempt=t.attempt + 1))


async def test_disabled_runtime_says_so(db, docker, tmp_path) -> None:
    off = RuntimeManager(
        db,
        docker,
        Settings(
            data_dir=tmp_path, nanoclaw_agent_image=BASE_REF, project_runtime=False
        ),
    )
    await create(db, "radar")
    t = await claim(db)
    sel = await off.prepare(A, req(t))
    assert sel.state == "disabled" and "off" in sel.reason and not sel.task_held


# -- restart -------------------------------------------------------------------------------------


async def test_interrupted_build_is_retried_then_failed(
    db, manager, docker, clock: FakeClock
) -> None:
    await create(db, "radar")
    t = await claim(db)
    await manager.prepare(A, req(t))
    for attempt in range(1, 4):
        async with db.write() as tx:  # a worker took it and the process died
            await tx.conn.execute(
                "UPDATE runtime_builds SET state = 'building', attempts = ?, working_until = ?",
                (attempt, "2026-01-01T00:00:01+00:00"),
            )
        clock.advance(3600)
        async with db.write() as tx:
            await service.reap_interrupted(tx)
        state = (await builds(db, "radar"))[0]
        assert state["state"] == ("preparing" if attempt < 3 else "failed")
    assert "interrupted 3 times" in state["reason"]
    assert (await task_row(db, t.id))["block_reason"] == "runtime_failed"


async def test_a_ready_build_whose_image_vanished_is_rebuilt(
    db, manager, docker
) -> None:
    _, sel = await ready_for(db, manager)
    del docker.images[sel.image.ref]  # operator pruned it
    t = await claim_new(db)
    assert (await manager.prepare(A, req(t))).state == "preparing"
    await manager.work_once()
    assert len(docker.builds) == 2


async def test_existing_image_with_the_same_key_is_reused_after_a_restart(
    db, manager, docker
) -> None:
    await ready_for(db, manager)
    async with db.write() as tx:  # Argus lost its rows (not its images)
        await tx.conn.execute("DELETE FROM runtime_builds")
    t = await claim_new(db)
    await manager.prepare(A, req(t))
    await manager.work_once()
    assert len(docker.builds) == 1  # labelled image found, probes re-run, no rebuild
    assert (await builds(db, "radar"))[0]["state"] == "ready"


# -- what ran ----------------------------------------------------------------------------------------


async def test_used_image_is_verified_against_the_selected_one(db, manager) -> None:
    t, sel = await ready_for(db, manager)
    res = await manager.used(
        A,
        UsedRequest(
            task_id=t.id, attempt=t.attempt, container="c1", image_id=sel.image.id
        ),
    )
    assert res.verified
    async with db.read() as conn:
        (row,) = await service.task_runtime(conn, t.id)
    assert row["status"] == "verified" and row["used_image_id"] == sel.image.id


async def test_session_that_ran_another_image_holds_the_task(db, manager) -> None:
    t, sel = await ready_for(db, manager)
    res = await manager.used(
        A,
        UsedRequest(task_id=t.id, attempt=t.attempt, container="c1", image_id=BASE_ID),
    )
    assert not res.verified
    row = await task_row(db, t.id)
    assert row["state"] == "blocked" and row["block_reason"] == "runtime_failed"
    assert row["failures"] == 0 and sel.image.id in row["error"]


async def test_used_needs_a_selection(db, manager) -> None:
    await create(db, "radar")
    t = await claim(db)
    with pytest.raises(ConflictError) as err:
        await manager.used(
            A,
            UsedRequest(
                task_id=t.id, attempt=t.attempt, container="c", image_id=BASE_ID
            ),
        )
    assert err.value.code == "runtime_not_selected"


async def test_gate_ignores_agents_that_never_prepared(db) -> None:
    await create(db, "radar")
    assert (
        await claim(db, A) is not None
    )  # no runtime_hosts row: the first prepare decides


async def test_gate_leaves_building_tasks_ready_for_others(db, manager, docker) -> None:
    await create(db, "radar")
    first = await claim(db, A)
    await manager.prepare(A, req(first))
    t = await create(db, "radar")
    assert await claim(db, A) is None  # this host's build is preparing: not handed out
    assert (await task_row(db, t.id))["state"] == "ready"  # and not blocked either
