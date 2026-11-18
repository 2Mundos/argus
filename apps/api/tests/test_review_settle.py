"""An approved independent review settles a `review=none`, acceptance-off parent only
on the forge's word (docs/specs/settings.spec.md 3.5, task-deliverable.spec.md 3.5)."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from api_support import FakeClock
from argus_api import main as _main  # noqa: F401  (loads the packages in order)
from argus_api import model_policy
from argus_api.db import Database
from argus_api.decisions import store as decisions
from argus_api.settings import service as settings_svc
from argus_api.tasks import deliverable, delivery, review_settle, service
from argus_api.tasks import independent_review as indep
from argus_core.delivery import CheckState, Delivery, FactOrigin, PrState, ShaFact
from argus_core.settings import IndependentReviewSetting
from argus_core.tasks import TaskCreate, TaskResult

HEAD = "a" * 40
MOVED = "b" * 40
PR = "https://github.com/o/r/pull/7"
SETTING = IndependentReviewSetting(mode="different_model", max_rounds=3)
POLICY = model_policy.validate(
    {"harness": {"claude": ["a"]}, "implementation": {"claude": ["a"]}}
)
APPROVE = TaskResult(summary="ok", data={"verdict": "approve", "head_sha": HEAD})


def seen(
    state: PrState,
    checks: CheckState | None,
    clock: FakeClock,
    *,
    head: str = HEAD,
    age_s: float = 0,
) -> Delivery:
    at = clock.now - timedelta(seconds=age_s)
    return Delivery(
        pr=PR,
        pr_state=state,
        merged_at=at if state is PrState.MERGED else None,
        checked_at=at,
        flags=[],
        accept_decision_id=None,
        accept_blocked=None,
        head=ShaFact(sha=head, source=FactOrigin.FORGE, checked_at=at),
        checks_combined=checks,
    )


async def parent(
    db: Database, level: str, *, review: str = "none", acceptance: bool = False
) -> str:
    async with db.write() as tx:
        task = await service.create_task(
            tx, TaskCreate(project="demo", title=level), "human:test"
        )
        await tx.conn.execute(
            "UPDATE tasks SET state = 'in_review', deliverable = ?, review = ?"
            " WHERE id = ?",
            (level, review, task.id),
        )
        await settings_svc.put_setting(
            tx, Path(), "global", settings_svc.ACCEPTANCE_KEY, acceptance, "human:test"
        )
    return task.id


async def approve(
    db: Database, task_id: str, forge: deliverable.ForgeRead | None
) -> None:
    async with db.write() as tx:
        child = {"id": "child", "parent_id": task_id, "input": "{}"}
        await indep.on_review_child_complete(
            tx,
            child,  # ty: ignore[invalid-argument-type]
            APPROVE,
            setting=SETTING,
            policy=POLICY,
            actor="agent:reviewer",
            forge=forge,
        )


async def outcome(db: Database, task_id: str) -> tuple[str, str | None, int]:
    async with db.read() as conn:
        task = await service.get_task(conn, task_id)
        row = await service.task_row(conn, task_id)
        n = len(await decisions.open_ids(conn, "task", task_id, ("review",)))
    return task.state.value, row["block_reason"], n


def read(d: Delivery | None) -> deliverable.ForgeRead:
    return deliverable.ForgeRead(d)


@pytest.mark.parametrize(
    ("level", "state", "checks", "expected"),
    [
        ("pr_green", PrState.OPEN, CheckState.SUCCESS, ("succeeded", None, 0)),
        ("pr_green", PrState.MERGED, None, ("succeeded", None, 0)),
        ("pr_merged", PrState.MERGED, None, ("succeeded", None, 0)),
        (
            "pr_merged",
            PrState.OPEN,
            CheckState.SUCCESS,
            ("in_review", "awaiting_forge", 0),
        ),
        (
            "pr_green",
            PrState.OPEN,
            CheckState.FAILURE,
            ("in_review", "awaiting_forge", 0),
        ),
        (
            "pr_green",
            PrState.OPEN,
            CheckState.PENDING,
            ("in_review", "awaiting_forge", 0),
        ),
        ("pr_green", PrState.OPEN, None, ("in_review", "awaiting_forge", 0)),
        ("pr_green", PrState.UNKNOWN, None, ("in_review", "awaiting_forge", 0)),
        ("pr_green", PrState.CLOSED, None, ("in_review", "awaiting_forge", 0)),
    ],
)
async def test_the_forge_decides_after_an_approval(
    db: Database, clock: FakeClock, level: str, state: PrState, checks, expected
) -> None:
    task_id = await parent(db, level)
    await approve(db, task_id, read(seen(state, checks, clock)))
    assert await outcome(db, task_id) == expected


async def test_no_forge_evidence_waits_instead_of_succeeding(
    db: Database, clock: FakeClock
) -> None:
    for forge in (None, read(None)):
        task_id = await parent(db, "pr_merged")
        await approve(db, task_id, forge)
        assert await outcome(db, task_id) == ("in_review", "awaiting_forge", 0)


async def test_a_stale_forge_read_never_succeeds(
    db: Database, clock: FakeClock
) -> None:
    task_id = await parent(db, "pr_merged")
    old = seen(PrState.MERGED, None, clock, age_s=indep.FORGE_FRESH_S + 1)
    await approve(db, task_id, read(old))
    assert await outcome(db, task_id) == ("in_review", "awaiting_forge", 0)


async def test_a_moved_head_is_reviewed_again_not_accepted(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[dict[str, Any]] = []

    async def spawn(tx, parent_row, **kwargs):
        spawned.append(kwargs)

    monkeypatch.setattr(indep, "spawn_reviewer", spawn)
    task_id = await parent(db, "pr_merged")
    await approve(db, task_id, read(seen(PrState.MERGED, None, clock, head=MOVED)))
    assert await outcome(db, task_id) == ("in_review", None, 0)
    assert [s["head_sha"] for s in spawned] == [MOVED]


@pytest.mark.parametrize(
    ("review", "acceptance"),
    [("orchestrator", False), ("human", False), ("none", True)],
)
async def test_a_manual_review_or_acceptance_keeps_its_decision(
    db: Database, clock: FakeClock, review: str, acceptance: bool
) -> None:
    task_id = await parent(db, "pr_merged", review=review, acceptance=acceptance)
    await approve(db, task_id, read(seen(PrState.MERGED, None, clock)))
    assert await outcome(db, task_id) == ("in_review", None, 1)


async def test_a_replayed_approval_changes_nothing(
    db: Database, clock: FakeClock
) -> None:
    task_id = await parent(db, "pr_merged")
    forge = read(seen(PrState.MERGED, None, clock))
    await approve(db, task_id, forge)
    await approve(db, task_id, forge)
    assert await outcome(db, task_id) == ("succeeded", None, 0)
    waiting = await parent(db, "pr_merged")
    open_ = read(seen(PrState.OPEN, CheckState.SUCCESS, clock))
    await approve(db, waiting, open_)
    await approve(db, waiting, open_)
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT count(*) FROM events WHERE subject_id = ? AND kind = ?",
            (waiting, indep.WAITING_EVENT),
        ) as cur,
    ):
        assert (await cur.fetchone())[0] == 1


class Forge:
    """The forge as the recheck sees it: whatever `current` holds."""

    def __init__(self) -> None:
        self.current: Delivery | None = None

    async def __call__(self, conn, checker, task) -> Delivery | None:
        return self.current


@pytest.fixture
def forge_now(monkeypatch: pytest.MonkeyPatch) -> Forge:
    fake = Forge()
    monkeypatch.setattr(delivery, "delivery_of", fake)
    return fake


async def test_the_recheck_settles_when_the_forge_catches_up(
    db: Database, clock: FakeClock, forge_now: Forge
) -> None:
    task_id = await parent(db, "pr_merged")
    await approve(db, task_id, read(seen(PrState.OPEN, CheckState.SUCCESS, clock)))
    forge_now.current = seen(PrState.OPEN, CheckState.SUCCESS, clock)
    assert await review_settle.settle_task(db, None, task_id) is False  # ty: ignore[invalid-argument-type]
    assert await outcome(db, task_id) == ("in_review", "awaiting_forge", 0)
    forge_now.current = seen(PrState.MERGED, None, clock)
    assert await review_settle.settle_task(db, None, task_id) is True  # ty: ignore[invalid-argument-type]
    assert await outcome(db, task_id) == ("succeeded", None, 0)
    assert await review_settle.settle_task(db, None, task_id) is False  # ty: ignore[invalid-argument-type]


async def test_the_recheck_never_settles_a_moved_or_unavailable_forge(
    db: Database, clock: FakeClock, forge_now: Forge
) -> None:
    task_id = await parent(db, "pr_merged")
    await approve(db, task_id, None)
    forge_now.current = None
    await review_settle.settle_task(db, None, task_id)  # ty: ignore[invalid-argument-type]
    assert await outcome(db, task_id) == ("in_review", "awaiting_forge", 0)
    forge_now.current = seen(PrState.MERGED, None, clock, head=MOVED)
    await review_settle.settle_task(db, None, task_id)  # ty: ignore[invalid-argument-type]
    state, _, _ = await outcome(db, task_id)
    assert state != "succeeded"


async def test_the_recheck_hands_over_when_acceptance_is_switched_on(
    db: Database, clock: FakeClock, forge_now: Forge
) -> None:
    task_id = await parent(db, "pr_merged")
    await approve(db, task_id, None)
    async with db.write() as tx:
        await settings_svc.put_setting(
            tx, Path(), "global", settings_svc.ACCEPTANCE_KEY, True, "human:test"
        )
    forge_now.current = seen(PrState.MERGED, None, clock)
    assert await review_settle.settle_task(db, None, task_id) is False  # ty: ignore[invalid-argument-type]
    assert await outcome(db, task_id) == ("in_review", None, 1)


async def test_only_a_parent_without_manual_review_settles_alone(db: Database) -> None:
    task_id = await parent(db, "pr_merged")
    async with db.read() as conn:
        row = await service.task_row(conn, task_id)
        assert await indep.settles_alone(conn, row)
    manual = await parent(db, "pr_merged", review="human")
    async with db.read() as conn:
        row = await service.task_row(conn, manual)
        assert not await indep.settles_alone(conn, row)


async def test_startup_repair_leaves_a_waiting_parent_alone(
    db: Database, clock: FakeClock
) -> None:
    task_id = await parent(db, "pr_merged")
    await approve(db, task_id, None)
    async with db.write() as tx:
        assert await indep.backfill_manual_reviews(tx) == []
    assert await outcome(db, task_id) == ("in_review", "awaiting_forge", 0)


async def test_the_reviewer_completion_reads_the_parent_forge_only_to_approve(
    db: Database, clock: FakeClock, forge_now: Forge
) -> None:
    forge_now.current = seen(PrState.MERGED, None, clock)
    task_id = await parent(db, "pr_merged")
    manual = await parent(db, "pr_merged", review="human")
    kids = {}
    for name, parent_id in (("settles", task_id), ("manual", manual)):
        async with db.write() as tx:
            kid = await service.create_task(
                tx, TaskCreate(project="demo", title=name), "human:test"
            )
            await tx.conn.execute(
                "UPDATE tasks SET kind = ?, parent_id = ? WHERE id = ?",
                (indep.CODE_REVIEW_KIND, parent_id, kid.id),
            )
        kids[name] = kid.id
    changes = TaskResult(summary="x", data={"verdict": "request_changes"})
    for name, result, got in (
        ("settles", APPROVE, True),
        ("settles", changes, False),
        ("manual", APPROVE, False),
    ):
        read_ = await indep.completion_forge_read(
            db,
            None,
            kids[name],
            result,
            None,  # ty: ignore[invalid-argument-type]
        )
        assert (read_ is not None) is got, (name, result.data)
