"""Disk guard against a real, disposable runtime (operations.spec.md 3.5).

Opt-in: `ARGUS_DISK_GUARD_REAL_RUNTIME=1` / `make test-disk-guard-runtime`, as root
on a Linux host with cgroup v2. The production `DiskGuard.run_pass` and
`PauseController` drive a real `DockerTarget` on a dockerd the harness started
(own socket, data-root and exec-root in its temp dir) while the harness fills a
512 MiB ext4 loop filesystem that holds the database, the instance directories
and the Docker data root. Guard thresholds are scaled to that size.

Opted in on a host without the prerequisites, the test fails and names them: a
skip is never evidence. The JSON evidence file records each check as observed.
"""

from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path
from typing import Any

import pytest
from api_support import FakeClock, FakeTarget
from argus_api.config import Settings
from argus_api.db import Database, ts
from argus_api.disk_guard import DiskGuard
from argus_api.disk_guard_pause import PauseController, container_io_stat
from argus_api.errors import ConflictError
from argus_api.fleet.runtimes import descendant_label
from argus_api.fleet.targets import INSTANCE_LABEL, POOL_LABEL, TargetRegistry
from argus_api.ops import LoopStats
from argus_api.reaper import reap
from argus_api.tasks.views import read_task
from disk_guard_runtime import (
    MIB,
    OPT_IN,
    POOL,
    Evidence,
    Harness,
    Refused,
    gate,
    seed_world,
    wait_until,
)

pytestmark = pytest.mark.skipif(
    os.environ.get(OPT_IN) != "1",
    reason=f"opt-in real runtime: set {OPT_IN}=1 (make test-disk-guard-runtime)",
)

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path(
    os.environ.get(
        "ARGUS_DISK_GUARD_RT_EVIDENCE", ROOT / ".argus" / "disk-guard-runtime.json"
    )
)
IMAGE = os.environ.get("ARGUS_DISK_GUARD_RT_IMAGE", "busybox:1.36.1")
IMAGE_TAR = os.environ.get("ARGUS_DISK_GUARD_RT_IMAGE_TAR")
WARN, CLEAR, REFUSE, RESUME = 192 * MIB, 224 * MIB, 96 * MIB, 128 * MIB
LOW, DEAD, OK, ABOVE_CLEAR, IN_WARN = (
    80 * MIB,
    112 * MIB,
    200 * MIB,
    256 * MIB,
    160 * MIB,
)
SHED_AFTER_S = 300.0
WAIT_S = 30.0
# The heavy writer rewrites a small preallocated file with fsync; the light one
# ticks a counter next to a large idle file. io.stat ranks the heavy one first,
# directory size the light one, so the shed victim shows which basis was used.
HEAVY = (
    "i=0; while :; do i=$((i+1));"
    " dd if=/dev/zero of=/w/blob bs=1M count=8 conv=notrunc,fsync 2>/dev/null;"
    " echo $i > /w/beat; done"
)
LIGHT = "i=0; while :; do i=$((i+1)); echo $i > /w/beat; sleep 1; done"
STILL_UNVERIFIED = [
    "production thresholds and a real host disk (scaled 512 MiB filesystem only)",
    "real NanoClaw host and session containers (busybox stand-ins with its labels)",
    "the systemd cgroup driver path of container_io_stat (cgroupfs driver used)",
    "the failed-measurement pause and the stale snapshot (no injected faults)",
    "adoption of already-paused containers after an API restart",
    "remote Docker daemons and Kubernetes (warn/refuse only by design)",
]


def wbytes(container_id: str) -> int | None:
    snap = container_io_stat(container_id)
    return sum(snap.wbytes_by_device.values()) if snap.readable else None


def beat(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def load_image(client: Any) -> str:
    try:
        if IMAGE_TAR:
            with open(IMAGE_TAR, "rb") as f:
                return client.images.load(f.read())[0].tags[0]
        client.images.pull(IMAGE)
        return IMAGE
    except Exception as exc:
        raise RuntimeError(
            f"missing prerequisite: image {IMAGE_TAR or IMAGE} for the isolated"
            f" daemon ({exc}); set ARGUS_DISK_GUARD_RT_IMAGE_TAR to a `docker save`"
            " tarball on hosts without registry access"
        ) from exc


class Run:
    """One scenario: the production guard and pause controller on real runtime."""

    def __init__(
        self, h: Harness, ev: Evidence, db: Database, clock: FakeClock
    ) -> None:
        self.h, self.ev, self.db = h, ev, db
        self.clock = clock  # the database clock: lease and quiet time
        self.mono = [1000.0]  # the controller's clock: renewal and shed periods
        lay = h.layout
        self.host = f"unix://{lay.socket}"
        self.settings = Settings(
            data_dir=lay.data_dir,
            disk_guard_enabled=True,
            disk_guard_warn_bytes=WARN,
            disk_guard_warn_clear_bytes=CLEAR,
            disk_guard_refuse_bytes=REFUSE,
            disk_guard_resume_bytes=RESUME,
            disk_guard_interval_s=1.0,
            disk_guard_pause=True,
            disk_guard_shed_after_s=SHED_AFTER_S,
            nanoclaw_instances_root=str(lay.instances),
            nanoclaw_docker_socket=str(lay.socket),
        )
        self.controller = PauseController(
            self.settings, db, TargetRegistry(), clock=lambda: self.mono[0]
        )
        self.guard = DiskGuard(
            self.settings, db, LoopStats("disk_guard", 1.0), pause=self.controller
        )

    async def guard_pass(self, step: str, free: int | None) -> dict[str, Any]:
        actual = self.h.set_free(free) if free is not None else self.h.free_bytes()
        await self.guard.run_pass()
        snap = self.guard.snapshot()
        seen = {
            "target_free": free,
            "statvfs_free": actual,
            "guard_free": snap.free_bytes,
            "measurement": snap.measurement,
            "claim_admission": snap.claim_admission,
            "warn_episode": snap.warn_episode,
            "roots": {r.role: r.applicability for r in snap.roots},
        }
        self.ev.observed.setdefault("passes", []).append({"step": step, **seen})
        return seen

    def state(self, c: Any) -> dict[str, Any]:
        s = self.h.client.api.inspect_container(c.id)["State"]
        return {"status": s["Status"], "paused": s["Paused"]}

    async def row(self, task_id: str) -> Any:
        async with (
            self.db.read() as conn,
            conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur,
        ):
            return await cur.fetchone()

    async def events(self, kind: str) -> list[str]:
        async with (
            self.db.read() as conn,
            conn.execute("SELECT data FROM events WHERE kind = ?", (kind,)) as cur,
        ):
            return [r["data"] for r in await cur.fetchall()]

    def tick(self, seconds: float) -> None:
        self.clock.advance(seconds)
        self.mono[0] += seconds

    def admission_refused(self) -> bool:
        try:
            self.guard.require_admission()
        except ConflictError as exc:
            return exc.code == "disk_low"
        return False


async def scenario(h: Harness, ev: Evidence) -> None:
    check = ev.check
    lay = h.layout
    h.mount_fs()
    h.verify_fs()
    h.start_daemon()
    ev.observed["daemon"] = h.preflight()  # raises Refused: nothing destructive yet
    image = load_image(h.client)
    clock_db = FakeClock()
    db = await Database.open(lay.data_dir / "argus.db", readers=2, clock=clock_db)
    try:
        run = Run(h, ev, db, clock_db)
        fake = FakeTarget()
        fake.shares_filesystem = True
        heavy, light = await seed_world(db, run.host, lay.instances, 2, fake)
        for s, name, size in ((heavy, "blob", 8), (light, "idle", 48)):
            d = lay.instances / s.instance_id
            d.mkdir()
            with (d / name).open("wb") as f:
                os.posix_fallocate(f.fileno(), 0, size * MIB)
        pool = await _pool(db)
        key = descendant_label(pool)
        assert key is not None

        def start(name: str, cmd: str, labels: dict[str, str], s: Any = None) -> Any:
            volumes = (
                {str(lay.instances / s.instance_id): {"bind": "/w", "mode": "rw"}}
                if s
                else {}
            )
            return h.client.containers.run(
                image,
                ["sh", "-c", cmd],
                name=name,
                detach=True,
                network_mode="none",
                labels=h.labels(**labels),
                volumes=volumes,
            )

        # The Argus-started host container even carries a descendant label: it
        # sends the heartbeats and must never be paused.
        host_c = start(
            "argus-dg-host",
            "while :; do sleep 1; done",
            {
                POOL_LABEL: POOL,
                INSTANCE_LABEL: heavy.instance_id,
                key: heavy.group_folder,
            },
        )
        heavy_c = start("argus-dg-heavy", HEAVY, {key: heavy.group_folder}, heavy)
        light_c = start("argus-dg-light", LIGHT, {key: light.group_folder}, light)
        heavy_beat = lay.instances / heavy.instance_id / "beat"
        light_beat = lay.instances / light.instance_id / "beat"
        wait_until(lambda: beat(heavy_beat) and beat(light_beat), "writers", WAIT_S)

        # cgroup io.stat: readable per container, and does it see the writes?
        io_readable = wbytes(heavy_c.id) is not None
        ev.observed["io_stat_readable"] = io_readable
        if io_readable:
            with contextlib.suppress(TimeoutError):
                wait_until(lambda: (wbytes(heavy_c.id) or 0) > 0, "io.stat", WAIT_S)
            seen = {"heavy": wbytes(heavy_c.id), "light": wbytes(light_c.id)}
            check(
                "io.stat attributes the heavy writer's writes",
                (seen["heavy"] or 0) > (seen["light"] or 0),
                seen,
            )
        else:
            ev.unverified.append(
                "cgroup io.stat unreadable at the production paths for this daemon:"
                " ranking falls back to directory size"
            )

        # 1. Baseline: every root measured on the bounded filesystem.
        p = await run.guard_pass("baseline", None)
        check(
            "baseline measures data, instances and docker roots",
            p["roots"] == dict.fromkeys(("data", "instances", "docker"), "measured"),
            p,
        )
        if not check(
            "baseline is above warn clear",
            p["claim_admission"] == "admitted" and not p["warn_episode"],
            p,
        ):
            raise RuntimeError(f"bounded filesystem starts too full: {p}")

        # 2. Warn band: one event, claims admitted, nothing paused.
        p = await run.guard_pass("warn", IN_WARN)
        check(
            "warn enters an episode and admits",
            p["warn_episode"] and p["claim_admission"] == "admitted",
            p,
        )
        check("one disk.warn event", len(await run.events("disk.warn")) == 1)
        check(
            "warn pauses nothing",
            not run.state(heavy_c)["paused"] and not run.state(light_c)["paused"],
        )

        # 3. Refuse band: claims refused, descendants frozen, host untouched.
        p = await run.guard_pass("refuse", LOW)
        check("refuse band refuses claims with disk_low", run.admission_refused(), p)
        states = {
            n: run.state(c)
            for n, c in (("host", host_c), ("heavy", heavy_c), ("light", light_c))
        }
        check(
            "descendant containers really paused",
            states["heavy"]["paused"] and states["light"]["paused"],
            states,
        )
        check("host container not paused", not states["host"]["paused"], states)
        rows = {s.task_id: await run.row(s.task_id) for s in (heavy, light)}
        check(
            "claims flagged paused_disk_at",
            all(r["paused_disk_at"] for r in rows.values()),
        )
        async with db.read() as conn:
            causes = [
                (await read_task(conn, s.task_id, clock_db.now)).stall_cause
                for s in (heavy, light)
            ]
        check(
            "stall_cause reports paused_disk",
            causes == ["paused_disk", "paused_disk"],
            causes,
        )
        check("one disk.pause event", len(await run.events("disk.pause")) == 1)
        frozen_beat = beat(light_beat)
        lease0 = rows[light.task_id]["lease_expires_at"]

        # 4. Guard-authoritative renewal; reaper skips the silent paused claims.
        run.tick(70)
        await run.guard_pass("renew", LOW)
        lease1 = (await run.row(light.task_id))["lease_expires_at"]
        check(
            "guard renews the paused lease",
            lease1 > lease0,
            {"before": lease0, "after": lease1},
        )
        run.tick(70)  # past the original deadline, inside the renewed one
        async with db.write() as tx:
            # The host container keeps the agents alive (its heartbeats); the
            # paused sessions send nothing, so only the guard renews their leases.
            await tx.conn.execute(
                "UPDATE agents SET last_heartbeat_at = ?", (ts(tx.now),)
            )
            reaped = await reap(tx, quiet_claim_s=30, stall_fail_s=60)
        check(
            "reaper neither expires nor flags paused claims",
            not reaped.expired and not reaped.quieted,
            {"expired": reaped.expired, "quieted": reaped.quieted},
        )
        await run.guard_pass("renew-2", LOW)
        rows = {s.task_id: await run.row(s.task_id) for s in (heavy, light)}
        check(
            "paused claims still claimed and not quiet",
            all(
                r["state"] == "claimed" and r["quiet_at"] is None for r in rows.values()
            ),
        )

        # 5. Dead band (refuse < free < resume): refusal and pause hold.
        p = await run.guard_pass("dead-band", DEAD)
        check("dead band keeps claims refused", p["claim_admission"] == "refused", p)
        check(
            "dead band keeps containers paused",
            run.state(heavy_c)["paused"] and run.state(light_c)["paused"],
        )

        # 6. Shed after the period: the heaviest writer by the basis available.
        io_before = {ref: e.io_before for ref, e in run.controller._paused.items()}
        run.mono[0] += SHED_AFTER_S  # controller clock only: leases stay valid
        await run.guard_pass("shed", LOW)
        shed = await run.events("disk.shed")
        states = {"heavy": run.state(heavy_c), "light": run.state(light_c)}
        after = {"heavy": wbytes(heavy_c.id), "light": wbytes(light_c.id)}
        ev.observed["shed"] = {
            "events": shed,
            "states": states,
            "io_before": {
                k: sum(v.wbytes_by_device.values()) for k, v in io_before.items()
            },
            "io_after": after,
        }
        check("exactly one disk.shed event", len(shed) == 1, shed)
        stopped = [n for n, s in states.items() if s["status"] == "exited"]
        if not check("exactly one container stopped", len(stopped) == 1, states):
            raise RuntimeError(f"shed stopped {stopped}")
        by_io = '"io.stat wbytes delta"' in shed[0]
        expected = "heavy" if by_io else "light"
        check(
            "victim matches the ranking basis (io.stat: heavy, size: light)",
            stopped == [expected],
            {"ranked_by_io": by_io, "stopped": stopped},
        )
        if io_readable:
            check("io.stat available, so the shed ranked by io.stat", by_io, shed[0])
        victim, survivor = (heavy, light) if stopped == ["heavy"] else (light, heavy)
        survivor_c, survivor_beat = (
            (light_c, light_beat) if survivor is light else (heavy_c, heavy_beat)
        )
        vrow = await run.row(victim.task_id)
        check(
            "shed task ready with failures unchanged",
            vrow["state"] == "ready" and vrow["failures"] == 0,
            dict(vrow),
        )
        check("survivor still paused", run.state(survivor_c)["paused"])

        # 7. Dead band after shed: survivor stays frozen (and did not write).
        await run.guard_pass("dead-band-2", DEAD)
        check("survivor paused in the dead band", run.state(survivor_c)["paused"])
        if survivor is light:
            check(
                "frozen container wrote nothing while paused",
                beat(light_beat) == frozen_beat,
                {"at_pause": frozen_beat, "now": beat(light_beat)},
            )

        # 8. Resume above the resume edge: thaw, clear, restart the quiet clock.
        p = await run.guard_pass("resume", OK)
        check("resume admits claims", p["claim_admission"] == "admitted", p)
        check(
            "survivor really unpaused",
            run.state(survivor_c) == {"status": "running", "paused": False},
            run.state(survivor_c),
        )
        srow = await run.row(survivor.task_id)
        check(
            "pause cleared, quiet clock restarted",
            srow["paused_disk_at"] is None
            and srow["quiet_at"] is None
            and srow["state"] == "claimed",
            dict(srow),
        )
        check("one disk.resume event", len(await run.events("disk.resume")) == 1)
        before = beat(survivor_beat)
        try:
            wait_until(
                lambda: beat(survivor_beat) != before, "survivor progress", WAIT_S
            )
            check(
                "survivor makes progress after resume",
                True,
                {"before": before, "after": beat(survivor_beat)},
            )
        except TimeoutError:
            check("survivor makes progress after resume", False, {"before": before})
        check(
            "victim stays stopped",
            run.state(heavy_c if victim is heavy else light_c)["status"] == "exited",
        )

        # 9. Warn hysteresis: held below clear, cleared above it, re-entered once.
        check("warn episode held between warn and clear", p["warn_episode"], p)
        p = await run.guard_pass("warn-clear", ABOVE_CLEAR)
        check("warn clears above clear", not p["warn_episode"], p)
        await run.guard_pass("warn-again", IN_WARN)
        check(
            "a new episode records a second disk.warn",
            len(await run.events("disk.warn")) == 2,
        )
    finally:
        await db.close()


async def _pool(db: Database) -> Any:
    from argus_api.fleet import service as fleet

    async with db.read() as conn:
        return next(p for p in await fleet.list_pools(conn) if p.name == POOL)


async def test_disk_guard_real_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    ev = Evidence(unverified=list(STILL_UNVERIFIED))
    verdict, why = gate(dict(os.environ))
    if verdict != "run":
        ev.outcome = "missing_prerequisites"
        ev.observed["missing"] = why
        ev.write(EVIDENCE)
        pytest.fail(
            "cannot run the real-runtime disk guard test; missing: " + "; ".join(why)
        )
    outer = os.environ.get("DOCKER_HOST")
    # The guard's DockerRootDir probe must see the harness socket, never DOCKER_HOST.
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    try:
        h = Harness.create(secrets.token_hex(8), outer_docker_host=outer)
    except (Refused, ValueError) as exc:  # nothing was created
        ev.outcome = "refused"
        ev.observed["refused"] = str(exc)
        ev.write(EVIDENCE)
        raise
    ev.observed["base"] = str(h.layout.base)
    problems: list[str] = []
    try:
        await scenario(h, ev)
    except Refused as exc:
        ev.outcome = "refused"
        ev.observed["refused"] = str(exc)
        raise
    except Exception as exc:
        ev.outcome = "error"
        ev.observed["error"] = repr(exc)
        raise
    finally:
        problems = h.teardown()
        ev.observed["teardown_problems"] = problems
        ev.finish()
        ev.write(EVIDENCE)
    assert not problems, problems
    assert ev.outcome == "verified", ev.failed()
