import json
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
            tx, TaskCreate(project="demo", title="t", **kw), "admin:test"
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
    assert err.value.details == [
        "<root>: 'word_count' is a required property",
        "expected result.data to be an object with required keys word_count;"
        " allowed keys word_count",
    ]
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


REVIEW_DATA = {"verdict": "approve", "findings": [], "head_sha": "abcdef1"}


async def complete_review(db: Database, result: TaskResult):
    await create(db, kind="code_review", input={"task_id": "t1", "head_sha": "abcdef1"})
    task = await claim(db)
    async with db.write() as tx:
        return await tasks.complete(tx, A, task.id, task.attempt, result)


async def test_code_review_top_level_envelope_is_moved_into_data(db: Database) -> None:
    done = await complete_review(db, TaskResult(summary="ok", envelope=REVIEW_DATA))
    assert done.result is not None
    assert done.result.data == REVIEW_DATA
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT data FROM events WHERE kind = 'review.result_normalised'"
        ) as cur,
    ):
        (data,) = await cur.fetchone()
    assert json.loads(data) == {"keys": sorted(REVIEW_DATA), "from": "envelope"}


async def test_code_review_envelope_names_missing_and_extra_keys(db: Database) -> None:
    with pytest.raises(ValidationFailedError) as err:
        await complete_review(
            db,
            TaskResult(summary="ok", envelope={"verdict": "approve", "findings": []}),
        )
    assert "<root>: 'head_sha' is a required property" in err.value.details
    assert err.value.details[-1] == (
        "expected result.data to be an object with required keys findings, head_sha,"
        " verdict; allowed keys findings, fixed_head_sha, head_sha, tests, verdict and no"
        " other keys"
    )
    with pytest.raises(ValidationFailedError) as extra:
        await complete_review(
            db, TaskResult(summary="ok", envelope={**REVIEW_DATA, "notes": "x"})
        )
    assert extra.value.details == [
        "top level: unexpected key notes; the review envelope takes only verdict,"
        " findings, head_sha, fixed_head_sha and tests"
    ]


async def test_envelope_is_ignored_for_other_kinds(db: Database) -> None:
    await create(db, kind="report", input={"topic": "x"})
    task = await claim(db, ["writing"])
    with pytest.raises(ValidationFailedError) as err:
        async with db.write() as tx:
            await tasks.complete(
                tx,
                A,
                task.id,
                task.attempt,
                TaskResult(summary="s", envelope={"word_count": 10}),
            )
    assert err.value.details[0] == "<root>: None is not of type 'object'"


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


async def test_progress_percent_is_kept_until_a_new_one_is_reported(
    db: Database, clock: FakeClock
) -> None:
    await create(db)
    task = await claim(db)
    await progress(db, task, "half way", 50)
    clock.advance(10)
    after = await progress(db, task, "still working")
    assert after.progress_percent == 50
    clock.advance(1)
    after = await milestone(db, task, "commit_created", COMMIT)
    assert after.progress_percent == 50
    clock.advance(10)
    after = await progress(db, task, "finishing", 90)
    assert after.progress_percent == 90


@pytest.mark.parametrize(
    "message",
    [
        "progress pr_opened: PR #207 ready",
        "see {braces} here",
        '{"kind": oops',
        "[1, 2]",
    ],
)
async def test_non_object_messages_are_stored_unchanged(
    db: Database, message: str
) -> None:
    await create(db)
    task = await claim(db)
    assert (await progress(db, task, message)).progress_message == message


async def test_json_object_message_is_rendered_and_data_is_untouched(
    db: Database, clock: FakeClock
) -> None:
    await create(db)
    task = await claim(db)
    url = "https://git.example.com/o/r/pulls/159"
    raw = json.dumps({"kind": "pr_opened", "number": 159, "url": url})
    after = await progress(db, task, raw)
    assert after.progress_message == f"PR opened: #159 {url}"
    assert after.progress_data is None
    clock.advance(10)
    other = json.dumps({"kind": "ci", "state": "red", "tries": 2, "x": {"a": 1}})
    assert (
        await progress(db, task, other)
    ).progress_message == "ci: state red, tries 2"
    clock.advance(10)
    data = {"number": 159, "url": url}
    after = await milestone(db, task, "pr_opened", data, json.dumps(data))
    assert after.progress_message == f"PR opened: #159 {url}"
    assert after.progress_data is not None
    assert after.progress_data.pop("at")
    assert after.progress_data == data


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


# -- milestones (progress-milestones spec) ----------------------------------------


async def milestone(db: Database, task, kind, data=None, message: str = "m"):
    async with db.write() as tx:
        return await tasks.report_progress(
            tx, A, task.id, task.attempt, message, None, kind, data
        )


COMMIT = {"branch": "feat/x", "sha": "abc1234"}
WAITING = {"on": "ci", "ref": "#412", "since": "2026-11-02T10:00:00.000Z"}


@pytest.mark.parametrize(
    ("kind", "data"), [("commit_created", COMMIT), ("waiting", WAITING)]
)
async def test_milestone_is_recorded_with_server_time(
    db: Database, clock: FakeClock, kind, data
) -> None:
    await create(db)
    task = await claim(db)
    after = await milestone(db, task, kind, data)
    assert after.progress_kind == kind
    assert after.progress_data is not None
    assert after.progress_data.pop("at")
    assert after.progress_data == data


async def test_legacy_task_has_no_milestone(db: Database) -> None:
    await create(db)
    task = await claim(db)
    after = await progress(db, task, "plain note")
    assert (after.progress_kind, after.progress_data) == (None, None)


async def test_milestone_with_a_different_kind_skips_the_rate_limit(
    db: Database, clock: FakeClock
) -> None:
    await create(db)
    task = await claim(db)
    await progress(db, task)
    clock.advance(1)
    await milestone(db, task, "commit_created", COMMIT)
    clock.advance(1)
    pr = {"number": 7, "url": "https://git.example.com/o/r/pulls/7"}
    after = await milestone(db, task, "pr_opened", pr)
    assert after.progress_kind == "pr_opened"
    clock.advance(1)
    with pytest.raises(RateLimitedError):  # same kind again inside 10 s
        await milestone(db, task, "pr_opened", pr)


async def test_free_text_note_keeps_the_milestone_and_is_limited(
    db: Database, clock: FakeClock
) -> None:
    await create(db)
    task = await claim(db)
    await milestone(db, task, "commit_created", COMMIT)
    clock.advance(1)
    with pytest.raises(RateLimitedError):
        await progress(db, task, "chatter")
    clock.advance(10)
    after = await progress(db, task, "chatter")
    assert after.progress_message == "chatter"
    assert after.progress_kind == "commit_created"


@pytest.mark.parametrize(
    ("kind", "data"),
    [
        ("deployed", {}),
        ("commit_created", {"branch": "b", "sha": "nothex!"}),
        ("commit_created", {"branch": "b"}),
        ("commit_created", {**COMMIT, "extra": 1}),
        ("pr_opened", {"number": 1, "url": "javascript:alert(1)"}),
        ("pr_opened", {"number": 1, "url": "https://u:p@git.example.com/pulls/1"}),
        ("pr_opened", {"number": 1, "url": "https://git.example.com/pulls/1?token=x"}),
        ("pr_opened", {"number": 0, "url": "https://git.example.com/pulls/1"}),
        ("ci_failed", {}),
        ("waiting_input", {"x": 1}),
        ("looping", {"tool": "Bash"}),
        ("looping", {"tool": "a b", "count": 8}),
        ("looping", {"tool": "Bash", "count": 0}),
        ("looping", {"tool": "Bash", "count": 8, "note": "x"}),
        ("waiting", {**WAITING, "on": "deploy"}),
        ("waiting", {**WAITING, "since": "yesterday"}),
        ("waiting", {"on": "ci", "ref": "#1"}),
        (None, {"sha": "abc1234"}),
    ],
)
async def test_invalid_milestones_are_refused(db: Database, kind, data) -> None:
    await create(db)
    task = await claim(db)
    with pytest.raises(ValidationFailedError):
        await milestone(db, task, kind, data)


async def test_looping_milestone_is_accepted(db: Database) -> None:
    await create(db)
    task = await claim(db)
    after = await milestone(db, task, "looping", {"tool": "Bash", "count": 8})
    assert after.progress_kind == "looping"


async def test_retry_clears_the_milestone(db: Database) -> None:
    await create(db, max_attempts=1)
    task = await claim(db)
    await milestone(db, task, "waiting_input")
    async with db.write() as tx:
        await tasks.fail(tx, A, task.id, task.attempt, "boom")
    async with db.write() as tx:
        after = await tasks.retry(tx, task.id, "user:me")
    assert (after.progress_kind, after.progress_data) == (None, None)


async def test_a_rejected_reply_keeps_its_details_on_the_failed_attempt(
    db: Database,
) -> None:
    bad = {
        "verdict": "request_changes",
        "head_sha": "abcdef1",
        "findings": [{"priority": "P2", "title": "no description"}],
    }
    with pytest.raises(ValidationFailedError) as err:
        await complete_review(db, TaskResult(summary="ok", data=bad))
    path = next(d for d in err.value.details if "description" in d)
    assert path.startswith("findings/0:")
    async with db.read() as conn, conn.execute("SELECT id, attempt FROM tasks") as cur:
        row = await cur.fetchone()
    async with db.write() as tx:
        failed = await tasks.fail(
            tx,
            A,
            row["id"],
            row["attempt"],
            "The worker's reply could not be delivered (422 validation_failed)",
            "channel_error",
            details=err.value.details,
        )
    assert failed.error is not None
    assert path in failed.error
    async with db.read() as conn:
        async with conn.execute(
            "SELECT data FROM events WHERE kind = 'task.fail'"
        ) as cur:
            (data,) = await cur.fetchone()
        async with conn.execute("SELECT line FROM task_log") as cur:
            lines = [r["line"] for r in await cur.fetchall()]
    assert path in json.loads(data)["details"]
    assert f"[reply rejected] {path}" in lines
