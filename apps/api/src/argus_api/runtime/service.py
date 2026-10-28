"""Project runtime images (docs/specs/project-runtime.spec.md).

Reads and writes are short transactions; every Docker call (inspect, build, probe)
runs outside one. The builder is an in-process loop on this node's daemon: one
build at a time, deduplicated by the `runtime_builds` primary key, resumable
after a restart through `working_until`.
"""

import asyncio
import contextlib
import json
import logging
import re
from datetime import timedelta

import aiosqlite
from argus_core.runtime import (
    BuildView,
    GapView,
    HoldRequest,
    PrepareRequest,
    Recipe,
    RuntimeSpec,
    RuntimeSpecError,
    RuntimeView,
    SelectedImage,
    Selection,
    ToolEvidence,
    UsedRequest,
    UsedResult,
    build_key,
    contract_services,
    helper_bytes,
    image_ref,
    probes,
    recipe_digest,
    render_dockerfile,
    required_probe_names,
    requirement,
    resolve,
)

from argus_api.config import Settings
from argus_api.db import Database, Tx, ts
from argus_api.errors import ConflictError, NotFoundError, ValidationFailedError
from argus_api.events import record
from argus_api.runtime.docker import KEY_LABEL, Docker, DockerError
from argus_api.runtime.gate import spec_of
from argus_api.tasks import service as tasks

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3  # builds interrupted this often are failed, not retried forever
WORK_SLACK_S = 600  # probes after the build


# -- the declaration ------------------------------------------------------------------


async def put_spec(tx: Tx, project: str, spec: RuntimeSpec, actor: str) -> Recipe:
    """Store the declaration after resolving it: an invalid or unapproved one is
    refused with every problem listed, and nothing changes."""
    async with tx.conn.execute(
        "SELECT 1 FROM projects WHERE slug = ?", (project,)
    ) as cur:
        if await cur.fetchone() is None:
            raise NotFoundError(f"unknown project: {project}", code="unknown_project")
    try:
        recipe = resolve(project, spec)
    except RuntimeSpecError as e:
        raise ValidationFailedError(
            "the runtime declaration is not approved",
            code="runtime_invalid",
            details=e.problems,
        ) from e
    before, declared = await spec_of(tx.conn, project)
    old = recipe_digest(resolve(project, before)) if declared else None
    new = recipe_digest(recipe)
    await tx.conn.execute(
        "INSERT INTO project_runtimes (project, spec, updated_by, updated_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (project) DO UPDATE SET spec = excluded.spec,"
        " updated_by = excluded.updated_by, updated_at = excluded.updated_at",
        (project, spec.model_dump_json(), actor, ts(tx.now)),
    )
    await record(
        tx, actor, "project", project, "project.runtime_set", {"recipe_digest": new}
    )
    if new != old:  # a different image: held tasks are evaluated against it
        await tasks.release_runtime_holds(
            tx, project, (tasks.RUNTIME_FAILED, tasks.RUNTIME_PREPARING), actor
        )
    return recipe


async def rebuild(tx: Tx, project: str, actor: str) -> int:
    """Operator retry of failed builds of the project's current recipe."""
    recipe = resolve(project, (await spec_of(tx.conn, project))[0])
    cur = await tx.conn.execute(
        "UPDATE runtime_builds SET state = 'preparing', reason = NULL, attempts = 0,"
        " working_until = NULL, finished_at = NULL, updated_at = ?"
        " WHERE project = ? AND state = 'failed' AND recipe_digest = ?",
        (ts(tx.now), project, recipe_digest(recipe)),
    )
    await record(
        tx,
        actor,
        "project",
        project,
        "project.runtime_rebuild",
        {"builds": cur.rowcount},
    )
    await tasks.release_runtime_holds(tx, project, (tasks.RUNTIME_FAILED,), actor)
    return cur.rowcount


def _evidence(raw: str) -> list[ToolEvidence]:
    return [ToolEvidence.model_validate(e) for e in json.loads(raw)]


async def view(conn: aiosqlite.Connection, project: str) -> RuntimeView:
    async with conn.execute("SELECT 1 FROM projects WHERE slug = ?", (project,)) as cur:
        if await cur.fetchone() is None:
            raise NotFoundError(f"unknown project: {project}", code="unknown_project")
    spec, declared = await spec_of(conn, project)
    problems: list[str] = []
    try:
        recipe = resolve(project, spec)
        digest = recipe_digest(recipe)
    except RuntimeSpecError as e:  # only after a catalogue change; shown, not hidden
        problems, recipe, digest = e.problems, resolve(project, RuntimeSpec()), ""
    async with conn.execute(
        "SELECT * FROM runtime_builds WHERE project = ? ORDER BY updated_at DESC LIMIT 20",
        (project,),
    ) as cur:
        rows = await cur.fetchall()
    return RuntimeView(
        project=project,
        declared=declared,
        spec=spec,
        problems=problems,
        requirement=requirement(recipe),
        gaps=[GapView(component=g.component, reason=g.reason) for g in recipe.gaps],
        services=list(recipe.services),
        builds=[
            BuildView(
                build_key=r["build_key"],
                host_id=r["host_id"],
                run_as=r["run_as"],
                state=r["state"],
                reason=r["reason"],
                image_ref=r["image_ref"],
                image_id=r["image_id"],
                base_ref=r["base_ref"],
                base_id=r["base_id"],
                platform=r["platform"],
                attempts=r["attempts"],
                evidence=_evidence(r["evidence"]),
                current=r["recipe_digest"] == digest,
                updated_at=r["updated_at"],
            )
            for r in rows
        ],
    )


async def task_runtime(conn: aiosqlite.Connection, task_id: str) -> list[dict]:
    """Selected versus used image of every attempt of the task."""
    async with conn.execute(
        "SELECT * FROM task_runtimes WHERE task_id = ? ORDER BY attempt", (task_id,)
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


# -- the manager ----------------------------------------------------------------------


class RuntimeManager:
    def __init__(self, db: Database, docker: Docker, settings: Settings) -> None:
        self.db = db
        self.docker = docker
        self.settings = settings
        self._wake = asyncio.Event()

    def wake(self) -> None:
        self._wake.set()

    # worker-facing ----------------------------------------------------------------

    async def prepare(self, agent_id: str, req: PrepareRequest) -> Selection:
        """Resolve the claimed task's project image for this worker's host, held or
        ready. Nothing but a `ready` answer lets the worker start the model."""
        async with self.db.read() as conn:
            task = await _claimed(conn, agent_id, req.task_id, req.attempt)
            project = task["project"]
            spec, declared = await spec_of(conn, project)
            bases = await _agent_images(conn, self.settings)
        if not self.settings.project_runtime:
            async with self.db.write() as tx:
                await tx.conn.execute(
                    "DELETE FROM runtime_hosts WHERE agent_id = ?", (agent_id,)
                )
            return Selection(
                state="disabled",
                project=project,
                declared=declared,
                reason="project runtime images are off on this Argus (ARGUS_PROJECT_RUNTIME=off)",
            )
        try:
            recipe = resolve(project, spec)
        except RuntimeSpecError as e:
            return await self._hold_invalid(agent_id, req, project, declared, e)
        if req.base_ref not in bases:
            raise ValidationFailedError(
                f"{req.base_ref} is not the agent image of any NanoClaw pool; "
                "a project image is only derived from a pool's agent image",
                code="runtime_base_not_allowed",
            )
        failure, unverified = None, False
        try:
            daemon = await self.docker.daemon()
            local_base = await self.docker.image_id(req.base_ref)
        except DockerError:
            # Argus cannot look right now: the builder retries (bounded), the task waits.
            daemon, local_base, unverified = None, None, True
        if daemon is not None:
            if daemon.id != req.host_id:
                failure = (
                    f"the worker's Docker daemon ({req.host_id}) is not the one Argus builds on"
                    f" ({daemon.id}); project images are built on the Argus node's daemon only"
                )
            elif local_base is None:
                failure = f"base image {req.base_ref} is not present on the Argus node's daemon"
            elif local_base != req.base_id:
                failure = f"base image {req.base_ref} is {local_base} on Argus but {req.base_id} on the worker"
            elif daemon.platform != req.platform:
                failure = (
                    f"platform differs: Argus {daemon.platform}, worker {req.platform}"
                )
        digest = recipe_digest(recipe)
        key = build_key(recipe, req.base_id, req.platform)
        ref = image_ref(recipe, key, self.settings.runtime_image_repo)
        async with self.db.read() as conn:
            existing = await _build(conn, project, key, req.host_id, req.run_as)
        # A build is only as good as the image still on the daemon.
        stale = False
        if existing is not None and existing["state"] == "ready":
            try:
                stale = (
                    unverified
                    or await self.docker.image_id(ref) != existing["image_id"]
                )
            except DockerError:
                stale = True
        async with self.db.write() as tx:
            await _upsert_host(tx, agent_id, req)
            row = await _build_tx(tx, project, key, req.host_id, req.run_as)
            requested = row is None or (stale and row["state"] == "ready")
            if row is None:
                await tx.conn.execute(
                    "INSERT INTO runtime_builds (project, build_key, host_id, run_as, recipe_digest,"
                    " state, reason, recipe, base_ref, base_id, platform, image_ref, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, 'preparing', NULL, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        project,
                        key,
                        req.host_id,
                        req.run_as,
                        digest,
                        spec.model_dump_json(),
                        req.base_ref,
                        req.base_id,
                        req.platform,
                        ref,
                        ts(tx.now),
                        ts(tx.now),
                    ),
                )
            elif requested:
                await _set_state(
                    tx, row, "preparing", None, image_id=None, finished=False
                )
            if requested:
                await record(
                    tx,
                    f"agent:{agent_id}",
                    "project",
                    project,
                    "runtime.requested",
                    {"build_key": key, "host_id": req.host_id, "image_ref": ref},
                )
            row = await _build_tx(tx, project, key, req.host_id, req.run_as)
            assert row is not None
            if failure is not None and row["state"] != "failed":
                await _fail(tx, row, failure)
                row = await _build_tx(tx, project, key, req.host_id, req.run_as)
                assert row is not None
            selection = _selection(recipe, declared, row)
            if row["state"] == "ready":
                await tx.conn.execute(
                    "INSERT OR REPLACE INTO task_runtimes (task_id, attempt, project, build_key,"
                    " image_ref, image_id, status, selected_at) VALUES (?, ?, ?, ?, ?, ?, 'selected', ?)",
                    (
                        req.task_id,
                        req.attempt,
                        project,
                        key,
                        row["image_ref"],
                        row["image_id"],
                        ts(tx.now),
                    ),
                )
                await record(
                    tx,
                    f"agent:{agent_id}",
                    "task",
                    req.task_id,
                    "runtime.selected",
                    {
                        "project": project,
                        "image_ref": row["image_ref"],
                        "image_id": row["image_id"],
                        "build_key": key,
                    },
                )
            else:
                failed = row["state"] == "failed"
                await tasks.hold_for_runtime(
                    tx,
                    agent_id,
                    req.task_id,
                    req.attempt,
                    tasks.RUNTIME_FAILED if failed else tasks.RUNTIME_PREPARING,
                    row["reason"] if failed else None,
                )
                selection.task_held = True
        self.wake()
        return selection

    async def _hold_invalid(
        self,
        agent_id: str,
        req: PrepareRequest,
        project: str,
        declared: bool,
        err: RuntimeSpecError,
    ) -> Selection:
        reason = (
            "the project's runtime declaration is no longer approved: "
            + "; ".join(err.problems)
        )
        async with self.db.write() as tx:
            await tasks.hold_for_runtime(
                tx, agent_id, req.task_id, req.attempt, tasks.RUNTIME_FAILED, reason
            )
        return Selection(
            state="failed",
            project=project,
            declared=declared,
            reason=reason,
            task_held=True,
        )

    async def hold(self, agent_id: str, req: HoldRequest) -> None:
        async with self.db.write() as tx:
            await tasks.hold_for_runtime(
                tx, agent_id, req.task_id, req.attempt, tasks.RUNTIME_FAILED, req.error
            )

    async def used(self, agent_id: str, req: UsedRequest) -> UsedResult:
        """The worker reports the image its session container actually runs. A
        mismatch holds the task: a labelled or mounted image proves nothing."""
        async with self.db.write() as tx:
            await tasks._owned(tx, req.task_id, req.attempt, agent_id)
            async with tx.conn.execute(
                "SELECT * FROM task_runtimes WHERE task_id = ? AND attempt = ?",
                (req.task_id, req.attempt),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise ConflictError(
                    "no runtime image was selected for this attempt; call prepare first",
                    code="runtime_not_selected",
                )
            ok = req.image_id == row["image_id"]
            await tx.conn.execute(
                "UPDATE task_runtimes SET used_image_id = ?, status = ?, verified_at = ?"
                " WHERE task_id = ? AND attempt = ?",
                (
                    req.image_id,
                    "verified" if ok else "mismatch",
                    ts(tx.now),
                    req.task_id,
                    req.attempt,
                ),
            )
            await record(
                tx,
                f"agent:{agent_id}",
                "task",
                req.task_id,
                "runtime.verified" if ok else "runtime.mismatch",
                {
                    "selected": row["image_id"],
                    "used": req.image_id,
                    "container": req.container,
                },
            )
            if not ok:
                await tasks.hold_for_runtime(
                    tx,
                    agent_id,
                    req.task_id,
                    req.attempt,
                    tasks.RUNTIME_FAILED,
                    f"session container ran image {req.image_id}, not the selected {row['image_id']}",
                )
        return UsedResult(
            verified=ok, selected_image_id=row["image_id"], used_image_id=req.image_id
        )

    # the builder -------------------------------------------------------------------

    async def run(self, interval_s: float = 2.0) -> None:
        try:
            removed = await self.docker.remove_owned_containers()
            if removed:
                log.warning("removed %d orphaned runtime probe containers", removed)
        except DockerError as e:
            # Expected when Docker is not mounted (it is optional): one line, no trace.
            log.warning("could not clean orphaned runtime probe containers: %s", e)
        while True:
            try:
                async with self.db.write() as tx:
                    await reap_interrupted(tx)
                while await self.work_once():
                    pass
            except Exception:
                log.exception("runtime builder pass failed; continuing")
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), interval_s)

    async def work_once(self) -> bool:
        """Take the oldest preparing build and finish it. False: nothing to do."""
        async with self.db.write() as tx:
            async with tx.conn.execute(
                "SELECT * FROM runtime_builds WHERE state = 'preparing' ORDER BY created_at LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return False
            deadline = tx.now + timedelta(
                seconds=self.settings.runtime_build_timeout_s + WORK_SLACK_S
            )
            await tx.conn.execute(
                "UPDATE runtime_builds SET state = 'building', attempts = attempts + 1,"
                " working_until = ?, updated_at = ? WHERE project = ? AND build_key = ?"
                " AND host_id = ? AND run_as = ?",
                (ts(deadline), ts(tx.now), *_pk(row)),
            )
            await record(
                tx,
                "system",
                "project",
                row["project"],
                "runtime.building",
                {"build_key": row["build_key"]},
            )
        await self._build(row)
        return True

    async def _build(self, row: aiosqlite.Row) -> None:
        """Image, then probes, then `ready` or `failed` with the reason. A build
        key is content, so an image already carrying it is reused unrebuilt."""
        spec = RuntimeSpec.model_validate_json(row["recipe"])
        recipe = resolve(row["project"], spec)
        if recipe_digest(recipe) != row["recipe_digest"]:
            async with (
                self.db.write() as tx
            ):  # the renderer changed under a queued build
                await tx.conn.execute(
                    "DELETE FROM runtime_builds WHERE project = ? AND build_key = ? AND host_id = ? AND run_as = ?",
                    _pk(row),
                )
            return
        ref = row["image_ref"]
        try:
            if await self.docker.image_id(row["base_ref"]) != row["base_id"]:
                return await self._finish_failed(
                    row, f"base image {row['base_ref']} changed since it was reported"
                )
            image_id = None
            if await self.docker.image_label(ref, KEY_LABEL) == row["build_key"]:
                image_id = await self.docker.image_id(ref)
            if image_id is None:
                alias = (
                    f"{self.settings.runtime_image_repo}-base:{row['base_id'][7:19]}"
                )
                await self.docker.tag(row["base_id"], alias)
                built = await self.docker.build(
                    dockerfile=render_dockerfile(recipe),
                    files={"argus-test-services": helper_bytes()},
                    base=alias,
                    ref=ref,
                    key=row["build_key"],
                    timeout_s=self.settings.runtime_build_timeout_s,
                )
                if not built.ok:
                    return await self._finish_failed(
                        row, f"image build failed: {built.log_tail[-600:]}"
                    )
                image_id = await self.docker.image_id(ref)
            if image_id is None:
                return await self._finish_failed(
                    row, "image build reported success but the image is missing"
                )
            async with self.db.write() as tx:
                await tx.conn.execute(
                    "UPDATE runtime_builds SET state = 'verifying', image_id = ?, updated_at = ?"
                    " WHERE project = ? AND build_key = ? AND host_id = ? AND run_as = ?",
                    (image_id, ts(tx.now), *_pk(row)),
                )
            evidence = await self._probe(recipe, ref, row["run_as"])
        except DockerError as e:
            return await self._retry_or_fail(row, str(e))
        bad = [e.name for e in evidence if not e.ok]
        missing = required_probe_names(recipe) - {e.name for e in evidence}
        async with self.db.write() as tx:
            fresh = await _build_tx(
                tx, row["project"], row["build_key"], row["host_id"], row["run_as"]
            )
            if fresh is None:
                return
            await tx.conn.execute(
                "UPDATE runtime_builds SET evidence = ? WHERE project = ? AND build_key = ? AND host_id = ? AND run_as = ?",
                (json.dumps([e.model_dump() for e in evidence]), *_pk(row)),
            )
            if bad or missing:
                await _fail(
                    tx,
                    fresh,
                    "verification failed: "
                    + ", ".join(
                        sorted(bad) + [f"{m} (not run)" for m in sorted(missing)]
                    ),
                )
            else:
                await _ready(tx, fresh, image_id)
        self._wake.set()

    async def _probe(self, recipe: Recipe, ref: str, run_as: str) -> list[ToolEvidence]:
        evidence = []
        for p in probes(recipe):
            res = await self.docker.probe(ref, p, run_as)
            matched = bool(re.search(p.pattern, res.output, re.MULTILINE))
            evidence.append(
                ToolEvidence(
                    name=p.name, ok=res.ok and matched, output=res.output.strip()[-400:]
                )
            )
        return evidence

    async def _retry_or_fail(self, row: aiosqlite.Row, reason: str) -> None:
        """Docker itself failed (daemon down, call timed out): try again a bounded
        number of times before the failure becomes durable."""
        if row["attempts"] >= MAX_ATTEMPTS:
            return await self._finish_failed(row, reason)
        async with self.db.write() as tx:
            fresh = await _build_tx(
                tx, row["project"], row["build_key"], row["host_id"], row["run_as"]
            )
            if fresh is not None:
                await _set_state(
                    tx, fresh, "preparing", None, image_id=None, finished=False
                )
        await asyncio.sleep(min(5.0 * row["attempts"], 30.0))

    async def _finish_failed(self, row: aiosqlite.Row, reason: str) -> None:
        async with self.db.write() as tx:
            fresh = await _build_tx(
                tx, row["project"], row["build_key"], row["host_id"], row["run_as"]
            )
            if fresh is not None:
                await _fail(tx, fresh, reason)


# -- transactions on build rows ---------------------------------------------------------


def _pk(row: aiosqlite.Row) -> tuple[str, str, str, str]:
    return (row["project"], row["build_key"], row["host_id"], row["run_as"])


async def _build(
    conn: aiosqlite.Connection, project: str, key: str, host: str, run_as: str
):
    async with conn.execute(
        "SELECT * FROM runtime_builds WHERE project = ? AND build_key = ? AND host_id = ? AND run_as = ?",
        (project, key, host, run_as),
    ) as cur:
        return await cur.fetchone()


async def _build_tx(tx: Tx, project: str, key: str, host: str, run_as: str):
    return await _build(tx.conn, project, key, host, run_as)


async def _set_state(
    tx: Tx,
    row: aiosqlite.Row,
    state: str,
    reason: str | None,
    *,
    image_id: str | None,
    finished: bool,
) -> None:
    await tx.conn.execute(
        "UPDATE runtime_builds SET state = ?, reason = ?, image_id = ?, finished_at = ?,"
        " working_until = NULL, updated_at = ? WHERE project = ? AND build_key = ?"
        " AND host_id = ? AND run_as = ?",
        (
            state,
            reason,
            image_id,
            ts(tx.now) if finished else None,
            ts(tx.now),
            *_pk(row),
        ),
    )


async def _ready(tx: Tx, row: aiosqlite.Row, image_id: str) -> None:
    await _set_state(tx, row, "ready", None, image_id=image_id, finished=True)
    await record(
        tx,
        "system",
        "project",
        row["project"],
        "runtime.ready",
        {
            "build_key": row["build_key"],
            "image_ref": row["image_ref"],
            "image_id": image_id,
        },
    )
    await tasks.release_runtime_holds(tx, row["project"], (tasks.RUNTIME_PREPARING,))


async def _fail(tx: Tx, row: aiosqlite.Row, reason: str) -> None:
    reason = reason[:1500]
    await _set_state(tx, row, "failed", reason, image_id=row["image_id"], finished=True)
    await record(
        tx,
        "system",
        "project",
        row["project"],
        "runtime.failed",
        {"build_key": row["build_key"], "reason": reason},
    )
    await tasks.fail_runtime_holds(tx, row["project"], reason)


async def reap_interrupted(tx: Tx) -> list[str]:
    """Builds whose worker (this process, before a restart) is gone: back to
    `preparing`, or `failed` once they were interrupted `MAX_ATTEMPTS` times."""
    async with tx.conn.execute(
        "SELECT * FROM runtime_builds WHERE state IN ('building', 'verifying') AND working_until < ?",
        (ts(tx.now),),
    ) as cur:
        rows = await cur.fetchall()
    for r in rows:
        if r["attempts"] >= MAX_ATTEMPTS:
            await _fail(
                tx,
                r,
                f"the build was interrupted {r['attempts']} times (restart or timeout)",
            )
        else:
            await _set_state(tx, r, "preparing", None, image_id=None, finished=False)
    return [r["build_key"] for r in rows]


async def _upsert_host(tx: Tx, agent_id: str, req: PrepareRequest) -> None:
    await tx.conn.execute(
        "INSERT INTO runtime_hosts (agent_id, host_id, run_as, base_ref, base_id, platform, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (agent_id) DO UPDATE SET host_id = excluded.host_id,"
        " run_as = excluded.run_as, base_ref = excluded.base_ref, base_id = excluded.base_id,"
        " platform = excluded.platform, updated_at = excluded.updated_at",
        (
            agent_id,
            req.host_id,
            req.run_as,
            req.base_ref,
            req.base_id,
            req.platform,
            ts(tx.now),
        ),
    )


async def _claimed(
    conn: aiosqlite.Connection, agent_id: str, task_id: str, attempt: int
):
    async with conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"unknown task: {task_id}", code="unknown_task")
    if (
        row["state"] != "claimed"
        or row["assignee_agent_id"] != agent_id
        or row["attempt"] != attempt
    ):
        raise ConflictError(
            f"task {task_id} is not claimed by this agent at attempt {attempt}",
            code="not_claimed",
        )
    return row


async def _agent_images(conn: aiosqlite.Connection, settings: Settings) -> set[str]:
    """The agent images Argus itself knows: pools' `nanoclaw.agent_image` and the
    node setting. A worker cannot ask for a build on an arbitrary local image."""
    images = {settings.nanoclaw_agent_image} if settings.nanoclaw_agent_image else set()
    async with conn.execute(
        "SELECT config FROM pools WHERE runtime = 'nanoclaw'"
    ) as cur:
        for r in await cur.fetchall():
            if image := (json.loads(r["config"]).get("nanoclaw") or {}).get(
                "agent_image"
            ):
                images.add(image)
    return images


def _selection(recipe: Recipe, declared: bool, row: aiosqlite.Row) -> Selection:
    ready = row["state"] == "ready"
    return Selection(
        state=row["state"],
        project=recipe.project,
        declared=declared,
        reason=row["reason"],
        build_key=row["build_key"],
        requirement=requirement(recipe),
        image=SelectedImage(
            ref=row["image_ref"],
            id=row["image_id"],
            base_ref=row["base_ref"],
            base_id=row["base_id"],
            platform=row["platform"],
        )
        if ready
        else None,
        tools=_evidence(row["evidence"]),
        services=contract_services(recipe) if ready else [],
        gaps=[GapView(component=g.component, reason=g.reason) for g in recipe.gaps],
    )
