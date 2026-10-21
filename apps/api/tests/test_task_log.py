"""Task live log (docs/specs/task-live-log.spec.md)."""

import asyncio
import json
from collections.abc import Generator
from pathlib import Path

import pytest
from api_support import FakeClock, insert_agent
from argus_api import mcp_server
from argus_api.db import Database
from argus_api.errors import (
    LeaseConflictError,
    RateLimitedError,
    TooLargeError,
)
from argus_api.eventlog.routes import event_stream, wait
from argus_api.main import app
from argus_api.tasks import log
from argus_api.tasks import service as tasks
from argus_core.tasks import TaskCreate
from fastapi.testclient import TestClient

A, B = "agent-a", "agent-b"
ADMIN_TOKEN = "test-admin-token-" + "l" * 32


async def claim(db: Database, agent: str = A):
    async with db.write() as tx:
        task = await tasks.claim(tx, agent, [])
    assert task is not None
    return task


async def create(db: Database, **kw):
    async with db.write() as tx:
        return await tasks.create_task(
            tx, TaskCreate(project="global", title="t", **kw), "admin:test"
        )


async def post(db: Database, task, lines, *, agent=A, source="worker", attempt=None):
    async with db.write() as tx:
        return await log.append(
            tx, agent, task.id, attempt or task.attempt, source, log.prepare(lines)
        )


async def read(db: Database, task_id: str, **kw):
    kw = {"attempt": None, "after": None, "limit": 1000} | kw
    async with db.read() as conn:
        return await log.read(conn, task_id, **kw)


@pytest.fixture
async def claimed(db: Database):
    await insert_agent(db, A)
    await insert_agent(db, B)
    await create(db, max_attempts=6)
    return await claim(db)


# -- writing -------------------------------------------------------------------------


async def test_batch_is_stored_and_updates_output_at(
    db: Database, claimed, clock: FakeClock
) -> None:
    accepted = await post(db, claimed, ["one", "two"])
    assert (accepted.accepted, accepted.last_seq) == (2, 2)
    page = await read(db, claimed.id)
    assert [(ln.seq, ln.source, ln.line) for ln in page.lines] == [
        (1, "worker", "one"),
        (2, "worker", "two"),
    ]
    assert page.attempt == 1 and page.last_seq == 2
    async with db.read() as conn:
        task = await tasks.get_task(conn, claimed.id)
    assert task.output_at == clock.now
    assert task.progress_at is None


async def test_only_the_live_lease_of_the_current_attempt_may_write(
    db: Database, claimed, clock: FakeClock
) -> None:
    with pytest.raises(LeaseConflictError) as err:
        await post(db, claimed, ["x"], agent=B)
    assert err.value.code == "not_assignee"
    with pytest.raises(LeaseConflictError) as err:
        await post(db, claimed, ["x"], attempt=2)
    assert err.value.code == "stale_attempt"
    clock.advance(claimed.lease_ttl_s + 1)
    with pytest.raises(LeaseConflictError) as err:
        await post(db, claimed, ["x"])
    assert err.value.code == "lease_expired"
    assert (await read(db, claimed.id)).lines == []


async def test_old_attempt_cannot_write_into_the_new_one(
    db: Database, claimed, clock: FakeClock
) -> None:
    await post(db, claimed, ["first attempt"])
    async with db.write() as tx:
        await tasks.expire_leases(tx)  # not expired yet: nothing
    clock.advance(claimed.lease_ttl_s + 1)
    async with db.write() as tx:
        await tasks.expire_leases(tx)
    second = await claim(db, B)
    assert second.attempt == 2
    with pytest.raises(LeaseConflictError) as err:
        await post(db, claimed, ["late"], attempt=1)  # agent A, attempt 1
    assert err.value.code == "not_assignee"
    with pytest.raises(LeaseConflictError) as err:
        await post(db, second, ["late"], agent=B, attempt=1)
    assert err.value.code == "stale_attempt"
    await post(db, second, ["second attempt"], agent=B)
    assert [ln.line for ln in (await read(db, second.id)).lines] == ["second attempt"]
    old = await read(db, second.id, attempt=1)
    assert [ln.line for ln in old.lines] == ["first attempt"]
    assert old.lines[0].seq < (await read(db, second.id)).lines[0].seq


async def test_limits(db: Database, claimed, clock: FakeClock) -> None:
    long = "x" * (log.MAX_LINE_CHARS + 500)
    await post(db, claimed, [long])
    [line] = (await read(db, claimed.id)).lines
    assert len(line.line) == log.MAX_LINE_CHARS and line.line.endswith("…")

    clock.advance(2)
    huge = ["y" * 1000] * (log.MAX_BATCH_BYTES // 1000 + 1)
    with pytest.raises(TooLargeError) as err:
        await post(db, claimed, huge)
    assert err.value.code == "log_batch_too_large"
    assert len((await read(db, claimed.id)).lines) == 1


async def test_rate_limit_is_per_source(
    db: Database, claimed, clock: FakeClock
) -> None:
    await post(db, claimed, ["a"])
    await post(db, claimed, ["delegate line"], source="delegate")
    with pytest.raises(RateLimitedError):
        await post(db, claimed, ["too soon"])
    with pytest.raises(RateLimitedError):
        await post(db, claimed, ["too soon"], source="delegate")
    clock.advance(log.MIN_INTERVAL_S)
    await post(db, claimed, ["b"])
    for _ in range(3):  # a steady batch every 2 s keeps working
        clock.advance(2)
        await post(db, claimed, ["c"])
    assert len((await read(db, claimed.id)).lines) == 6


async def test_secrets_are_redacted_before_storing_and_publishing(
    db: Database, claimed
) -> None:
    stream = event_stream(db, after=None, keepalive=5)
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.05)
    await post(
        db, claimed, ["GITEA_TOKEN=abc123xyz", "Authorization: Bearer abcdefgh1234"]
    )
    event = json.loads((await asyncio.wait_for(pending, 2)).split("data: ")[1])
    assert event["data"]["lines"] == [
        "GITEA_TOKEN=[redacted]",
        "Authorization: [redacted]",
    ]
    stored = [ln.line for ln in (await read(db, claimed.id)).lines]
    assert stored == event["data"]["lines"]
    await stream.aclose()


async def test_text_is_cleaned(db: Database, claimed, clock: FakeClock) -> None:
    await post(db, claimed, ["\x1b[31mred\x1b[0m\r", "", "a\nb", "   "])
    assert [ln.line for ln in (await read(db, claimed.id)).lines] == ["red", "a", "b"]
    clock.advance(2)
    again = await post(db, claimed, ["", " "])  # nothing left: not a sign of life
    assert again.accepted == 0


def test_a_bare_carriage_return_splits_lines() -> None:
    assert log.clean(["50%\r100%\r\ndone", "a\rb\r"], ()) == [
        "50%",
        "100%",
        "done",
        "a",
        "b",
    ]


def test_a_bare_carriage_return_never_splits_a_secret() -> None:
    head, tail = "ghp_" + "a" * 20, "b" * 16
    [line] = log.clean([f"push {head}\r{tail} done"], ())
    assert "aaaa" not in line and "bbbb" not in line and "[redacted]" in line
    # a CR used as a space: redacted before the split
    assert log.clean(["Authorization: Bearer\rabcdefgh12345"], ()) == [
        "Authorization: [redacted]"
    ]
    assert log.clean(["curl -H 'Bearer\rabcdefgh12345'"], ()) == [
        "curl -H 'Bearer",
        "[redacted]'",
    ]
    # an ordinary progress line still splits, even where joining would hide a token
    assert log.clean([f"10%\r{head}"], ()) == ["10%", "[redacted]"]
    assert log.clean([f"abc\r{head}"], ()) == ["abc", "[redacted]"]


def test_escapes_are_stripped_before_redacting() -> None:
    token = "ghp_" + "a" * 36
    split = "ghp_\x1b[0m" + "a" * 36
    assert log.clean([f"pushing with {split}", f"\x1b[1m{token}\x1b[0m"], ()) == [
        "pushing with [redacted]",
        "[redacted]",
    ]
    # Idempotent: a line that is already clean comes out the same.
    once = log.clean(["Authorization: Bearer abcdefgh12345 and " + split], ())
    assert log.clean(once, ()) == once


def test_long_lines_are_cut_but_never_unmask_a_secret() -> None:
    token = "ghp_" + "b" * 36
    near_cut = "x" * (log.MAX_LINE_CHARS - 10) + " " + token
    [line] = log.clean([near_cut], ())
    assert "bbbbbbbbbb" not in line and len(line) <= log.MAX_LINE_CHARS
    # A key block whose lines are longer than the pre-cut is still masked whole.
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        + "M" * (log.REDACT_LINE_CHARS * 2)
        + "\n-----END PRIVATE KEY-----"
    )
    assert log.clean(["a", pem, "b"], ()) == ["a", "[redacted private key]", "b"]


@pytest.mark.parametrize(
    "line",
    ["a." * 32_000, "a-" * 32_000, "--" + "a-" * 32_000, "x://a:" * 10_000],
)
def test_cleaning_a_worst_case_batch_is_fast(line: str) -> None:
    import time

    started = time.perf_counter()
    log.prepare([line], ("argus_agent_" + "t" * 32,))
    assert time.perf_counter() - started < 1.0


async def test_ring_buffer_evicts_oldest_and_seq_keeps_counting(
    db: Database, claimed, clock: FakeClock
) -> None:
    total = log.RING_LINES + 150
    sent = 0
    while sent < total:
        n = min(log.MAX_LINES, total - sent)
        await post(db, claimed, [f"line {sent + i + 1}" for i in range(n)])
        sent += n
        clock.advance(2)
    page = await read(db, claimed.id, limit=log.MAX_READ)
    assert len(page.lines) == log.MAX_READ  # the newest
    assert page.lines[-1].line == f"line {total}"
    everything = []
    after = 0
    while chunk := (await read(db, claimed.id, after=after, limit=log.MAX_READ)).lines:
        everything += chunk
        after = chunk[-1].seq
    assert len(everything) == log.RING_LINES
    assert everything[0].seq == total - log.RING_LINES + 1
    assert everything[0].line == f"line {everything[0].seq}"


async def test_old_attempts_are_dropped_and_retry_clears(
    db: Database, claimed, clock: FakeClock
) -> None:
    await post(db, claimed, ["attempt 1"])
    agents = [A, B]
    for n in range(2, 5):  # attempts 2, 3, 4
        clock.advance(claimed.lease_ttl_s + 1)
        async with db.write() as tx:
            await tasks.expire_leases(tx)
        task = await claim(db, agents[n % 2])
        assert task.attempt == n
        await post(db, task, [f"attempt {n}"], agent=agents[n % 2])
    assert (await read(db, claimed.id, attempt=1)).lines == []  # past the newest three
    assert [ln.line for ln in (await read(db, claimed.id, attempt=2)).lines] == [
        "attempt 2"
    ]
    assert [ln.line for ln in (await read(db, claimed.id)).lines] == ["attempt 4"]

    # a failed task that is retried starts over: attempt numbers restart at 1
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET state = 'failed', assignee_agent_id = NULL,"
            " lease_expires_at = NULL, timeout_at = NULL WHERE id = ?",
            (claimed.id,),
        )
        await tasks.retry(tx, claimed.id, "admin:test")
    assert (await read(db, claimed.id, attempt=4)).lines == []
    async with db.read() as conn:
        assert (await tasks.get_task(conn, claimed.id)).output_at is None


async def test_read_after_and_unknown_attempt(db: Database, claimed, clock) -> None:
    await post(db, claimed, ["a", "b", "c"])
    page = await read(db, claimed.id, after=1)
    assert [ln.line for ln in page.lines] == ["b", "c"] and page.last_seq == 3
    empty = await read(db, claimed.id, after=3)
    assert empty.lines == [] and empty.last_seq == 3
    assert (await read(db, claimed.id, attempt=9)).lines == []
    tail = await read(db, claimed.id, limit=2)
    assert [ln.line for ln in tail.lines] == ["b", "c"]


async def test_an_empty_tail_gives_the_tasks_log_seq(
    db: Database, claimed, clock: FakeClock
) -> None:
    assert (await read(db, claimed.id)).last_seq == 0  # nothing written yet
    await post(db, claimed, ["a", "b", "c"])
    clock.advance(claimed.lease_ttl_s + 1)
    async with db.write() as tx:
        await tasks.expire_leases(tx)
    task = await claim(db, B)
    assert task.attempt == 2
    # seq counts across attempts: attempt 2's first line will be 4, with no gap before it
    empty = await read(db, claimed.id)
    assert empty.attempt == 2 and empty.lines == [] and empty.last_seq == 3
    await post(db, task, ["d"], agent=B)
    assert [ln.seq for ln in (await read(db, claimed.id)).lines] == [4]
    # with `after`, an empty page still answers `after`
    assert (await read(db, claimed.id, after=4)).last_seq == 4


# -- SSE ---------------------------------------------------------------------------------


async def test_task_log_is_live_only_and_not_persisted(db: Database, claimed) -> None:
    stream = event_stream(db, after=None, keepalive=5)
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.05)
    await post(db, claimed, ["hello", "world"], source="delegate")
    chunk = await asyncio.wait_for(pending, 2)
    assert "id:" not in chunk  # Last-Event-ID must not move
    event = json.loads(chunk.split("data: ")[1])
    assert event["kind"] == "task.log" and event["subject_id"] == claimed.id
    assert event["data"] == {
        "task_id": claimed.id,
        "attempt": 1,
        "source": "delegate",
        "seq": 1,
        "at": event["data"]["at"],
        "lines": ["hello", "world"],
    }
    await stream.aclose()
    async with (
        db.read() as conn,
        conn.execute("SELECT count(*) FROM events WHERE kind = 'task.log'") as cur,
    ):
        assert (await cur.fetchone())[0] == 0
    replay = event_stream(db, after=0, keepalive=0.01)
    seen = [await anext(replay) for _ in range(40)]
    assert not any("task.log" in c for c in seen)
    await replay.aclose()


async def test_stream_delivers_task_log_only_to_the_lessee(
    db: Database, claimed
) -> None:
    other = event_stream(db, after=None, keepalive=0.2, projects={"global"}, agent_id=B)
    owner = event_stream(db, after=None, keepalive=5, projects={"global"}, agent_id=A)
    t_other, t_owner = (asyncio.ensure_future(anext(s)) for s in (other, owner))
    await asyncio.sleep(0.05)
    await post(db, claimed, ["private"])
    assert json.loads((await asyncio.wait_for(t_owner, 2)).split("data: ")[1])["data"][
        "lines"
    ] == ["private"]
    assert await asyncio.wait_for(t_other, 2) == ": keepalive\n\n"
    await other.aclose()
    await owner.aclose()


async def test_stream_checks_the_lease_once_per_task(
    db: Database, claimed, monkeypatch: pytest.MonkeyPatch
) -> None:
    from argus_api.eventlog import routes

    checks: list[str] = []
    real = routes.has_leased_task

    async def counting(conn, agent_id, task_id):
        checks.append(task_id)
        return await real(conn, agent_id, task_id)

    monkeypatch.setattr(routes, "has_leased_task", counting)
    owner = event_stream(db, after=None, keepalive=5, projects={"global"}, agent_id=A)
    pending = asyncio.ensure_future(anext(owner))
    await asyncio.sleep(0.05)
    for source in ("worker", "delegate"):  # one batch per source per interval
        await post(db, claimed, [source], source=source)
        await asyncio.wait_for(pending, 2)
        pending = asyncio.ensure_future(anext(owner))
    pending.cancel()
    await owner.aclose()
    assert checks == [claimed.id]


async def test_wait_ignores_the_transient_log_and_keeps_its_cursor(
    db: Database, claimed
) -> None:
    async with db.read() as conn, conn.execute("SELECT max(id) FROM events") as cur:
        head = (await cur.fetchone())[0]
    pending = asyncio.ensure_future(wait(db, head, 0.3))
    await asyncio.sleep(0.05)
    await post(db, claimed, ["output only"])
    result = await asyncio.wait_for(pending, 2)
    assert result.timed_out and result.events == [] and result.next_cursor == head


async def test_a_rolled_back_batch_publishes_nothing(db: Database, claimed) -> None:
    stream = event_stream(db, after=None, keepalive=0.1)
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.05)
    with pytest.raises(RuntimeError):
        async with db.write() as tx:
            await log.append(tx, A, claimed.id, 1, "worker", ["x"])
            raise RuntimeError
    assert await asyncio.wait_for(pending, 2) == ": keepalive\n\n"
    await stream.aclose()


# -- REST and MCP ------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        yield c


def register(client: TestClient, role: str = "worker") -> tuple[dict[str, str], str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": role}).json()
    res = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    assert res.status_code == 201
    return {"Authorization": f"Bearer {res.json()['token']}"}, res.json()["token"]


def start(client: TestClient):
    (worker, token), (other, _) = register(client), register(client)
    task = client.post("/api/tasks", json={"title": "t", "project": "global"}).json()
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    assert claimed["id"] == task["id"]
    return claimed, worker, other, token


def test_rest_permissions_and_contract(client: TestClient) -> None:
    claimed, worker, other, token = start(client)
    url = f"/api/tasks/{claimed['id']}/log"
    body = {"attempt": claimed["attempt"], "source": "worker", "lines": ["go"]}

    assert client.post(url, json=body).status_code == 403  # admin is not an agent
    assert client.post(url, json=body, headers=other).status_code == 409
    assert client.post(url, json=body, headers={"Authorization": ""}).status_code == 401
    ok = client.post(url, json=body, headers=worker)
    assert ok.status_code == 200, ok.text
    assert ok.json()["accepted"] == 1 and ok.json()["last_seq"] == 1
    assert ok.json()["output_at"]

    assert client.post(url, json=body, headers=worker).status_code == 429
    bad = {**body, "lines": ["x"] * (log.MAX_LINES + 1)}
    assert client.post(url, json=bad, headers=worker).status_code == 422
    assert (
        client.post(url, json={**body, "lines": []}, headers=worker).status_code == 422
    )
    assert (
        client.post(url, json={**body, "source": "nope"}, headers=worker).status_code
        == 422
    )
    stale = client.post(url, json={**body, "attempt": 7}, headers=worker)
    assert (
        stale.status_code == 409 and stale.json()["detail"]["code"] == "stale_attempt"
    )

    # the agent's own token never lands in the log
    own = {**body, "source": "delegate", "lines": [f"token is {token}"]}
    assert client.post(url, json=own, headers=worker).status_code == 200
    got = client.get(url, headers=worker).json()
    assert [ln["line"] for ln in got["lines"]] == ["go", "token is [redacted]"]
    assert token not in json.dumps(got)

    # read scope: managers all, the lessee, not another worker
    assert client.get(url).status_code == 200
    denied = client.get(url, headers=other)
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "not_leased"
    assert client.get(f"{url}?after=1", headers=worker).json()["lines"][0]["seq"] == 2
    assert client.get(f"{url}?limit=0").status_code == 422

    view = client.get(f"/api/tasks/{claimed['id']}").json()
    assert view["output_at"]
    listed = client.get("/api/tasks?view=summary").json()["items"][0]
    assert listed["output_at"] == view["output_at"]
    assert "output_at" in client.get("/openapi.json").text


def test_a_refused_batch_is_never_cleaned(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    claimed, worker, other, _ = start(client)
    url = f"/api/tasks/{claimed['id']}/log"
    body = {"attempt": claimed["attempt"], "source": "worker", "lines": ["go"]}
    assert client.post(url, json=body, headers=worker).status_code == 200

    def costly(*_: object) -> list[str]:
        raise AssertionError("cleaned a batch that is refused anyway")

    monkeypatch.setattr(log, "clean", costly)
    stale = client.post(url, json={**body, "attempt": 7}, headers=worker)
    assert stale.status_code == 409, stale.text
    assert client.post(url, json=body, headers=other).status_code == 409
    assert client.post(url, json=body, headers=worker).status_code == 429


def test_the_token_is_masked_whatever_the_scheme_case(client: TestClient) -> None:
    claimed, _, _, token = start(client)
    url = f"/api/tasks/{claimed['id']}/log"
    body = {
        "attempt": claimed["attempt"],
        "source": "worker",
        "lines": [f"saw {token}"],
    }
    res = client.post(url, json=body, headers={"Authorization": f"BEARER  {token}"})
    assert res.status_code == 200, res.text
    got = client.get(url).json()
    assert [ln["line"] for ln in got["lines"]] == ["saw [redacted]"]


def test_read_bounds_are_422_not_500(client: TestClient) -> None:
    claimed, *_ = start(client)
    url = f"/api/tasks/{claimed['id']}/log"
    for param in ("attempt", "after"):
        assert client.get(f"{url}?{param}={2**63}").status_code == 422
        assert client.get(f"{url}?{param}={2**63 - 1}").status_code == 200


def test_mcp_get_task_log(client: TestClient) -> None:
    from test_mcp import rpc, tool

    claimed, worker, other, token = start(client)
    other_token = other["Authorization"].removeprefix("Bearer ")
    client.post(
        f"/api/tasks/{claimed['id']}/log",
        json={"attempt": 1, "source": "delegate", "lines": ["from the delegate"]},
        headers=worker,
    )
    names = {t["name"] for t in rpc(client, ADMIN_TOKEN, "tools/list")["tools"]}
    assert "get_task_log" in names
    result = tool(client, token, "get_task_log", task_id=claimed["id"])
    assert result["attempt"] == 1
    assert [(ln["source"], ln["line"]) for ln in result["lines"]] == [
        ("delegate", "from the delegate")
    ]
    after = tool(
        client, token, "get_task_log", task_id=claimed["id"], after=result["last_seq"]
    )
    assert after["lines"] == []
    assert tool(client, ADMIN_TOKEN, "get_task_log", task_id=claimed["id"])["lines"]
    denied = tool(client, other_token, "get_task_log", task_id=claimed["id"])
    assert denied["error"].startswith("403 not_leased")
    assert mcp_server.audiences["get_task_log"]
