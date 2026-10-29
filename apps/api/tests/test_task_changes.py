"""Task changes (docs/specs/task-changes.spec.md)."""

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
    NotFoundError,
    RateLimitedError,
    TooLargeError,
)
from argus_api.eventlog.routes import event_stream
from argus_api.main import app
from argus_api.tasks import changes
from argus_api.tasks import service as tasks
from argus_core.tasks import TaskCreate
from fastapi.testclient import TestClient

A, B = "agent-a", "agent-b"
ADMIN_TOKEN = "test-admin-token-" + "c" * 32
SHA = "0123456789abcdef0123456789abcdef01234567"
PATCH = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"


def snapshot(attempt: int = 1, **kw) -> dict:
    return {
        "attempt": attempt,
        "at": "2026-10-29T12:00:00Z",
        "branch": "argus/task-1a2b3c4d",
        "base": {"ref": "origin/main", "sha": SHA},
        "head": SHA[::-1],
        "commits": [{"sha": SHA[::-1], "subject": "feat: x"}],
        "files": [{"path": "x.py", "status": "M", "added": 1, "deleted": 1}],
        "untracked": [{"path": "notes.txt", "bytes": 12}],
        "patch": PATCH,
    } | kw


async def claim(db: Database, agent: str = A):
    async with db.write() as tx:
        task = await tasks.claim(tx, agent, [])
    assert task is not None
    return task


async def put(db: Database, task, *, agent=A, secrets=(), **kw):
    body = changes.prepare(
        changes.ChangesWrite.model_validate(snapshot(task.attempt) | kw), secrets
    )
    async with db.write() as tx:
        return await changes.store(tx, agent, task.id, body)


async def read(db: Database, task_id: str, attempt: int | None = None):
    async with db.read() as conn:
        return await changes.read(conn, task_id, attempt)


@pytest.fixture
async def claimed(db: Database):
    await insert_agent(db, A)
    await insert_agent(db, B)
    async with db.write() as tx:
        await tasks.create_task(
            tx, TaskCreate(project="global", title="t", max_attempts=6), "admin:test"
        )
    return await claim(db)


# -- writing -------------------------------------------------------------------------


async def test_snapshot_is_stored_and_read_back(
    db: Database, claimed, clock: FakeClock
) -> None:
    accepted = await put(db, claimed)
    assert (accepted.files, accepted.added, accepted.deleted) == (1, 1, 1)
    got = await read(db, claimed.id)
    assert got.attempt == 1 and got.received_at == clock.now
    assert got.base is not None and got.base.ref == "origin/main"
    assert [f.path for f in got.files] == ["x.py"]
    assert got.untracked[0].bytes == 12 and got.patch == PATCH
    assert not got.truncated.patch


async def test_a_new_snapshot_replaces_the_attempts_one(
    db: Database, claimed, clock: FakeClock
) -> None:
    await put(db, claimed)
    clock.advance(changes.MIN_INTERVAL_S)
    await put(db, claimed, base=None, head=None, files=[], patch="")
    got = await read(db, claimed.id)
    assert got.base is None and got.head is None and got.files == []


async def test_no_snapshot_is_404_no_changes(db: Database, claimed) -> None:
    with pytest.raises(NotFoundError) as err:
        await read(db, claimed.id)
    assert err.value.code == "no_changes"
    with pytest.raises(NotFoundError) as err:
        await read(db, "missing")
    assert err.value.code == "not_found"


async def test_only_the_live_lease_of_the_current_attempt_may_write(
    db: Database, claimed, clock: FakeClock
) -> None:
    with pytest.raises(LeaseConflictError) as err:
        await put(db, claimed, agent=B)
    assert err.value.code == "not_assignee"
    with pytest.raises(LeaseConflictError) as err:
        body = changes.ChangesWrite.model_validate(snapshot(2))
        async with db.write() as tx:
            await changes.store(tx, A, claimed.id, body)
    assert err.value.code == "stale_attempt"
    clock.advance(claimed.lease_ttl_s + 1)
    with pytest.raises(LeaseConflictError) as err:
        await put(db, claimed)
    assert err.value.code == "lease_expired"
    with pytest.raises(NotFoundError):
        await read(db, claimed.id)


async def test_one_snapshot_per_interval(
    db: Database, claimed, clock: FakeClock
) -> None:
    await put(db, claimed)
    clock.advance(changes.MIN_INTERVAL_S - 1)
    with pytest.raises(RateLimitedError):
        await put(db, claimed)
    clock.advance(1)
    await put(db, claimed)


# -- bounds and redaction ------------------------------------------------------------


def test_a_long_patch_is_cut_at_a_line() -> None:
    line = "+" + "x" * 99 + "\n"
    patch, cut = changes.cut_patch(line * 3000)  # ~300 KiB
    assert cut and patch.endswith("\n" + changes.CUT_MARK)
    assert len(patch.encode()) <= changes.MAX_PATCH_BYTES
    assert all(ln == line.rstrip("\n") for ln in patch.splitlines()[:-1])
    assert changes.cut_patch(PATCH) == (PATCH, False)


async def test_a_cut_patch_sets_the_flag(db: Database, claimed) -> None:
    await put(db, claimed, patch="+" + "y" * 300 * 1024 + "\n+tail\n")
    got = await read(db, claimed.id)
    assert got.truncated.patch and got.patch.endswith(changes.CUT_MARK)


def test_an_oversized_body_is_413() -> None:
    body = changes.ChangesWrite.model_validate(
        snapshot(patch="z" * (changes.MAX_BODY_BYTES + 1))
    )
    with pytest.raises(TooLargeError) as err:
        changes.prepare(body)
    assert err.value.code == "changes_too_large"


async def test_secrets_are_redacted_everywhere(db: Database, claimed) -> None:
    key = "ghp_" + "a" * 36
    await put(
        db,
        claimed,
        secrets=("my-own-token-value",),
        branch=f"argus/{key}",
        commits=[{"sha": SHA, "subject": f"add {key}"}],
        files=[{"path": f"{key}.txt", "status": "A", "added": 1, "deleted": 0}],
        untracked=[{"path": f"tmp/{key}", "bytes": 1}],
        patch=f"+TOKEN={key}\n+saw my-own-token-value\n+\x1b[31mred\x1b[0m\n",
    )
    got = await read(db, claimed.id)
    stored = got.model_dump_json()
    assert key not in stored and "my-own-token-value" not in stored
    assert "\x1b" not in got.patch and "+red" in got.patch


# -- retention -----------------------------------------------------------------------


async def test_three_attempts_keep_a_snapshot_and_retry_clears_them(
    db: Database, claimed, clock: FakeClock
) -> None:
    task, agents = claimed, (A, B)
    for n in range(1, 5):
        await put(db, task, agent=agents[(n - 1) % 2], patch=f"+attempt {n}\n")
        clock.advance(task.lease_ttl_s + 1)
        async with db.write() as tx:
            await tasks.expire_leases(tx)
        if n < 4:
            task = await claim(db, agents[n % 2])
    with pytest.raises(NotFoundError):
        await read(db, claimed.id, attempt=1)  # past the newest three
    assert (await read(db, claimed.id, attempt=2)).patch == "+attempt 2\n"
    assert (await read(db, claimed.id, attempt=4)).patch == "+attempt 4\n"

    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET state = 'failed', assignee_agent_id = NULL,"
            " lease_expires_at = NULL, timeout_at = NULL WHERE id = ?",
            (claimed.id,),
        )
        await tasks.retry(tx, claimed.id, "admin:test")
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT count(*) FROM task_changes WHERE task_id = ?", (claimed.id,)
        ) as cur,
    ):
        assert (await cur.fetchone())[0] == 0


# -- SSE -----------------------------------------------------------------------------


async def test_task_changes_event_is_a_live_summary(db: Database, claimed) -> None:
    stream = event_stream(db, after=None, keepalive=5)
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.05)
    await put(db, claimed)
    chunk = await asyncio.wait_for(pending, 2)
    assert "id:" not in chunk  # transient: Last-Event-ID must not move
    event = json.loads(chunk.split("data: ")[1])
    assert event["kind"] == "task.changes"
    assert event["data"] == {
        "task_id": claimed.id,
        "attempt": 1,
        "at": event["data"]["at"],
        "head": SHA[::-1],
        "files": 1,
        "added": 1,
        "deleted": 1,
    }  # never the patch
    await stream.aclose()
    async with (
        db.read() as conn,
        conn.execute("SELECT count(*) FROM events WHERE kind = 'task.changes'") as cur,
    ):
        assert (await cur.fetchone())[0] == 0


# -- REST and MCP --------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        yield c


def register(client: TestClient) -> tuple[dict[str, str], str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
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
    url = f"/api/tasks/{claimed['id']}/changes"
    body = snapshot(claimed["attempt"], patch=f"+saw {token}\n")

    assert client.get(url).status_code == 404  # nothing posted yet
    assert client.put(url, json=body).status_code == 403  # admin is not an agent
    assert client.put(url, json=body, headers=other).status_code == 409
    res = client.put(url, json=body, headers=worker)
    assert res.status_code == 200, res.text
    assert res.json()["files"] == 1
    assert client.put(url, json=body, headers=worker).status_code == 429

    got = client.get(url).json()
    assert got["patch"] == "+saw [redacted]\n" and got["attempt"] == 1
    assert client.get(url, headers=worker).status_code == 200
    denied = client.get(url, headers=other)
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "not_leased"
    assert client.get(f"{url}?attempt={2**63}").status_code == 422
    bad = snapshot(claimed["attempt"], files=[{"path": "x", "status": "Q"}])
    assert client.put(url, json=bad, headers=worker).status_code == 422


def test_mcp_get_task_changes(client: TestClient) -> None:
    from test_mcp import rpc, tool

    claimed, worker, other, token = start(client)
    other_token = other["Authorization"].removeprefix("Bearer ")
    url = f"/api/tasks/{claimed['id']}/changes"
    client.put(url, json=snapshot(), headers=worker)
    names = {t["name"] for t in rpc(client, ADMIN_TOKEN, "tools/list")["tools"]}
    assert "get_task_changes" in names
    result = tool(client, token, "get_task_changes", task_id=claimed["id"])
    assert result["files"][0]["path"] == "x.py" and result["patch"] == PATCH
    denied = tool(client, other_token, "get_task_changes", task_id=claimed["id"])
    assert denied["error"].startswith("403 not_leased")
    assert mcp_server.audiences["get_task_changes"]
