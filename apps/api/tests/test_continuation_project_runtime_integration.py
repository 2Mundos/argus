"""Continuation contract + project runtime images on the same SQLite journey.

Simulated transport only: no Gitea, browser, or live Docker build in this module
(the real builder is covered in test_runtime_docker.py). Exercises claim gating,
runtime holds that do not spend attempts, effective continuation after resume,
and isolation between projects/tasks.
"""

import pytest
from api_support import FakeClock, insert_agent
from argus_api.auth import Identity
from argus_api.config import Settings
from argus_api.db import Database
from argus_api.decisions import service as decisions
from argus_api.errors import ConflictError
from argus_api.runtime import service as runtime_service
from argus_api.runtime.service import RuntimeManager
from argus_api.tasks import service as tasks
from argus_core.auth import Scope
from argus_core.continuation import ContinuationPhase
from argus_core.decisions import Ask, DecisionAnswer
from argus_core.tasks import TaskCreate
from argus_core.tasks import TaskState as S
from test_project_runtime import (
    ARGUS,
    BASE_REF,
    RADAR,
    A,
    B,
    FakeDocker,
    builds,
    create,
    ready_for,
    req,
    task_row,
)

ORCH_ID = Identity("t-orch", Scope.ORCHESTRATOR, "orch", None)


@pytest.fixture
def docker() -> FakeDocker:
    return FakeDocker()


@pytest.fixture
def manager(db: Database, docker: FakeDocker, tmp_path) -> RuntimeManager:
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
        await runtime_service.put_spec(tx, "radar", RADAR, "human:test")
        await runtime_service.put_spec(tx, "argus", ARGUS, "human:test")


async def claim(db: Database, agent: str = A):
    async with db.write() as tx:
        return await tasks.claim(tx, agent, [])


async def seed_radar_qa_continuation(db: Database, clock: FakeClock):
    """Merged PR + pending browser QA on project radar (continuation FL4 shape)."""
    async with db.write() as tx:
        await tasks.create_task(
            tx,
            TaskCreate(
                project="radar",
                title="Radar QA resume",
                spec="Ship radar.\n\n## Acceptance\nBrowser QA desktop/mobile.",
                acceptance=["browser QA desktop/mobile done"],
                # Highest priority so reclaim beats sibling radar tasks (claim orders
                # priority DESC, then created_at, then id — ties are not stable).
                priority=10,
            ),
            "human:test",
        )
    t = await claim(db)
    async with db.write() as tx:
        asked = await decisions.ask(
            tx,
            A,
            t.id,
            Ask(
                attempt=t.attempt,
                question="May I run browser QA on a disposable stack?",
            ),
        )
        await decisions.answer(
            tx,
            asked.id,
            DecisionAnswer(text="Yes on a disposable stack only."),
            ORCH_ID,
        )
    t = await claim(db)
    clock.advance(11)
    async with db.write() as tx:
        await tasks.report_progress(
            tx,
            A,
            t.id,
            t.attempt,
            "pr_opened",
            kind="pr_opened",
            data={"number": 42, "url": "https://git.example/radar/pulls/42"},
        )
    from argus_core.continuation import ObservationRequest

    async with db.write() as tx:
        t = await tasks.observe(
            tx,
            A,
            t.id,
            ObservationRequest(
                attempt=t.attempt,
                artifacts=[{"kind": "pr", "ref": "42", "state": "merged"}],
            ),
        )
    async with db.write() as tx:
        await tasks.release(tx, t.id, "human:test", "seed")
    return t


async def test_radar_continuation_waits_on_runtime_without_spending_attempts(
    db: Database, manager: RuntimeManager, clock: FakeClock
) -> None:
    t = await seed_radar_qa_continuation(db, clock)
    assert t.continuation.phase is ContinuationPhase.QA

    # Scheduler: priority DESC, created_at, id. Sibling radar and argus both stay
    # ready while this host builds radar; without explicit priority, equal defaults
    # let agent B take the older radar sibling instead of argus (flaky).
    other = await create(db, "radar", priority=0)
    await create(db, "argus", priority=5)

    first = await claim(db, A)
    assert first.id == t.id
    held = await manager.prepare(A, req(first))
    assert held.task_held and held.state == "preparing"
    row = await task_row(db, t.id)
    assert row["failures"] == 0 and row["attempt"] == first.attempt

    # Same host is building radar: sibling radar tasks stay ready; another project may claim.
    assert (await task_row(db, other.id))["state"] == S.READY.value
    while_building = await claim(db, A)
    assert while_building is not None and while_building.project == "argus"
    async with db.write() as tx:
        await tasks.release(tx, while_building.id, "human:test", "test")

    assert await manager.work_once()
    resumed = await claim(db, A)
    assert resumed is not None and resumed.id == t.id
    sel = await manager.prepare(A, req(resumed))
    assert sel.state == "ready" and sel.image is not None
    assert "radar" in sel.image.ref

    c = resumed.continuation
    assert c.phase is ContinuationPhase.QA and c.pending
    qa = next(d for d in c.decisions if "browser QA" in d.question)
    assert qa.status == "effective"
    with pytest.raises(ConflictError) as err:
        async with db.write() as tx:
            await decisions.ask(
                tx,
                A,
                t.id,
                Ask(
                    attempt=resumed.attempt,
                    question="May I run browser QA on a disposable stack?",
                ),
            )
    assert err.value.code == "already_answered"

    # Argus project keeps its own runtime queue independent of radar's build.
    argus_claim = await claim(db, B)
    assert argus_claim is not None and argus_claim.project == "argus"
    assert len(await builds(db, "radar")) >= 1


async def test_prepare_rejects_wrong_base_identity(
    db: Database, manager: RuntimeManager
) -> None:
    t, _ = await ready_for(db, manager, "radar", A)
    sel = await manager.prepare(A, req(t, base_id="sha256:" + "f" * 64))
    assert sel.state == "failed" and sel.task_held
    assert "on the worker" in (sel.reason or "")
    row = await task_row(db, t.id)
    assert row["failures"] == 0


async def test_runtime_hold_does_not_increment_failures(
    db: Database, manager: RuntimeManager
) -> None:
    t = await create(db, "radar")
    claimed = await claim(db)
    async with db.write() as tx:
        await tasks.hold_for_runtime(
            tx,
            A,
            claimed.id,
            claimed.attempt,
            "runtime_preparing",
            None,
        )
    row = await task_row(db, t.id)
    assert row["block_reason"] == "runtime_preparing"
    assert row["failures"] == 0
