"""Command guard denials as events (docs/specs/command-guard.spec.md 3)."""

import json
from collections.abc import Generator
from pathlib import Path

import pytest
from api_support import FakeClock, insert_agent
from argus_api.db import Database
from argus_api.errors import LeaseConflictError, ValidationFailedError
from argus_api.main import app
from argus_api.tasks import command_blocked as cb
from argus_api.tasks import service as tasks
from argus_core.events import COMMAND_GUARD_REASONS, TASK_COMMAND_BLOCKED
from argus_core.tasks import TaskCreate
from fastapi.testclient import TestClient

A, B = "agent-a", "agent-b"
ADMIN_TOKEN = "test-admin-token-" + "c" * 32
GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2
SK_KEY = "sk-" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8"
BEARER = "Authorization: Bearer abcdefghijklmnop.qrstuvwxyz012345"
PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAx\nabc\n"
    "-----END RSA PRIVATE KEY-----"
)


def body(attempt: int = 1, **kw) -> cb.CommandBlockedWrite:
    return cb.CommandBlockedWrite.model_validate(
        {
            "attempt": attempt,
            "tool_use_id": "toolu_01",
            "rule": "rm_root",
            "command": "rm -rf /",
        }
        | kw
    )


async def report(db: Database, task, *, agent: str = A, secrets=(), **kw):
    async with db.write() as tx:
        return await cb.store(
            tx, agent, task.id, body(kw.pop("attempt", task.attempt), **kw), secrets
        )


async def events(db: Database, task_id: str) -> list[dict]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT actor, data FROM events WHERE kind = ? AND subject_id = ? ORDER BY id",
            (TASK_COMMAND_BLOCKED, task_id),
        ) as cur,
    ):
        return [
            {"actor": r["actor"], **json.loads(r["data"])} for r in await cur.fetchall()
        ]


@pytest.fixture
async def claimed(db: Database):
    await insert_agent(db, A)
    await insert_agent(db, B)
    async with db.write() as tx:
        await tasks.create_task(
            tx, TaskCreate(project="demo", title="t", max_attempts=6), "admin:test"
        )
    async with db.write() as tx:
        task = await tasks.claim(tx, A, [])
    assert task is not None
    return task


async def test_one_denial_is_one_event_with_the_servers_reason(
    db: Database, claimed
) -> None:
    assert (await report(db, claimed)).status == "recorded"
    assert await events(db, claimed.id) == [
        {
            "actor": f"agent:{A}",
            "tool": "Bash",
            "rule": "rm_root",
            "reason": COMMAND_GUARD_REASONS["rm_root"],
            "attempt": 1,
            "command": "rm -rf /",
        }
    ]


async def test_the_same_denial_twice_is_one_event(db: Database, claimed) -> None:
    await report(db, claimed)
    assert (await report(db, claimed, command="rm -rf /")).status == "duplicate"
    assert len(await events(db, claimed.id)) == 1
    await report(db, claimed, tool_use_id="toolu_02")
    assert len(await events(db, claimed.id)) == 2


async def test_an_attempt_records_a_bounded_number_of_events(
    db: Database, claimed
) -> None:
    for i in range(cb.MAX_PER_ATTEMPT):
        await report(db, claimed, tool_use_id=f"toolu_{i}")
    over = await report(db, claimed, tool_use_id="toolu_over")
    assert over.status == "dropped"
    assert len(await events(db, claimed.id)) == cb.MAX_PER_ATTEMPT
    # a repeat of a stored one is still a duplicate, not a drop
    assert (await report(db, claimed, tool_use_id="toolu_0")).status == "duplicate"


async def test_a_stale_or_foreign_attempt_is_refused(
    db: Database, claimed, clock: FakeClock
) -> None:
    with pytest.raises(LeaseConflictError) as err:
        await report(db, claimed, agent=B)
    assert err.value.code == "not_assignee"
    with pytest.raises(LeaseConflictError) as err:
        await report(db, claimed, attempt=2)
    assert err.value.code == "stale_attempt"
    clock.advance(claimed.lease_ttl_s + 1)
    with pytest.raises(LeaseConflictError) as err:
        await report(db, claimed)
    assert err.value.code == "lease_expired"
    assert await events(db, claimed.id) == []


@pytest.mark.parametrize("rule", ["", "nope", "RM_ROOT", "rm_root "])
async def test_the_rule_must_be_in_the_closed_set(
    db: Database, claimed, rule: str
) -> None:
    with pytest.raises(ValidationFailedError) as err:
        await report(db, claimed, rule=rule)
    assert err.value.code == "unknown_rule"
    assert await events(db, claimed.id) == []


async def test_every_rule_takes_its_reason_from_the_server(
    db: Database, claimed
) -> None:
    for rule, reason in COMMAND_GUARD_REASONS.items():
        await report(db, claimed, rule=rule, tool_use_id=f"id_{rule}")
        assert (await events(db, claimed.id))[-1]["reason"] == reason


@pytest.mark.parametrize(
    ("secret", "needle"),
    [
        (GITHUB, GITHUB[:20]),
        (SK_KEY, SK_KEY[3:]),
        (BEARER, "abcdefghijklmnop"),
        (PRIVATE_KEY, "MIIEowIBAAKCAQEAx"),
    ],
    ids=["github", "sk", "bearer", "pem"],
)
async def test_credentials_never_reach_the_stored_event(
    db: Database, claimed, secret: str, needle: str
) -> None:
    await report(db, claimed, command=f"curl -H 'x' {secret} ; rm -rf /")
    stored = await events(db, claimed.id)
    assert needle not in json.dumps(stored)
    assert "[redacted" in stored[0]["command"]


async def test_the_callers_own_token_is_masked(db: Database, claimed) -> None:
    token = "tok-" + "z9" * 16
    await report(db, claimed, secrets=(token,), command=f"echo {token}")
    assert token not in json.dumps(await events(db, claimed.id))


def test_the_command_is_one_clean_bounded_line() -> None:
    assert cb.clean_command("rm\t-rf\n  /\x1b[31m\x07") == "rm -rf /"
    long = "echo " + "x" * 1000
    out = cb.clean_command(long)
    assert len(out) == cb.MAX_COMMAND_CHARS and out.endswith("…")
    assert out == long[: cb.MAX_COMMAND_CHARS - 1] + "…"
    exact = "y" * cb.MAX_COMMAND_CHARS
    assert cb.clean_command(exact) == exact


def test_a_secret_is_masked_before_the_cut() -> None:
    out = cb.clean_command("a" * 190 + " " + GITHUB)
    assert "ghp_" not in out and len(out) <= cb.MAX_COMMAND_CHARS


async def test_a_denial_leaves_the_task_alone(db: Database, claimed) -> None:
    async def snapshot():
        async with db.read() as conn:
            async with conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (claimed.id,)
            ) as cur:
                task = tuple(await cur.fetchone())
            async with conn.execute(
                "SELECT kind FROM events WHERE kind != ? ORDER BY id",
                (TASK_COMMAND_BLOCKED,),
            ) as cur:
                others = [r["kind"] for r in await cur.fetchall()]
        return task, others

    before = await snapshot()
    await report(db, claimed)
    assert await snapshot() == before


async def test_a_retry_forgets_the_blocks_of_the_old_attempts(
    db: Database, claimed
) -> None:
    await report(db, claimed)
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET state = 'failed', assignee_agent_id = NULL,"
            " lease_expires_at = NULL WHERE id = ?",
            (claimed.id,),
        )
        await tasks.retry(tx, claimed.id, "admin:test")
    async with (
        db.read() as conn,
        conn.execute("SELECT count(*) FROM task_command_blocks") as cur,
    ):
        assert (await cur.fetchone())[0] == 0


# -- REST ----------------------------------------------------------------------------


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


def test_rest_permissions_and_contract(client: TestClient) -> None:
    (worker, token), (other, _) = register(client), register(client)
    client.post("/api/tasks", json={"title": "t", "project": "demo"})
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    url = f"/api/tasks/{claimed['id']}/command-blocked"
    payload = {
        "attempt": claimed["attempt"],
        "tool_use_id": "toolu_01",
        "rule": "mkfs",
        "command": f"mkfs.ext4 /dev/sda1 {token}",
    }
    assert client.post(url, json=payload).status_code == 403  # admin is not an agent
    assert client.post(url, json=payload, headers=other).status_code == 409
    res = client.post(url, json=payload, headers=worker)
    assert res.status_code == 200 and res.json() == {"status": "recorded"}
    assert client.post(url, json=payload, headers=worker).json() == {
        "status": "duplicate"
    }
    stale = payload | {"attempt": claimed["attempt"] + 1, "tool_use_id": "t2"}
    res = client.post(url, json=stale, headers=worker)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "stale_attempt"
    res = client.post(
        url, json=payload | {"rule": "x", "tool_use_id": "t3"}, headers=worker
    )
    assert res.status_code == 422 and res.json()["detail"]["code"] == "unknown_rule"
    bad_id = payload | {"tool_use_id": "has space"}
    assert client.post(url, json=bad_id, headers=worker).status_code == 422
    got = client.get(f"/api/tasks/{claimed['id']}", headers=worker).json()
    assert got["state"] == "claimed"
