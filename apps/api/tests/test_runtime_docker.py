"""Real Docker: Argus derives the Radar image from an agent image, probes it as the
task uid with the task hardening, and PostgreSQL and Valkey really run per task.

Opt-in: ARGUS_DOCKER_TESTS=1 and ARGUS_RUNTIME_TEST_BASE=<an agent image present on
the local daemon>. Everything it creates carries a random exclusive repository name
and is removed at the end; no existing image, tag, pool or container is touched.
"""

import json
import os
import secrets
import subprocess
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from api_support import insert_agent
from argus_api.config import Settings
from argus_api.db import Database
from argus_api.runtime import service
from argus_api.runtime.docker import CliDocker
from argus_api.runtime.service import RuntimeManager
from argus_api.tasks import service as tasks
from argus_core.runtime import (
    ComponentRef,
    PrepareRequest,
    RuntimeSpec,
    ServiceRef,
    UsedRequest,
    declaration,
)
from argus_core.tasks import TaskCreate

BASE = os.environ.get("ARGUS_RUNTIME_TEST_BASE", "")
pytestmark = [
    pytest.mark.skipif(
        os.environ.get("ARGUS_DOCKER_TESTS") != "1", reason="set ARGUS_DOCKER_TESTS=1"
    ),
    pytest.mark.skipif(
        not BASE, reason="set ARGUS_RUNTIME_TEST_BASE to a local agent image"
    ),
]

RUN_AS = "1000:1000"
HARDENING = ["--user", RUN_AS, "--cap-drop=ALL", "--security-opt", "no-new-privileges",
             "--read-only", "--tmpfs", "/tmp:rw,exec,nosuid,size=512m", "-e", "HOME=/home/node"]  # fmt: skip
RADAR = RuntimeSpec(
    components=[ComponentRef(id=i) for i in
                ("python", "pango", "graft", "psql", "postgres-server", "valkey", "node", "git", "chromium")]
    + [ComponentRef(id=i, required=False) for i in ("pnpm", "sops-age", "minio", "elasticsearch")],
    services=[
        ServiceRef(kind="postgres", env={"RADAR_TEST_DATABASE_URL": "{url_asyncpg}", "RADAR_DATABASE_URL": "{url_asyncpg}"}),
        ServiceRef(kind="valkey", env={"RADAR_VALKEY_URL": "{url}"}),
    ],
    codex_turn_timeout_s=7200,
)  # fmt: skip


def docker(
    *args: str, check: bool = True, timeout: int = 300
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=check
    )


def sh(image: str, script: str, *extra: str) -> subprocess.CompletedProcess:
    return docker(
        "run",
        "--rm",
        *HARDENING,
        *extra,
        "--entrypoint",
        "sh",
        image,
        "-c",
        script,
        check=False,
    )


@pytest.fixture(scope="module")
def repo() -> str:
    return f"argus-runtime-test-{secrets.token_hex(4)}"


@pytest.fixture(scope="module")
def evidence_dir() -> Path | None:
    d = os.environ.get("ARGUS_RUNTIME_EVIDENCE_DIR")
    return Path(d) if d else None


@pytest.fixture
async def world(tmp_path: Path, repo: str) -> AsyncGenerator[dict]:
    db = await Database.open(tmp_path / "argus.db", readers=2)
    cli = CliDocker(owner=repo)
    manager = RuntimeManager(
        db,
        cli,
        Settings(data_dir=tmp_path, nanoclaw_agent_image=BASE, runtime_image_repo=repo),
    )
    for agent in ("agent-a", "agent-b"):
        await insert_agent(db, agent)
    async with db.write() as tx:
        for slug in ("radar", "argus"):
            await tx.conn.execute(
                "INSERT INTO projects (slug, name, description, created_at, updated_at) VALUES (?, ?, '', ?, ?)",
                (slug, slug, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
        await service.put_spec(tx, "radar", RADAR, "human:test")
        await service.put_spec(
            tx, "argus", RuntimeSpec(components=[ComponentRef(id="git")]), "human:test"
        )
    daemon = await cli.daemon()
    base_id = await cli.image_id(BASE)
    assert base_id, f"{BASE} is not on the local daemon"
    yield {
        "db": db,
        "manager": manager,
        "cli": cli,
        "daemon": daemon,
        "base_id": base_id,
        "repo": repo,
    }
    await db.close()


def prepare_request(w: dict, task) -> PrepareRequest:
    return PrepareRequest(
        task_id=task.id, attempt=task.attempt, host_id=w["daemon"].id, run_as=RUN_AS,
        base_ref=BASE, base_id=w["base_id"], platform=w["daemon"].platform,
    )  # fmt: skip


async def claim_task(w: dict, project: str, agent: str, priority: int = 0):
    async with w["db"].write() as tx:
        await tasks.create_task(
            tx,
            TaskCreate(project=project, title=f"{project} smoke", priority=priority),
            "human:test",
        )
    async with w["db"].write() as tx:
        return await tasks.claim(tx, agent, [])


async def test_radar_image_is_built_probed_and_tasks_run_postgres_and_valkey(
    world: dict, repo: str, evidence_dir: Path | None
) -> None:
    w = world
    try:
        # 1. preparation holds the task, the builder makes a real image
        first = await claim_task(w, "radar", "agent-a")
        held = await w["manager"].prepare("agent-a", prepare_request(w, first))
        assert held.state == "preparing" and held.task_held
        assert await w["manager"].work_once()
        again = await claim_task(
            w, "radar", "agent-a"
        )  # the released task, or a new one
        sel = await w["manager"].prepare("agent-a", prepare_request(w, again))
        assert sel.state == "ready", sel.reason
        ref, image_id = sel.image.ref, sel.image.id
        assert ref.startswith(f"{repo}:radar-")
        assert (
            docker("image", "inspect", "--format", "{{.Id}}", ref).stdout.strip()
            == image_id
        )
        by_name = {t.name: t for t in sel.tools}
        assert "(PostgreSQL) 16." in by_name["psql"].output
        assert "Valkey server v=8.0." in by_name["valkey-server"].output
        assert all(t.ok for t in sel.tools), [t for t in sel.tools if not t.ok]
        assert [g.component for g in sel.gaps] == [
            "elasticsearch",
            "minio",
            "pnpm",
            "sops-age",
        ]
        (build,) = await _builds(w["db"], "radar")
        assert build["state"] == "ready" and build["run_as"] == RUN_AS

        # 2. a second task of the project reuses the image: same id, no rebuild
        before = docker(
            "image", "inspect", "--format", "{{.Id}} {{.Created}}", ref
        ).stdout
        third = await claim_task(w, "radar", "agent-b")
        assert (
            await w["manager"].prepare("agent-b", prepare_request(w, third))
        ).state == "ready"
        assert (
            docker("image", "inspect", "--format", "{{.Id}} {{.Created}}", ref).stdout
            == before
        )

        # 3. the argus project gets its own, different image from the same base
        other = await claim_task(w, "argus", "agent-a", priority=5)
        held = await w["manager"].prepare("agent-a", prepare_request(w, other))
        assert held.task_held
        await w["manager"].work_once()
        async with w["db"].read() as conn:
            argus_view = await service.view(conn, "argus")
        argus_ref = argus_view.builds[0].image_ref
        assert argus_view.builds[0].state == "ready" and argus_ref != ref
        assert (
            sh(argus_ref, "command -v psql valkey-server").returncode != 0
        )  # radar's tools are not there

        # 4. real services, per task, in a task-hardened container; the URLs come from the product
        decl_a = json.dumps(declaration("task-a", sel.services))
        decl_b = json.dumps(declaration("task-b", sel.services))
        script = _services_script(decl_a, decl_b)
        done = sh(ref, script, "--network", "none")
        if evidence_dir:
            (evidence_dir / "services-run.log").write_text(done.stdout + done.stderr)
        assert done.returncode == 0, done.stdout + done.stderr
        for marker in (
            "A:pg-ok",
            "A:rollback-ok",
            "A:valkey-ok",
            "B:pg-ok",
            "B:valkey-ok",
            "ISOLATED",
            "CLEAN",
        ):
            assert marker in done.stdout, (marker, done.stdout)

        # 5. the image holds no source, credentials or secret-named variables
        audit = sh(
            ref,
            "env | grep -iE 'token|secret|passw|api_key|credential' || echo none; "
            "ls -d /home/node/.git-credentials /home/node/.config/gh /home/node/.netrc /home/node/.env 2>&1 | grep -v 'No such' || echo none-files; "
            'test -z "$(find /workspace -type f 2>/dev/null | head -1)" && echo workspace-empty',
        )
        assert audit.stdout.split() == ["none", "none-files", "workspace-empty"], (
            audit.stdout
        )
        env = json.loads(
            docker("image", "inspect", "--format", "{{json .Config.Env}}", ref).stdout
        )
        base_env = json.loads(
            docker("image", "inspect", "--format", "{{json .Config.Env}}", BASE).stdout
        )
        assert sorted(set(env) - set(base_env)) == [
            "ARGUS_CODEX_TURN_TIMEOUT_MS=7200000"
        ]
        history = docker(
            "history", "--no-trunc", "--format", "{{.CreatedBy}}", ref
        ).stdout.lower()
        assert not any(
            w_ in history for w_ in ("bearer ", "api_key", "password=", "token=")
        )

        # 6. what actually runs is checked against what was selected
        name = f"{repo}-session"
        docker(
            "run",
            "-d",
            "--name",
            name,
            *HARDENING[:6],
            "--entrypoint",
            "sleep",
            ref,
            "300",
        )
        ran = docker("inspect", "--format", "{{.Image}}", name).stdout.strip()
        res = await w["manager"].used(
            "agent-b",
            UsedRequest(
                task_id=third.id, attempt=third.attempt, container=name, image_id=ran
            ),
        )
        assert res.verified and ran == image_id
        base_name = f"{repo}-base-session"
        docker(
            "run",
            "-d",
            "--name",
            base_name,
            *HARDENING[:6],
            "--entrypoint",
            "sleep",
            BASE,
            "300",
        )
        base_ran = docker("inspect", "--format", "{{.Image}}", base_name).stdout.strip()
        radar_again = await claim_task(w, "radar", "agent-b", priority=9)
        await w["manager"].prepare("agent-b", prepare_request(w, radar_again))
        wrong = await w["manager"].used(
            "agent-b",
            UsedRequest(
                task_id=radar_again.id,
                attempt=radar_again.attempt,
                container=base_name,
                image_id=base_ran,
            ),
        )
        assert (
            not wrong.verified and base_ran != image_id
        )  # the base image is not the project image
        if evidence_dir:
            async with w["db"].read() as conn:
                (evidence_dir / "radar-runtime-view.json").write_text(
                    (await service.view(conn, "radar")).model_dump_json(indent=2)
                )
    finally:
        _cleanup(repo)


def _services_script(decl_a: str, decl_b: str) -> str:
    """Two tasks in one container: each starts its own PostgreSQL and Valkey from the
    declaration the worker would write, uses them through the injected variables,
    and tears down; one task's cleanup must not touch the other's data."""
    return rf"""
set -eu
mkdir -p /tmp/spec
printf '%s' '{decl_a}' > /tmp/spec/a.json
printf '%s' '{decl_b}' > /tmp/spec/b.json
export ARGUS_TEST_SERVICES_ROOT=/tmp/argus-test-services
# a pre-existing variable pointing at a real database must be overridden
export RADAR_DATABASE_URL=postgresql+asyncpg://prod:prod@db.internal:5432/prod
pgurl() {{ echo "$RADAR_TEST_DATABASE_URL" | sed 's/+asyncpg//'; }}
argus-test-services --spec /tmp/spec/a.json up > /tmp/a.env
argus-test-services --spec /tmp/spec/b.json up > /tmp/b.env
( . /tmp/a.env
  test "$RADAR_DATABASE_URL" = "$RADAR_TEST_DATABASE_URL" && echo A:override-ok
  psql "$(pgurl)" -v ON_ERROR_STOP=1 -c 'CREATE TABLE fx (id int primary key, v text)' -c "INSERT INTO fx VALUES (1, 'a')" >/dev/null
  test "$(psql "$(pgurl)" -tA -c 'SELECT v FROM fx')" = a && echo A:pg-ok
  test "$(psql "$(pgurl)" -tA -c "BEGIN; INSERT INTO fx VALUES (2,'b'); ROLLBACK; SELECT count(*) FROM fx" | tail -1)" = 1 && echo A:rollback-ok
  valkey-cli -u "$RADAR_VALKEY_URL" --no-auth-warning set k a >/dev/null
  test "$(valkey-cli -u "$RADAR_VALKEY_URL" --no-auth-warning get k)" = a && echo A:valkey-ok
  valkey-cli -u "$RADAR_VALKEY_URL" --no-auth-warning ping | grep -q PONG )
( . /tmp/b.env
  psql "$(pgurl)" -v ON_ERROR_STOP=1 -c 'CREATE TABLE fx (id int primary key, v text)' -c "INSERT INTO fx VALUES (1, 'b')" >/dev/null
  test "$(psql "$(pgurl)" -tA -c 'SELECT v FROM fx')" = b && echo B:pg-ok
  test -z "$(valkey-cli -u "$RADAR_VALKEY_URL" --no-auth-warning get k)" && echo B:valkey-ok )
argus-test-services --spec /tmp/spec/a.json down
( . /tmp/b.env
  test "$(psql "$(pgurl)" -tA -c 'SELECT v FROM fx')" = b
  valkey-cli -u "$RADAR_VALKEY_URL" --no-auth-warning ping | grep -q PONG && echo ISOLATED )
argus-test-services --spec /tmp/spec/b.json down
test -z "$(ls -A /tmp/argus-test-services)"
! pgrep -x postgres >/dev/null && ! pgrep -x valkey-server >/dev/null && echo CLEAN
"""


async def _builds(db: Database, project: str) -> list:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT * FROM runtime_builds WHERE project = ?", (project,)
        ) as cur,
    ):
        return await cur.fetchall()


def _cleanup(repo: str) -> None:
    """Remove only what this module created: containers, then the repo's images and tags."""
    for cid in docker(
        "ps", "-aq", "--filter", f"name={repo}", check=False
    ).stdout.split():
        docker("rm", "-f", cid, check=False)
    for cid in docker(
        "ps", "-aq", "--filter", f"label=dev.argus.runtime.owner={repo}", check=False
    ).stdout.split():
        docker("rm", "-f", cid, check=False)
    refs = docker(
        "image", "ls", "--format", "{{.Repository}}:{{.Tag}}", check=False
    ).stdout.split()
    for ref in refs:
        if ref.startswith((f"{repo}:", f"{repo}-base:")):
            docker("rmi", ref, check=False)
