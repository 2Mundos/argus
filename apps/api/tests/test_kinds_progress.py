from datetime import timedelta

import pytest
from api_support import DONE, FakeClock, insert_agent
from argus_api.db import Database
from argus_api.errors import (
    LeaseConflictError,
    NotFoundError,
    RateLimitedError,
    ValidationFailedError,
)
from argus_api.kinds import service as kinds
from argus_api.tasks import service as tasks
from argus_core.kinds import TaskKindUpsert
from argus_core.tasks import Review, TaskCreate, TaskResult

A = "agent-a"

REPORT = TaskKindUpsert(
    description="Write a report",
    input_schema={
        "type": "object",
        "required": ["topic"],
        "properties": {"topic": {"type": "string"}},
    },
    result_schema={
        "type": "object",
        "required": ["word_count"],
        "properties": {"word_count": {"type": "integer", "minimum": 1}},
    },
    default_capabilities=["writing"],
    default_lease_ttl_s=600,
    default_max_attempts=5,
    default_review=Review.HUMAN,
)


@pytest.fixture(autouse=True)
async def setup(db: Database) -> None:
    await insert_agent(db, A)
    async with db.write() as tx:
        await kinds.upsert_kind(tx, "report", REPORT, "admin:test")


async def create(db: Database, **kw):
    async with db.write() as tx:
        return await tasks.create_task(
            tx, TaskCreate(project="global", title="t", **kw), "admin:test"
        )


async def claim(db: Database, caps: list[str] | None = None):
    async with db.write() as tx:
        task = await tasks.claim(tx, A, caps or [])
    assert task is not None
    return task


# -- kinds ------------------------------------------------------------------------


async def test_generic_kind_exists_and_unknown_kinds_are_rejected(
    db: Database,
) -> None:
    assert (await create(db)).kind == "generic"
    with pytest.raises(NotFoundError) as err:
        await create(db, kind="nope")
    assert err.value.code == "unknown_kind"


async def test_kind_defaults_apply_only_to_unset_fields(db: Database) -> None:
    task = await create(db, kind="report", input={"topic": "x"})
    assert task.required_capabilities == ["writing"]
    assert (task.lease_ttl_s, task.max_attempts, task.review) == (600, 5, Review.HUMAN)
    explicit = await create(
        db, kind="report", input={"topic": "x"}, lease_ttl_s=60, review=Review.NONE
    )
    assert (explicit.lease_ttl_s, explicit.review) == (60, Review.NONE)
    assert explicit.max_attempts == 5


async def test_input_is_validated_with_every_error_listed(db: Database) -> None:
    with pytest.raises(ValidationFailedError) as err:
        await create(db, kind="report", input={"topic": 3})
    assert err.value.details == ["topic: 3 is not of type 'string'"]


async def test_invalid_schema_is_rejected(db: Database) -> None:
    bad = TaskKindUpsert(input_schema={"type": "not-a-type"})
    with pytest.raises(ValidationFailedError) as err:
        async with db.write() as tx:
            await kinds.upsert_kind(tx, "bad", bad, "admin:test")
    assert err.value.code == "invalid_schema"


async def test_result_data_is_validated_on_complete(db: Database) -> None:
    await create(db, kind="report", input={"topic": "x"})
    task = await claim(db, ["writing"])
    with pytest.raises(ValidationFailedError) as err:
        async with db.write() as tx:
            await tasks.complete(
                tx, A, task.id, task.attempt, TaskResult(summary="s", data={})
            )
    assert err.value.details == ["<root>: 'word_count' is a required property"]
    async with db.write() as tx:
        done = await tasks.complete(
            tx,
            A,
            task.id,
            task.attempt,
            TaskResult(summary="s", data={"word_count": 10}),
        )
    assert done.result is not None
    assert done.result.data == {"word_count": 10}


async def test_unknown_artifact_in_result_is_rejected(db: Database) -> None:
    await create(db)
    task = await claim(db)
    with pytest.raises(ValidationFailedError) as err:
        async with db.write() as tx:
            await tasks.complete(
                tx, A, task.id, task.attempt, TaskResult(summary="s", artifacts=["x"])
            )
    assert err.value.details == ["x"]


# -- progress ---------------------------------------------------------------------


async def progress(db: Database, task, message: str = "working", percent=None):
    async with db.write() as tx:
        return await tasks.report_progress(
            tx, A, task.id, task.attempt, message, percent
        )


async def test_progress_is_recorded_and_renews_the_lease(
    db: Database, clock: FakeClock
) -> None:
    await create(db, lease_ttl_s=60)
    task = await claim(db)
    clock.advance(50)
    after = await progress(db, task, "half way", 50)
    assert (after.progress_message, after.progress_percent) == ("half way", 50)
    assert after.lease_expires_at == clock.now + timedelta(seconds=60)


async def test_progress_is_rate_limited(db: Database, clock: FakeClock) -> None:
    await create(db)
    task = await claim(db)
    await progress(db, task)
    clock.advance(5)
    with pytest.raises(RateLimitedError):
        await progress(db, task)
    clock.advance(5)
    await progress(db, task)


async def test_progress_requires_the_lease(db: Database, clock: FakeClock) -> None:
    await create(db, lease_ttl_s=60)
    task = await claim(db)
    clock.advance(60)
    with pytest.raises(LeaseConflictError):
        await progress(db, task)


async def test_completion_without_progress_still_works(db: Database) -> None:
    await create(db)
    task = await claim(db)
    async with db.write() as tx:
        assert (await tasks.complete(tx, A, task.id, task.attempt, DONE)).result
