"""Editing a task's definition through the shared service (task edit spec)."""

import asyncio
import contextlib
import json

import pytest
from api_support import DONE, FakeClock, insert_agent
from argus_api.accounts import service as accounts
from argus_api.db import Database
from argus_api.errors import ConflictError, NotFoundError, ValidationFailedError
from argus_api.knowledge import service as kn
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import service as tasks
from argus_core.accounts import AccountCreate
from argus_core.knowledge import PageCreate, ProjectUpsert
from argus_core.task_edit import EDITABLE, EDITABLE_BY_STATE, TaskChanges
from argus_core.tasks import ExecutorModel, HarnessModel, TaskCreate
from argus_core.tasks import TaskState as S

A = "agent-a"
WHO = "admin:test"


@pytest.fixture(autouse=True)
async def agent(db: Database) -> None:
    await insert_agent(db, A)


async def create(db: Database, deps: list[str] | None = None, **kw):
    kw.setdefault("title", "t")
    async with db.write() as tx:
        return await tasks.create_task(
            tx,
            TaskCreate(project="global", depends_on=deps or [], **kw),
            WHO,
        )


async def get(db: Database, task_id: str):
    async with db.read() as conn:
        return await tasks.get_task(conn, task_id)


async def edit(db: Database, task_id: str, version: str | None = None, **changes):
    current = await get(db, task_id)
    async with db.write() as tx:
        return await tasks.edit_task(
            tx, task_id, TaskChanges(**changes), version or current.version, WHO
        )


async def events(db: Database, task_id: str, kind: str) -> list[dict]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT actor, at, data FROM events WHERE subject_id = ? AND kind = ?",
            (task_id, kind),
        ) as cur,
    ):
        return [
            {"actor": r[0], "at": r[1], "data": json.loads(r[2])}
            for r in await cur.fetchall()
        ]


async def claim(db: Database):
    async with db.write() as tx:
        return await tasks.claim(tx, A, [])


async def page(db: Database, project: str) -> str:
    async with db.write() as tx:
        with contextlib.suppress(Exception):  # the project may already exist
            await kn.upsert_project(tx, project, ProjectUpsert(name=project), WHO)
    async with db.write() as tx:
        return await kn.create_page(tx, PageCreate(title="p", project=project), WHO)


# -- patch semantics -----------------------------------------------------------------


async def test_omitted_fields_are_preserved_and_the_version_grows(
    db: Database,
) -> None:
    task = await create(
        db,
        spec="s",
        acceptance=["a"],
        priority=3,
        required_capabilities=["git"],
    )
    edited = await edit(db, task.id, title="new title")
    assert edited.title == "new title"
    assert (edited.spec, edited.acceptance, edited.priority) == ("s", ["a"], 3)
    assert edited.required_capabilities == ["git"]
    assert edited.version > task.version
    assert (await get(db, task.id)).version == edited.version


async def test_every_editable_field_changes_and_reads_back(db: Database) -> None:
    other = await create(db)
    pin = await page(db, "global")
    task = await create(db)
    edited = await edit(
        db,
        task.id,
        title="T2",
        spec="S2",
        acceptance=["x", "y"],
        priority=9,
        required_capabilities=["b", "a", "a"],
        knowledge=[pin, pin],
        depends_on=[other.id],
    )
    assert (edited.title, edited.spec, edited.acceptance) == ("T2", "S2", ["x", "y"])
    assert edited.priority == 9
    assert edited.required_capabilities == ["a", "b"]
    assert (edited.knowledge, edited.depends_on) == ([pin], [other.id])
    assert edited.state is S.PENDING  # the new dependency has not finished
    assert await get(db, task.id) == edited


async def test_null_clears_what_can_be_cleared_and_only_that(db: Database) -> None:
    dep = await create(db)
    pin = await page(db, "global")
    task = await create(
        db,
        [dep.id],
        spec="s",
        acceptance=["a"],
        required_capabilities=["c"],
        knowledge=[pin],
    )
    cleared = await edit(
        db,
        task.id,
        spec=None,
        acceptance=None,
        required_capabilities=None,
        knowledge=None,
        depends_on=None,
    )
    assert (cleared.spec, cleared.acceptance, cleared.required_capabilities) == (
        "",
        [],
        [],
    )
    assert (cleared.knowledge, cleared.depends_on) == ([], [])
    assert cleared.state is S.READY  # no dependency left to wait for
    for field in ("title", "priority"):
        with pytest.raises(ValidationFailedError) as exc:
            await edit(db, task.id, **{field: None})
        assert exc.value.details == [f"{field}: cannot be cleared"]


async def test_an_edit_that_changes_nothing_writes_nothing(db: Database) -> None:
    task = await create(db, spec="s")
    same = await edit(db, task.id, spec="s", priority=0)
    assert same.version == task.version
    assert await events(db, task.id, "task.edited") == []


async def test_an_empty_patch_is_refused(db: Database) -> None:
    task = await create(db)
    with pytest.raises(ValidationFailedError) as exc:
        await edit(db, task.id)
    assert exc.value.details == ["changes: nothing to change"]


# -- immutable fields ----------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "state",
        "status",
        "assignee_agent_id",
        "lease_expires_at",
        "attempt",
        "result",
        "project",
        "parent_id",
        "kind",
        "created_by",
        "nonsense",
    ],
)
async def test_immutable_fields_are_refused_with_one_structured_error(
    db: Database, field: str
) -> None:
    task = await create(db)
    with pytest.raises(ValidationFailedError) as exc:
        await edit(db, task.id, title="ok", **{field: "x"})
    assert exc.value.code == "immutable_field"
    assert exc.value.details == [field]
    assert await get(db, task.id) == task  # not even the valid title was applied


# -- atomic validation ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad", "detail_prefix"),
    [
        ({"title": ""}, "title:"),
        ({"title": "x" * 201}, "title:"),
        ({"spec": 5}, "spec:"),
        ({"acceptance": ["ok", 3]}, "acceptance."),
        ({"required_capabilities": [None]}, "required_capabilities."),
        ({"priority": "high"}, "priority:"),
    ],
)
async def test_an_invalid_value_rejects_the_whole_edit(
    db: Database, bad: dict, detail_prefix: str
) -> None:
    task = await create(db, spec="keep")
    try:
        changes = TaskChanges(**bad)
    except ValueError:
        return  # the typed patch model already refuses it before the service
    async with db.write() as tx:
        with pytest.raises(ValidationFailedError) as exc:
            await tasks.edit_task(
                tx,
                task.id,
                changes.model_copy(update={"priority": 7}),
                task.version,
                WHO,
            )
    assert exc.value.details[0].startswith(detail_prefix)
    after = await get(db, task.id)
    assert after == task and after.priority == 0


async def test_title_length_is_checked_by_the_service(db: Database) -> None:
    task = await create(db)
    for title in ("", "x" * 201):
        with pytest.raises(ValidationFailedError) as exc:
            await edit(db, task.id, title=title, priority=4)
        assert exc.value.details[0].startswith("title:")
    assert await get(db, task.id) == task


async def test_missing_and_foreign_pins_are_refused_without_a_partial_write(
    db: Database,
) -> None:
    mine = await create(db)
    task_pin = await page(db, "global")
    async with db.write() as tx:
        await kn.upsert_project(tx, "other", ProjectUpsert(name="other"), WHO)
    foreign = await page(db, "other")
    before = await get(db, mine.id)
    with pytest.raises(NotFoundError) as missing:
        await edit(db, mine.id, title="z", knowledge=[task_pin, "no-such-page"])
    assert missing.value.details == ["knowledge: no-such-page"]
    with pytest.raises(ValidationFailedError) as cross:
        await edit(db, mine.id, title="z", knowledge=[foreign])
    assert foreign in cross.value.details[0]
    assert "title" not in cross.value.message  # nothing about the other page leaks
    assert await get(db, mine.id) == before


async def test_dependency_rules_hold_and_nothing_is_written(db: Database) -> None:
    a = await create(db)
    b = await create(db, [a.id])
    before = await get(db, a.id)
    with pytest.raises(ValidationFailedError) as selfdep:
        await edit(db, a.id, title="z", depends_on=[a.id])
    assert selfdep.value.code == "dependency_cycle"
    with pytest.raises(ValidationFailedError) as cycle:
        await edit(db, a.id, title="z", depends_on=[b.id])
    assert cycle.value.code == "dependency_cycle"
    assert cycle.value.details == [a.id, b.id, a.id]
    with pytest.raises(NotFoundError) as missing:
        await edit(db, a.id, title="z", depends_on=["ghost"])
    assert missing.value.details == ["depends_on: ghost"]
    assert await get(db, a.id) == before


async def test_dependency_edits_move_ready_and_pending(db: Database) -> None:
    first = await create(db)
    second = await create(db)
    held = await edit(db, second.id, depends_on=[first.id])
    assert held.state is S.PENDING
    freed = await edit(db, second.id, depends_on=[])
    assert freed.state is S.READY
    approval = await create(db, requires_approval=True)
    assert (await edit(db, approval.id, depends_on=[first.id])).state is (
        S.AWAITING_APPROVAL
    )


# -- versions, concurrency and the edit/claim race -------------------------------------


async def test_a_stale_version_is_refused_with_the_current_one(db: Database) -> None:
    task = await create(db)
    fresh = await edit(db, task.id, title="one")
    with pytest.raises(ConflictError) as exc:
        await edit(db, task.id, task.version, title="two")
    assert exc.value.code == "stale_version"
    assert exc.value.details == [fresh.version]
    assert (await get(db, task.id)).title == "one"


async def test_two_edits_from_one_version_apply_exactly_one(
    db: Database, clock: FakeClock
) -> None:
    task = await create(db)  # the clock never moves: versions must still differ
    results = await asyncio.gather(
        edit(db, task.id, task.version, title="left"),
        edit(db, task.id, task.version, title="right"),
        return_exceptions=True,
    )
    wins = [r for r in results if not isinstance(r, Exception)]
    losses = [r for r in results if isinstance(r, ConflictError)]
    assert len(wins) == 1 and len(losses) == 1
    assert losses[0].code == "stale_version"
    assert (await get(db, task.id)).title == wins[0].title


async def test_edit_then_claim_hands_the_worker_the_edited_definition(
    db: Database,
) -> None:
    task = await create(db, spec="old")
    await edit(db, task.id, spec="new")
    claimed = await claim(db)
    assert claimed is not None and claimed.spec == "new"


async def test_claim_then_edit_is_refused_and_the_attempt_keeps_its_definition(
    db: Database,
) -> None:
    task = await create(db, spec="old")
    claimed = await claim(db)
    assert claimed is not None
    with pytest.raises(ConflictError) as stale:
        await edit(db, task.id, task.version, spec="new")
    assert stale.value.code == "stale_version"
    with pytest.raises(ConflictError) as blocked:
        await edit(db, task.id, spec="new")  # prepared against the claimed version
    assert blocked.value.code == "task_not_editable"
    assert blocked.value.details == [
        "state: claimed",
        "not editable: spec",
        "editable: priority",
    ]
    assert (await get(db, task.id)).spec == "old"


async def test_the_edit_claim_race_has_one_of_two_outcomes(db: Database) -> None:
    task = await create(db, spec="old")
    edited, claimed = await asyncio.gather(
        edit(db, task.id, task.version, spec="new"),
        claim(db),
        return_exceptions=True,
    )
    assert claimed is not None and not isinstance(claimed, Exception)
    if isinstance(edited, Exception):  # the claim won
        assert isinstance(edited, ConflictError)
        assert claimed.spec == "old"
        assert (await get(db, task.id)).spec == "old"
    else:  # the edit won, and the claim saw it
        assert claimed.spec == "new"


# -- per-state policy ------------------------------------------------------------------


async def in_state(db: Database, state: S):
    """A task in `state`, built through the real operations."""
    if state is S.AWAITING_APPROVAL:
        return await create(db, requires_approval=True)
    if state is S.PENDING:
        return await create(db, [(await create(db)).id])
    task = await create(db, review="human" if state is S.IN_REVIEW else "none")
    if state is S.READY:
        return task
    if state is S.CANCELLED:
        async with db.write() as tx:
            return await tasks.cancel(tx, task.id, WHO)
    claimed = await claim(db)
    assert claimed is not None and claimed.id == task.id
    if state is S.CLAIMED:
        return claimed
    async with db.write() as tx:
        if state is S.BLOCKED:
            return await tasks.block(tx, A, task.id, claimed.attempt, "why")
        if state in (S.SUCCEEDED, S.IN_REVIEW):
            return await tasks.complete(tx, A, task.id, claimed.attempt, DONE)
        assert state is S.FAILED
        for _ in range(3):
            await tasks.fail(tx, A, task.id, claimed.attempt, "boom", "session_lost")
            again = await tasks.claim(tx, A, [])
            if again is None:
                break
            claimed = again
        return await tasks.get_task(tx.conn, task.id)


@pytest.mark.parametrize("state", list(S))
async def test_each_state_permits_exactly_its_documented_fields(
    db: Database, state: S
) -> None:
    task = await in_state(db, state)
    assert task.state is state
    allowed = EDITABLE_BY_STATE[state]
    assert set(task.editable_fields) == allowed
    store = MemorySecretStore()  # the account a harness account pin names
    await store.put("pin", {"api_key": "k"})
    async with db.write() as tx:
        await accounts.create_account(
            tx,
            "acc-pin",
            AccountCreate(name="pin", provider="anthropic", secret_ref="sops:pin"),
            "sops:pin",
            WHO,
            store,
        )
    values = {
        "title": "T",
        "spec": "S",
        "acceptance": ["a"],
        "priority": 5,
        "required_capabilities": ["c"],
        "knowledge": [],
        "depends_on": [],
        "difficulty": "trivial",
        "account_providers": ["anthropic", "local"],
        "harness_model": HarnessModel(engine="claude", model="claude-opus-5-5"),
        "executor_model": ExecutorModel(executor="codex", model="gpt-6.1-sol"),
        "harness_account": "acc-pin",
    }
    assert set(values) == set(EDITABLE)
    for field, value in values.items():
        before = await get(db, task.id)
        if field in ("knowledge", "depends_on") and not getattr(before, field):
            value = [] if field == "knowledge" else [(await create(db)).id]
        try:
            after = await edit(db, task.id, **{field: value})
        except ConflictError as exc:
            assert field not in allowed
            assert exc.code == "task_not_editable"
            assert await get(db, task.id) == before
        else:
            assert field in allowed
            assert getattr(after, field) == value or field == "knowledge"


async def test_archived_tasks_are_read_only(db: Database) -> None:
    task = await in_state(db, S.CANCELLED)
    async with db.write() as tx:
        await tasks.archive(tx, task.id, WHO)
    archived = await get(db, task.id)
    assert archived.editable_fields == []
    with pytest.raises(ConflictError):
        await edit(db, task.id, priority=2)


# -- snapshots and audit ---------------------------------------------------------------


async def test_every_attempt_keeps_the_definition_it_was_handed(db: Database) -> None:
    task = await create(db, spec="v1", title="first")
    first = await claim(db)
    assert first is not None
    async with db.write() as tx:
        await tasks.block(tx, A, task.id, first.attempt, "stuck")
        await tasks.unblock(tx, task.id, WHO)
    await edit(db, task.id, title="second", spec="v2")
    second = await claim(db)
    assert second is not None and (second.attempt, second.spec) == (2, "v2")
    async with db.read() as conn:
        attempts = await tasks.attempt_definitions(conn, task.id)
    assert [
        (a.attempt, a.definition["title"], a.definition["spec"]) for a in attempts
    ] == [
        (1, "first", "v1"),
        (2, "second", "v2"),
    ]
    assert attempts[0].agent_id == A
    with pytest.raises(NotFoundError):
        async with db.read() as conn:
            await tasks.attempt_definitions(conn, "ghost")


async def test_the_audit_records_who_when_and_what_without_the_text(
    db: Database, clock: FakeClock
) -> None:
    dep = await create(db)
    task = await create(db, spec="old secret-token-123", acceptance=["one"])
    clock.advance(5)
    edited = await edit(
        db,
        task.id,
        title="renamed",
        spec="new secret-token-456",
        acceptance=["one", "two"],
        priority=4,
        required_capabilities=["git"],
        depends_on=[dep.id],
    )
    [event] = await events(db, task.id, "task.edited")
    assert event["actor"] == WHO
    assert event["data"]["from_version"] == task.version
    assert event["data"]["to_version"] == edited.version
    changes = event["data"]["changes"]
    assert set(changes) == {
        "title",
        "spec",
        "acceptance",
        "priority",
        "required_capabilities",
        "depends_on",
    }
    assert changes["title"] == {"from": "t", "to": "renamed"}
    assert changes["priority"] == {"from": 0, "to": 4}
    assert changes["depends_on"] == {"added": [dep.id], "removed": []}
    assert changes["spec"]["from_sha256"] != changes["spec"]["to_sha256"]
    assert changes["acceptance"]["to_size"] == 2
    dumped = json.dumps(event)
    assert "secret-token" not in dumped
    assert [e["data"] for e in await events(db, task.id, "task.dependencies_changed")]
