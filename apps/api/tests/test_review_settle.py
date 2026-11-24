"""An approved independent review settles a `review=none`, acceptance-off parent only
on the forge's word (docs/specs/settings.spec.md 3.5, task-deliverable.spec.md 3.5)."""

import json
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
from argus_api.tasks import deliverable, delivery, review_settle, service, steward
from argus_api.tasks import independent_review as indep
from argus_core.delivery import (
    CheckState,
    Delivery,
    DeliveryCheck,
    FactOrigin,
    MergeFact,
    PrState,
    ShaFact,
)
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


# -- PR steward (docs/specs/pr-steward.spec.md) --


def broken(
    clock: FakeClock,
    *,
    state: PrState = PrState.OPEN,
    checks: CheckState | None = CheckState.FAILURE,
    mergeable: bool | None = None,
    head: str = HEAD,
    partial: list[str] | None = None,
    age_s: float = 0,
) -> Delivery:
    d = seen(state, checks, clock, head=head, age_s=age_s)
    at = d.checked_at
    if checks is not None:
        if checks in (CheckState.ERROR, CheckState.CANCELLED):
            d.checks_combined = CheckState.FAILURE  # as combine_checks maps them
        d.checks = [
            DeliveryCheck(
                name="lint", state=checks, kind="check_run", sha=head, checked_at=at
            ),
            DeliveryCheck(
                name="test",
                state=CheckState.SUCCESS,
                kind="check_run",
                sha=head,
                checked_at=at,
            ),
        ]
    if mergeable is not None:
        d.merge = MergeFact(mergeable=mergeable, source=FactOrigin.FORGE, checked_at=at)
    d.partial = partial or []
    return d


async def configure(db: Database, max_repairs: int) -> None:
    async with db.write() as tx:
        await settings_svc.put_setting(
            tx,
            Path(),
            "global",
            settings_svc.REVIEW_KEY,
            {"mode": "different_model", "max_rounds": 3, "max_repairs": max_repairs},
            "human:test",
        )


async def waiting(
    db: Database, clock: FakeClock, forge_now: Forge, max_repairs: int = 0
) -> str:
    await configure(db, max_repairs)
    task_id = await parent(db, "pr_merged")
    await approve(db, task_id, read(seen(PrState.OPEN, CheckState.PENDING, clock)))
    forge_now.current = seen(PrState.OPEN, CheckState.PENDING, clock)
    return task_id


async def events(db: Database, task_id: str, kind: str) -> list[dict[str, Any]]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT data FROM events WHERE subject_id = ? AND kind = ? ORDER BY id",
            (task_id, kind),
        ) as cur,
    ):
        return [json.loads(r[0]) for r in await cur.fetchall()]


async def claim(tx: Any, task_id: str) -> None:
    """The repair attempt is running: the state its completion leaves."""
    now = tx.now.isoformat()
    await tx.conn.execute(
        "INSERT OR IGNORE INTO agents (id, name, role, instance_id, capabilities,"
        " state, last_heartbeat_at, created_at) VALUES ('w1', 'w1', 'worker', 'i1',"
        " '[]', 'active', ?, ?)",
        (now, now),
    )
    await tx.conn.execute(
        "UPDATE tasks SET state = 'claimed', assignee_agent_id = 'w1',"
        " attempt = attempt + 1, lease_expires_at = '2099-01-01T00:00:00Z'"
        " WHERE id = ?",
        (task_id,),
    )


async def sweep_one(db: Database, task_id: str) -> bool:
    return await review_settle.settle_task(db, None, task_id)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    "build",
    [
        lambda c: seen(PrState.OPEN, CheckState.PENDING, c),
        lambda c: seen(PrState.OPEN, None, c),
        lambda c: seen(PrState.OPEN, CheckState.UNKNOWN, c),
        lambda c: seen(PrState.UNKNOWN, None, c),
        lambda c: broken(c, partial=["checks"]),
        lambda c: broken(c, age_s=600),
    ],
    ids=["ci_running", "ci_missing", "ci_unknown", "unknown_pr", "partial", "stale"],
)
async def test_the_steward_keeps_waiting_on_a_pending_unread_or_partial_read(
    db: Database, clock: FakeClock, forge_now: Forge, build
) -> None:
    task_id = await waiting(db, clock, forge_now, max_repairs=2)
    forge_now.current = build(clock)
    await sweep_one(db, task_id)
    assert await outcome(db, task_id) == ("in_review", "awaiting_forge", 0)
    assert await events(db, task_id, steward.REQUESTED_EVENT) == []
    assert await events(db, task_id, steward.STOPPED_EVENT) == []


async def test_review_required_alone_keeps_waiting(
    db: Database, clock: FakeClock, forge_now: Forge
) -> None:
    task_id = await waiting(db, clock, forge_now, max_repairs=2)
    async with db.write() as tx:
        await settings_svc.put_setting(
            tx, Path(), "global", settings_svc.FORGE_AGENTIC_KEY, True, "human:test"
        )
    forge_now.current = seen(PrState.OPEN, CheckState.SUCCESS, clock)
    await sweep_one(db, task_id)
    assert await outcome(db, task_id) == ("in_review", "awaiting_forge", 0)
    assert await events(db, task_id, steward.STOPPED_EVENT) == []


async def test_a_ready_or_merged_pr_settles_as_before(
    db: Database, clock: FakeClock, forge_now: Forge
) -> None:
    task_id = await waiting(db, clock, forge_now, max_repairs=2)
    forge_now.current = seen(PrState.OPEN, CheckState.SUCCESS, clock)
    await sweep_one(db, task_id)  # ready: the merge resumes, no steward event
    assert await events(db, task_id, steward.REQUESTED_EVENT) == []
    assert await events(db, task_id, steward.STOPPED_EVENT) == []
    forge_now.current = seen(PrState.MERGED, None, clock)
    await sweep_one(db, task_id)
    assert await events(db, task_id, steward.REQUESTED_EVENT) == []


async def test_a_moved_head_is_voided_not_repaired(
    db: Database,
    clock: FakeClock,
    forge_now: Forge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[dict[str, Any]] = []

    async def spawn(tx, parent_row, **kwargs):
        spawned.append(kwargs)

    monkeypatch.setattr(indep, "spawn_reviewer", spawn)
    task_id = await waiting(db, clock, forge_now, max_repairs=2)
    forge_now.current = seen(PrState.MERGED, None, clock, head=MOVED)
    await sweep_one(db, task_id)
    assert [s["head_sha"] for s in spawned] == [MOVED]
    forge_now.current = broken(clock, head=MOVED)
    await sweep_one(db, task_id)
    assert await events(db, task_id, steward.REQUESTED_EVENT) == []


@pytest.mark.parametrize("max_repairs", [0, 2])
async def test_a_closed_pr_opens_one_decision_even_when_repairs_are_allowed(
    db: Database, clock: FakeClock, forge_now: Forge, max_repairs: int
) -> None:
    task_id = await waiting(db, clock, forge_now, max_repairs)
    forge_now.current = seen(PrState.CLOSED, None, clock)
    await sweep_one(db, task_id)
    await sweep_one(db, task_id)
    assert await outcome(db, task_id) == ("in_review", None, 1)
    (stopped,) = await events(db, task_id, steward.STOPPED_EVENT)
    assert stopped["class"] == "pr_closed"
    async with db.read() as conn:
        (decision_id,) = await decisions.open_ids(conn, "task", task_id, ("review",))
        decision = await decisions.get(conn, decision_id)
    assert decision_id == stopped["decision_id"]
    assert decision.payload == {
        "gate": "pr_not_ready",
        "class": "pr_closed",
        "pr": PR,
        "head_sha": HEAD,
        "failed_checks": [],
    }
    assert "pr_closed" in decision.question
    assert decision.options == ["accept", "accept_with_deviation", "reject"]


@pytest.mark.parametrize(
    ("mergeable", "checks", "classes"),
    [
        (None, CheckState.FAILURE, ["ci_failed"]),
        (None, CheckState.ERROR, ["ci_failed"]),
        (None, CheckState.CANCELLED, ["ci_failed"]),
        (False, CheckState.SUCCESS, ["not_mergeable"]),
        (False, CheckState.FAILURE, ["ci_failed", "not_mergeable"]),
    ],
)
async def test_a_broken_pr_opens_the_decision_when_repairs_are_off(
    db: Database,
    clock: FakeClock,
    forge_now: Forge,
    mergeable: bool | None,
    checks: CheckState,
    classes: list[str],
) -> None:
    task_id = await waiting(db, clock, forge_now, max_repairs=0)
    forge_now.current = broken(clock, checks=checks, mergeable=mergeable)
    await sweep_one(db, task_id)
    assert await outcome(db, task_id) == ("in_review", None, 1)
    (stopped,) = await events(db, task_id, steward.STOPPED_EVENT)
    assert stopped["class"] == classes[0]
    assert await events(db, task_id, steward.REQUESTED_EVENT) == []


async def test_the_decision_never_waives_the_independent_approval(
    db: Database, clock: FakeClock, forge_now: Forge
) -> None:
    task_id = await waiting(db, clock, forge_now)
    forge_now.current = seen(PrState.CLOSED, None, clock)
    await sweep_one(db, task_id)
    async with db.read() as conn:
        (decision_id,) = await decisions.open_ids(conn, "task", task_id, ("review",))
        assert await indep.reviewed_approval_of(conn, task_id) == (PR, HEAD)
        # accept still needs the forge to show the deliverable met
        with pytest.raises(Exception, match=r"(?i)merged|closed|pull request|forge"):
            deliverable.check_accept(
                await service.task_row(conn, task_id), read(forge_now.current)
            )
    assert decision_id


async def test_a_repair_is_requested_once_per_approval_and_capped_in_total(
    db: Database, clock: FakeClock, forge_now: Forge
) -> None:
    task_id = await waiting(db, clock, forge_now, max_repairs=2)
    forge_now.current = broken(clock, mergeable=False)
    await sweep_one(db, task_id)
    state, block, n = await outcome(db, task_id)
    assert (state, block, n) == ("ready", None, 0)
    (asked,) = await events(db, task_id, steward.REQUESTED_EVENT)
    assert asked["classes"] == ["ci_failed", "not_mergeable"]
    assert asked["failed_checks"] == ["lint"]
    assert asked["head_sha"] == HEAD
    async with db.read() as conn:
        spec = (await service.task_row(conn, task_id))["spec"]
        assert await steward.repaired_for_approval(conn, task_id)
    assert "## PR repair" in spec
    assert f"argus-task ci-digest --sha {HEAD}" in spec
    assert 'argus-task deliver --undo-note "<reason>"' in spec

    # The repair pushed nothing: the same head is not reviewed again and the second
    # repair of the same approval is refused, so the person decides.
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET state = 'in_review', block_reason = 'awaiting_forge'"
            " WHERE id = ?",
            (task_id,),
        )
    await sweep_one(db, task_id)
    assert await outcome(db, task_id) == ("in_review", None, 1)
    assert len(await events(db, task_id, steward.REQUESTED_EVENT)) == 1
    assert len(await events(db, task_id, steward.STOPPED_EVENT)) == 1


async def test_the_total_cap_holds_across_approvals(
    db: Database, clock: FakeClock, forge_now: Forge
) -> None:
    task_id = await waiting(db, clock, forge_now, max_repairs=1)
    forge_now.current = broken(clock)
    await sweep_one(db, task_id)
    assert len(await events(db, task_id, steward.REQUESTED_EVENT)) == 1
    # A later approval of a new head breaks again: the cap of one is spent.
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET state = 'in_review' WHERE id = ?", (task_id,)
        )
        child = {"id": "child2", "parent_id": task_id, "input": "{}"}
        await indep.on_review_child_complete(
            tx,
            child,  # ty: ignore[invalid-argument-type]
            TaskResult(summary="ok", data={"verdict": "approve", "head_sha": MOVED}),
            setting=SETTING,
            policy=POLICY,
            actor="agent:reviewer",
            forge=read(seen(PrState.OPEN, CheckState.PENDING, clock, head=MOVED)),
        )
    forge_now.current = broken(clock, head=MOVED)
    await sweep_one(db, task_id)
    assert len(await events(db, task_id, steward.REQUESTED_EVENT)) == 1
    assert await outcome(db, task_id) == ("in_review", None, 1)


async def test_a_pushed_repair_is_recorded_and_the_new_head_is_reviewed(
    db: Database,
    clock: FakeClock,
    forge_now: Forge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[dict[str, Any]] = []

    async def spawn(tx, parent_row, **kwargs):
        spawned.append(kwargs)

    task_id = await waiting(db, clock, forge_now, max_repairs=1)
    forge_now.current = broken(clock)
    await sweep_one(db, task_id)
    monkeypatch.setattr(indep, "spawn_reviewer", spawn)
    async with db.write() as tx:
        row = await service.task_row(tx.conn, task_id)
        await claim(tx, task_id)
        await indep.on_parent_complete(
            tx,
            await service.task_row(tx.conn, task_id),
            TaskResult(summary="repaired", data={"pr": PR, "head_sha": MOVED}),
            setting=SETTING,
            policy=POLICY,
            actor="agent:worker",
            forge=read(seen(PrState.OPEN, CheckState.PENDING, clock, head=MOVED)),
        )
        assert row
    assert await events(db, task_id, steward.PUSHED_EVENT) == [
        {"pr": PR, "old_head_sha": HEAD, "new_head_sha": MOVED}
    ]
    assert [s["head_sha"] for s in spawned] == [MOVED]
    async with db.read() as conn:
        assert await indep.review_rounds(conn, task_id) == 0


async def test_a_repair_that_pushes_nothing_settles_or_opens_the_decision(
    db: Database, clock: FakeClock, forge_now: Forge
) -> None:
    task_id = await waiting(db, clock, forge_now, max_repairs=1)
    forge_now.current = broken(clock)
    await sweep_one(db, task_id)
    async with db.write() as tx:
        await claim(tx, task_id)
        await indep.on_parent_complete(
            tx,
            await service.task_row(tx.conn, task_id),
            TaskResult(
                summary="flaky, nothing to change", data={"pr": PR, "head_sha": HEAD}
            ),
            setting=SETTING,
            policy=POLICY,
            actor="agent:worker",
            forge=read(broken(clock)),
        )
    assert await events(db, task_id, steward.PUSHED_EVENT) == []
    assert await outcome(db, task_id) == ("in_review", None, 1)
    (stopped,) = await events(db, task_id, steward.STOPPED_EVENT)
    assert stopped["class"] == "ci_failed"


async def test_a_head_that_moves_between_the_read_and_the_write_does_nothing(
    db: Database, clock: FakeClock, forge_now: Forge
) -> None:
    task_id = await waiting(db, clock, forge_now, max_repairs=2)

    class Racing(Forge):
        async def __call__(self, conn, checker, task):
            # The head is approved anew between the read and the write.
            async with db.write() as tx:
                await tx.conn.execute(
                    "INSERT INTO events (subject_type, subject_id, kind, data, at,"
                    " actor) VALUES ('task', ?, ?, ?, ?, 'test')",
                    (
                        task_id,
                        indep.PASSED_EVENT,
                        json.dumps({"pr": PR, "head_sha": MOVED}),
                        tx.now.isoformat(),
                    ),
                )
            return broken(clock)

    forge_now.current = None
    import argus_api.tasks.delivery as d

    d.delivery_of = Racing()  # restored by the forge_now monkeypatch teardown
    await sweep_one(db, task_id)
    assert await events(db, task_id, steward.REQUESTED_EVENT) == []
    assert await events(db, task_id, steward.STOPPED_EVENT) == []
