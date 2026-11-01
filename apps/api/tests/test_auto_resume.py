"""The automatic resume rule for confirmed exits (failure-classes.spec.md 3.2)."""

import asyncio
import json
from collections.abc import Generator
from pathlib import Path

import pytest
from api_support import FakeClock
from argus_api.agents import service as agents
from argus_api.agents.service import LOST_AFTER_S
from argus_api.db import Database
from argus_api.errors import LeaseConflictError
from argus_api.main import app
from argus_api.tasks import log as task_log
from argus_api.tasks import service as tasks
from argus_core.agents import AgentRegister, BootstrapTokenCreate
from argus_core.tasks import LeaseObservation, TaskCreate
from fastapi.testclient import TestClient

GONE = LeaseObservation(workspace="ok", session="gone", delegate="silent")
RUNNING = LeaseObservation(workspace="ok", session="running", delegate="silent")
# Synthetic marker standing in for anything sensitive in the exit's text.
MARKER = "SYNTHETIC-EXIT-MARKER-51c2"


# -- helpers -----------------------------------------------------------------------


async def worker(db: Database) -> str:
    async with db.write() as tx:
        issued = await agents.create_bootstrap_token(
            tx, BootstrapTokenCreate(), "human:test"
        )
    async with db.write() as tx:
        reg = await agents.register(tx, AgentRegister(bootstrap_token=issued.token))
    return reg.agent.id


async def new_task(db: Database, title: str = "crashy", **kw) -> str:
    kw.setdefault("max_attempts", 10)
    kw.setdefault("lease_ttl_s", 3600)
    async with db.write() as tx:
        task = await tasks.create_task(
            tx, TaskCreate(project="demo", title=title, **kw), "human:test"
        )
    return task.id


async def claim(db: Database, agent_id: str, task_id: str) -> int:
    async with db.write() as tx:
        task = await tasks.claim(tx, agent_id, [])
    assert task is not None and task.id == task_id
    return task.attempt


async def lose(db: Database, agent_id: str, task_id: str, attempt: int):
    async with db.write() as tx:
        return await tasks.fail(
            tx,
            agent_id,
            task_id,
            attempt,
            f"session container exited (code 137); last output: {MARKER}",
            reason="session_lost",
        )


async def progress(db: Database, agent_id: str, task_id: str, attempt: int, **kw):
    async with db.write() as tx:
        await tasks.report_progress(tx, agent_id, task_id, attempt, "working", **kw)


async def events(db: Database, task_id: str, kind: str) -> list[tuple[str, dict]]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT actor, data FROM events WHERE subject_id = ? AND kind = ?"
            " ORDER BY id",
            (task_id, kind),
        ) as cur,
    ):
        return [(r["actor"], json.loads(r["data"])) for r in await cur.fetchall()]


async def state(db: Database, task_id: str) -> str:
    async with db.read() as conn:
        return (await tasks.get_task(conn, task_id)).state


async def resumed_once(db: Database, clock: FakeClock) -> tuple[str, str, int]:
    """A task past its first confirmed exit, claimed again: (agent, task, attempt)."""
    agent = await worker(db)
    task_id = await new_task(db)
    attempt = await claim(db, agent, task_id)
    clock.advance(30)
    await lose(db, agent, task_id, attempt)
    assert await state(db, task_id) == "ready"
    clock.advance(5)
    return agent, task_id, await claim(db, agent, task_id)


# -- the rule ----------------------------------------------------------------------


async def test_crash_loop_resumes_once_then_fails_with_the_cause(
    db: Database, clock: FakeClock
) -> None:
    agent, task_id, attempt = await resumed_once(db, clock)
    assert await events(db, task_id, "task.auto_resumed") == [
        (f"agent:{agent}", {"attempt": 1, "reason": "session_lost"})
    ]
    clock.advance(30)
    task = await lose(db, agent, task_id, attempt)
    assert task.state == "failed"  # 8 attempts left, but no progress since
    assert task.attempt == 2 and task.failures == 2
    assert MARKER in (task.error or "")
    fails = await events(db, task_id, "task.fail")
    assert fails[0][1].get("auto_resume") is None
    assert fails[1][1]["auto_resume"] == "no_progress"
    assert fails[1][1]["reason"] == "session_lost"
    assert len(await events(db, task_id, "task.auto_resumed")) == 1


@pytest.mark.parametrize(
    "kw",
    [{}, {"kind": "commit_created", "data": {"branch": "b", "sha": "abc1234"}}],
    ids=["note", "milestone"],
)
async def test_progress_of_the_attempt_allows_another_resume(
    db: Database, clock: FakeClock, kw: dict
) -> None:
    agent, task_id, attempt = await resumed_once(db, clock)
    await progress(db, agent, task_id, attempt, **kw)
    clock.advance(30)
    assert (await lose(db, agent, task_id, attempt)).state == "ready"
    assert [d for _, d in await events(db, task_id, "task.auto_resumed")] == [
        {"attempt": 1, "reason": "session_lost"},
        {"attempt": 2, "reason": "session_lost"},
    ]
    # the third attempt starts over: no progress of its own, so it fails
    clock.advance(5)
    attempt = await claim(db, agent, task_id)
    assert (await lose(db, agent, task_id, attempt)).state == "failed"


async def test_signals_that_are_not_progress_do_not_count(
    db: Database, clock: FakeClock
) -> None:
    """Old-attempt progress, heartbeats (activity, observation), live log lines and
    another task's progress leave the second exit terminal."""
    agent = await worker(db)
    task_id = await new_task(db)
    attempt = await claim(db, agent, task_id)
    await progress(db, agent, task_id, attempt)  # attempt 1: before its own exit
    clock.advance(30)
    await lose(db, agent, task_id, attempt)
    other_agent = await worker(db)
    other = await new_task(db, title="other")
    clock.advance(5)
    attempt = await claim(db, agent, task_id)
    assert await claim(db, other_agent, other) == 1
    async with db.write() as tx:
        await agents.heartbeat(tx, agent, [(task_id, attempt, clock.now, RUNNING)])
    async with db.write() as tx:
        await task_log.append(tx, agent, task_id, attempt, "worker", ["line"])
    await progress(db, other_agent, other, 1)
    clock.advance(30)
    assert (await lose(db, agent, task_id, attempt)).state == "failed"


async def test_an_earlier_attempts_progress_after_the_resume_does_not_count(
    db: Database, clock: FakeClock
) -> None:
    """Attempt 2 progresses, then its lease lapses (requeue at once); attempt 3
    exits with nothing of its own."""
    agent, task_id, attempt = await resumed_once(db, clock)
    await progress(db, agent, task_id, attempt)
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET lease_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", task_id),
        )
        assert await tasks.expire_leases(tx) == [task_id]
    clock.advance(5)
    attempt = await claim(db, agent, task_id)
    assert attempt == 3
    assert (await lose(db, agent, task_id, attempt)).state == "failed"


async def test_max_attempts_still_caps_the_first_exit(db: Database) -> None:
    agent = await worker(db)
    task_id = await new_task(db, max_attempts=1)
    task = await lose(db, agent, task_id, await claim(db, agent, task_id))
    assert task.state == "failed"
    assert await events(db, task_id, "task.auto_resumed") == []
    assert (await events(db, task_id, "task.fail"))[0][1].get("auto_resume") is None


async def test_the_error_is_capped_and_the_event_has_no_text(db: Database) -> None:
    agent = await worker(db)
    task_id = await new_task(db)
    attempt = await claim(db, agent, task_id)
    async with db.write() as tx:
        task = await tasks.fail(
            tx, agent, task_id, attempt, MARKER + "x" * 5000, reason="session_lost"
        )
    assert task.error is not None and len(task.error) == tasks.EXIT_ERROR_MAX
    [(_, data)] = await events(db, task_id, "task.auto_resumed")
    assert MARKER not in json.dumps(data)


@pytest.mark.parametrize("char", ["é", "€", "😀"], ids=["2-byte", "3-byte", "4-byte"])
async def test_the_error_cap_is_bytes_and_never_splits_a_character(
    db: Database, char: str
) -> None:
    agent = await worker(db)
    task_id = await new_task(db)
    attempt = await claim(db, agent, task_id)
    error = "x" + char * 3000  # the boundary falls inside a character for each width
    async with db.write() as tx:
        task = await tasks.fail(
            tx, agent, task_id, attempt, error, reason="session_lost"
        )
    assert task.error is not None
    size = len(task.error.encode())
    assert tasks.EXIT_ERROR_MAX - len(char.encode()) < size <= tasks.EXIT_ERROR_MAX
    assert error.startswith(task.error)
    [(_, data)] = await events(db, task_id, "task.auto_resumed")
    assert MARKER not in json.dumps(data)


# -- recovery epoch ----------------------------------------------------------------


async def exhausted(db: Database, clock: FakeClock) -> tuple[str, str]:
    agent, task_id, attempt = await resumed_once(db, clock)
    await lose(db, agent, task_id, attempt)
    assert await state(db, task_id) == "failed"
    return agent, task_id


async def test_retry_starts_a_new_epoch(db: Database, clock: FakeClock) -> None:
    agent, task_id = await exhausted(db, clock)
    async with db.write() as tx:
        await tasks.retry(tx, task_id, "human:test")
    clock.advance(5)
    attempt = await claim(db, agent, task_id)
    assert attempt == 1  # retry resets the attempt numbers
    assert (await lose(db, agent, task_id, attempt)).state == "ready"
    assert len(await events(db, task_id, "task.auto_resumed")) == 2


@pytest.mark.parametrize(
    ("actor", "after"), [("human:test", "ready"), ("system", "failed")]
)
async def test_only_an_operator_unblock_starts_a_new_epoch(
    db: Database, clock: FakeClock, actor: str, after: str
) -> None:
    """A quota wake or runtime build (`system`) must not reset a crash loop."""
    agent, task_id, attempt = await resumed_once(db, clock)
    async with db.write() as tx:
        await tasks.block(tx, agent, task_id, attempt, "env_workspace_missing")
    async with db.write() as tx:
        await tasks.unblock(tx, task_id, actor)
    clock.advance(5)
    attempt = await claim(db, agent, task_id)
    assert (await lose(db, agent, task_id, attempt)).state == after


# -- unchanged paths ---------------------------------------------------------------


@pytest.mark.parametrize("reason", ["lease_expired", "timeout", "stalled"])
async def test_unconfirmed_exits_keep_requeue_at_once(
    db: Database, clock: FakeClock, reason: str
) -> None:
    agent = await worker(db)
    task_id = await new_task(
        db, lease_ttl_s=3600 if reason != "lease_expired" else 60, timeout_s=600
    )
    attempt = await claim(db, agent, task_id)
    await lose(db, agent, task_id, attempt)  # first confirmed exit: resumed
    for _ in range(2):  # no progress, yet each one requeues
        clock.advance(5)
        await claim(db, agent, task_id)
        clock.advance(601)
        async with db.write() as tx:
            if reason == "lease_expired":
                expired = await tasks.expire_leases(tx)
            elif reason == "timeout":
                expired = await tasks.expire_timeouts(tx)
            else:
                await tasks.flag_quiet(tx, 300)
                expired = await tasks.expire_stalled(tx, 300)
        assert expired == [task_id]
        assert await state(db, task_id) == "ready"
    expires = await events(db, task_id, "task.expire")
    assert [d["reason"] for _, d in expires] == [reason, reason]
    assert all("auto_resume" not in d for _, d in expires)
    assert len(await events(db, task_id, "task.auto_resumed")) == 1


async def test_quota_fallback_spends_nothing_and_does_not_reset(
    db: Database, clock: FakeClock
) -> None:
    agent, task_id, attempt = await resumed_once(db, clock)
    async with db.write() as tx:
        task = await tasks.fail(tx, agent, task_id, attempt, "limit", reason="quota")
    assert (task.state, task.failures) == ("ready", 1)
    assert len(await events(db, task_id, "task.auto_resumed")) == 1
    clock.advance(5)
    attempt = await claim(db, agent, task_id)
    assert (await lose(db, agent, task_id, attempt)).state == "failed"


async def test_environment_block_is_unchanged(db: Database, clock: FakeClock) -> None:
    agent, task_id, attempt = await resumed_once(db, clock)
    async with db.write() as tx:
        task = await tasks.block(
            tx, agent, task_id, attempt, "env_workspace_missing", "no checkout"
        )
    assert (task.state, task.block_reason, task.failures) == (
        "blocked",
        "env_workspace_missing",
        1,
    )
    assert len(await events(db, task_id, "task.auto_resumed")) == 1


# -- the reaper: agent_ended -------------------------------------------------------


# Heartbeats 30 s apart that saw the session gone for the whole 90 s grace.
STABLY_GONE = (GONE,) * 4


async def end_agent(db: Database, clock: FakeClock, task_id: str, *beats) -> str:
    """A fresh agent claims, heartbeats `beats` 30 s apart (an observation, or
    None for a renewal without one), then goes silent and is lost."""
    agent = await worker(db)
    attempt = await claim(db, agent, task_id)
    for obs in beats:
        clock.advance(30)
        async with db.write() as tx:
            await agents.heartbeat(tx, agent, [(task_id, attempt, None, obs)])
    clock.advance(LOST_AFTER_S + 1)
    async with db.write() as tx:
        await agents.mark_lost(tx)
    return agent


async def test_agent_ended_with_the_session_gone_follows_the_rule(
    db: Database, clock: FakeClock
) -> None:
    task_id = await new_task(db)
    first = await end_agent(db, clock, task_id, *STABLY_GONE)
    assert await state(db, task_id) == "ready"
    assert await events(db, task_id, "task.auto_resumed") == [
        ("system", {"attempt": 1, "reason": "agent_ended"})
    ]
    # the lost agent's late fail is fenced: nothing more is recorded
    with pytest.raises(LeaseConflictError):
        await lose(db, first, task_id, 1)
    await end_agent(db, clock, task_id, *STABLY_GONE)
    assert await state(db, task_id) == "failed"
    expires = await events(db, task_id, "task.expire")
    assert expires[-1][1]["auto_resume"] == "no_progress"
    assert len(await events(db, task_id, "task.auto_resumed")) == 1


@pytest.mark.parametrize(
    "beats",
    [
        (None,),
        (RUNNING,),
        (GONE,),  # one listing is not a confirmation
        (GONE, GONE, GONE),  # 60 s, short of the grace
        (GONE, GONE, RUNNING, GONE, GONE, GONE),  # a restart breaks the run
        (GONE, GONE, None, GONE, GONE, GONE),  # so does a renewal without one
        (*STABLY_GONE, RUNNING),  # the last observation is what counts
    ],
    ids=[
        "unobserved",
        "running",
        "gone-once",
        "gone-short",
        "restart-gap",
        "unobserved-gap",
        "running-last",
    ],
)
async def test_agent_ended_without_a_stable_gone_session_is_not_confirmed(
    db: Database, clock: FakeClock, beats
) -> None:
    task_id = await new_task(db)
    for _ in range(3):
        await end_agent(db, clock, task_id, *beats)
        assert await state(db, task_id) == "ready"
    assert await events(db, task_id, "task.auto_resumed") == []


async def test_an_observation_from_before_the_claim_does_not_confirm(
    db: Database, clock: FakeClock
) -> None:
    task_id = await new_task(db)
    await end_agent(db, clock, task_id, *STABLY_GONE)  # resumed
    agent = await worker(db)
    clock.advance(1)
    attempt = await claim(db, agent, task_id)
    # a stale GONE run written for this attempt but dated before its claim
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET observation = ?, observation_at = ?, session_gone_at = ?"
            " WHERE id = ?",
            (
                GONE.model_dump_json(),
                "2000-01-01T00:05:00+00:00",
                "2000-01-01T00:00:00+00:00",
                task_id,
            ),
        )
    clock.advance(LOST_AFTER_S + 1)
    async with db.write() as tx:
        await agents.mark_lost(tx)
    assert await state(db, task_id) == "ready"  # requeue at once, not the rule
    assert attempt == 2 and len(await events(db, task_id, "task.auto_resumed")) == 1


# -- fencing -----------------------------------------------------------------------


async def test_concurrent_fails_record_one_resume(
    db: Database, clock: FakeClock
) -> None:
    agent = await worker(db)
    task_id = await new_task(db)
    attempt = await claim(db, agent, task_id)
    results = await asyncio.gather(
        *(lose(db, agent, task_id, attempt) for _ in range(4)),
        return_exceptions=True,
    )
    conflicts = [r for r in results if isinstance(r, LeaseConflictError)]
    assert len(conflicts) == 3 and {c.code for c in conflicts} == {"not_claimed"}
    assert len(await events(db, task_id, "task.auto_resumed")) == 1
    assert len(await events(db, task_id, "task.fail")) == 1


async def test_a_stale_holder_is_rejected(db: Database, clock: FakeClock) -> None:
    old = await worker(db)
    task_id = await new_task(db, lease_ttl_s=60)
    attempt = await claim(db, old, task_id)
    clock.advance(61)
    async with db.write() as tx:
        await tasks.expire_leases(tx)
    new = await worker(db)
    assert await claim(db, new, task_id) == 2
    for who, at in ((old, attempt), (old, 2), (new, attempt)):
        with pytest.raises(LeaseConflictError):
            await lose(db, who, task_id, at)
    assert await events(db, task_id, "task.auto_resumed") == []
    assert await state(db, task_id) == "claimed"


# -- query plans -------------------------------------------------------------------


async def plan(db: Database, sql: str, params: tuple) -> list[str]:
    async with (
        db.read() as conn,
        conn.execute("EXPLAIN QUERY PLAN " + sql, params) as cur,
    ):
        return [r["detail"] for r in await cur.fetchall()]


QUERIES = [
    (tasks.LATEST_TASK_EVENT_SQL, ("t", "task.claim", 0, 1)),
    (tasks.LATEST_TASK_EVENT_SQL, ("t", "task.auto_resumed", 5, 0)),
    (tasks.ATTEMPT_PROGRESS_SQL, ("t", 5, 2)),
]


@pytest.mark.parametrize(("sql", "params"), QUERIES)
async def test_rule_reads_are_one_index_seek(db: Database, sql: str, params) -> None:
    lines = await plan(db, sql, params)
    assert len(lines) == 1, lines
    assert lines[0].startswith("SEARCH events USING"), lines
    assert "INDEX events_task_kind (subject_id=? AND kind=? AND id>?)" in lines[0]


async def test_plan_guard_notices_a_missing_index(db: Database) -> None:
    async with db.write() as tx:
        await tx.conn.execute("DROP INDEX events_task_kind")
    lines = await plan(db, *QUERIES[0])
    assert "events_task_kind" not in " ".join(lines)


# -- end to end over HTTP ----------------------------------------------------------

ADMIN_TOKEN = "test-admin-token-" + "r" * 32


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        c.put("/api/projects/alpha", json={"name": "alpha"})
        yield c


def test_crash_loop_over_the_api(client: TestClient) -> None:
    """The route the channel calls: resume, refuse a repeat, then fail on no progress;
    a milestone in between earns one more resume."""
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    h = {"Authorization": f"Bearer {reg.json()['token']}"}
    created = client.post(
        "/api/tasks",
        json={"title": "t", "project": "alpha", "spec": "s", "max_attempts": 10},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]

    def exit_(attempt: int):
        return client.post(
            f"/api/tasks/{task_id}/fail",
            json={
                "attempt": attempt,
                "error": f"session container exited (code 1); last output: {MARKER}",
                "reason": "session_lost",
            },
            headers=h,
        )

    def claim_() -> int:
        claimed = client.post("/api/tasks/claim", json={}, headers=h).json()
        assert claimed["id"] == task_id
        return claimed["attempt"]

    attempt = claim_()
    first = exit_(attempt)
    assert first.status_code == 200 and first.json()["state"] == "ready"
    assert exit_(attempt).status_code == 409  # repeated: recorded nothing
    attempt = claim_()
    milestone = client.post(
        f"/api/tasks/{task_id}/progress",
        json={
            "attempt": attempt,
            "message": "ci",
            "kind": "ci_running",
            "data": {"sha": "abc1234"},
        },
        headers=h,
    )
    assert milestone.status_code == 200, milestone.text
    assert exit_(attempt).json()["state"] == "ready"
    attempt = claim_()
    last = exit_(attempt)
    assert last.json()["state"] == "failed" and MARKER in last.json()["error"]
    feed = client.get("/api/events", params={"subject_id": task_id}).json()
    items = feed["items"] if isinstance(feed, dict) else feed
    resumed = [e for e in items if e["kind"] == "task.auto_resumed"]
    assert sorted(e["data"]["attempt"] for e in resumed) == [1, 2]
    assert all(MARKER not in json.dumps(e["data"]) for e in resumed)


@pytest.mark.parametrize("release", ["decision", "runtime_release"])
def test_operator_release_starts_a_new_epoch_over_the_api(
    client: TestClient, release: str
) -> None:
    """An answered decision and a manager's runtime rebuild are operator unblocks:
    after one the crash loop is bounded again from zero, not still exhausted."""
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    h = {"Authorization": f"Bearer {reg.json()['token']}"}
    task_id = client.post(
        "/api/tasks",
        json={"title": "t", "project": "alpha", "spec": "s", "max_attempts": 10},
    ).json()["id"]

    def claim_() -> int:
        claimed = client.post("/api/tasks/claim", json={}, headers=h).json()
        assert claimed["id"] == task_id
        return claimed["attempt"]

    def exit_(attempt: int) -> str:
        r = client.post(
            f"/api/tasks/{task_id}/fail",
            json={"attempt": attempt, "error": "exited", "reason": "session_lost"},
            headers=h,
        )
        assert r.status_code == 200, r.text
        return r.json()["state"]

    assert exit_(claim_()) == "ready"  # the one resume of this epoch
    attempt = claim_()
    if release == "decision":
        ask = client.post(
            f"/api/tasks/{task_id}/ask",
            json={"attempt": attempt, "question": "which way?"},
            headers=h,
        )
        assert ask.status_code == 201, ask.text
        answered = client.post(
            f"/api/decisions/{ask.json()['id']}/answer", json={"text": "this way"}
        )
        assert answered.status_code == 200, answered.text
    else:
        held = client.post(
            "/api/runtime/hold",
            json={"task_id": task_id, "attempt": attempt, "error": "no docker"},
            headers=h,
        )
        assert held.status_code == 204, held.text
        assert client.get(f"/api/tasks/{task_id}").json()["state"] == "blocked"
        rebuilt = client.post("/api/projects/alpha/runtime/rebuild")
        assert rebuilt.status_code == 200, rebuilt.text
    assert client.get(f"/api/tasks/{task_id}").json()["state"] == "ready"
    # Without the new epoch this exit would fail the task (no progress since the
    # resume); with it, the task resumes again.
    assert exit_(claim_()) == "ready"
