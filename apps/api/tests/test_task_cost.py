"""Task cost and budget (docs/specs/task-cost.spec.md)."""

import pytest
import test_mcp
from api_support import DONE, insert_agent
from argus_api.accounts import pricing, usage
from argus_api.accounts import service as accounts
from argus_api.db import Database
from argus_api.errors import ConflictError
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import cost as task_cost
from argus_api.tasks import service as tasks
from argus_api.tasks import steers
from argus_core.accounts import AccountCreate, UsageReport
from argus_core.task_edit import TaskChanges
from argus_core.tasks import TaskCreate
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_mcp import ADMIN_TOKEN, register, tool

client = test_mcp.client  # the same app fixture as the MCP tests

A = "agent-a"
OPUS = "claude-opus-5-5"  # $5 per million input tokens in the shipped table
DOLLAR = 200_000  # input tokens of OPUS that cost $1


@pytest.fixture(autouse=True)
async def agent(db: Database) -> None:
    await insert_agent(db, A)


async def create(db: Database, **kw):
    async with db.write() as tx:
        return await tasks.create_task(
            tx, TaskCreate(project="demo", title="t", **kw), "admin:test"
        )


async def claim(db: Database):
    async with db.write() as tx:
        task = await tasks.claim(tx, A, [])
    assert task is not None
    return task


async def spend(
    db: Database,
    task_id: str,
    attempt: int | None,
    dollars: float = 1.0,
    *,
    model: str | None = OPUS,
    measurement: str = "measured",
):
    """One usage row, through the hook the usage paths call; returns its tx."""
    measured = measurement == "measured"
    async with db.write() as tx:
        await usage.insert_usage(
            tx,
            None,
            A,
            task_id,
            attempt=attempt,
            input_tokens=int(dollars * DOLLAR) if measured else None,
            model=model,
            measurement=measurement,
            reason=None if measured else "no_report",
        )
        await task_cost.after_usage(tx, task_id)
    return tx


async def figures(db: Database, task_id: str):
    async with db.read() as conn:
        return await task_cost.task_cost(conn, task_id, pricing.load(None))


async def allow_steers(db: Database) -> None:
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE agents SET runtime = ? WHERE id = ?",
            ('{"features": ["steer"]}', A),
        )


async def budget_steers(db: Database, task_id: str) -> list[tuple[int, str]]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT attempt, actor FROM task_steers WHERE task_id = ? ORDER BY id",
            (task_id,),
        ) as cur,
    ):
        return [(r["attempt"], r["actor"]) for r in await cur.fetchall()]


# -- figures (3.1, 3.2) ------------------------------------------------------------


async def test_totals_and_attempts_with_an_unattributed_row(db: Database) -> None:
    task = await create(db)
    await spend(db, task.id, 1, 1.0)
    await spend(db, task.id, 1, 0.5, model="claude-haiku-5-5")
    await spend(db, task.id, 2, 2.0)
    await spend(db, task.id, None, 0.25)  # in the total only
    cost = await figures(db, task.id)
    assert cost.totals.runs == 4 and cost.totals.attempts == 2
    assert cost.totals.estimated_cost_usd == pytest.approx(1.0 + 0.1 + 2.0 + 0.25)
    assert [(a.attempt, a.runs, a.models) for a in cost.by_attempt] == [
        (1, 2, ["claude-haiku-5-5", OPUS]),
        (2, 1, [OPUS]),
    ]
    assert cost.by_attempt[1].estimated_cost_usd == pytest.approx(2.0)
    assert (cost.partial, cost.budget_usd, cost.budget_state) == (False, None, "none")


async def test_unpriced_and_not_reported_runs_make_the_task_partial(
    db: Database,
) -> None:
    task = await create(db, budget_usd=10)
    await spend(db, task.id, 1, 1.0)
    assert (await figures(db, task.id)).budget_state == "ok"
    await spend(db, task.id, 1, 1.0, model="no-such-model")
    cost = await figures(db, task.id)
    assert cost.partial and cost.totals.unpriced_runs == 1
    assert cost.pricing.unpriced_models == ["no-such-model"]
    assert cost.totals.estimated_cost_usd == pytest.approx(1.0)  # never 0 by omission
    assert cost.budget_state == "partial_ok"
    other = await create(db, budget_usd=10)
    await spend(db, other.id, 1, measurement="not_reported")
    cost = await figures(db, other.id)
    assert cost.partial and cost.totals.not_reported_runs == 1
    assert cost.totals.estimated_cost_usd is None and cost.totals.input_tokens is None
    assert cost.by_attempt[0].not_reported_attempts == 1


@pytest.mark.parametrize(
    ("estimated", "partial", "state"),
    [
        (None, False, "ok"),
        (7.99, False, "ok"),
        (8.0, False, "warning"),
        (8.0, True, "partial_warning"),
        (1.0, True, "partial_ok"),
        (10.0, False, "exhausted"),
        (10.0, True, "exhausted"),  # judged on the known figure
    ],
)
def test_budget_state(estimated: float | None, partial: bool, state: str) -> None:
    assert task_cost.budget_state(estimated, 10.0, partial) == state
    assert task_cost.budget_state(estimated, None, partial) == "none"


# -- live event and steer (3.3, 3.5) ------------------------------------------------


async def test_usage_publishes_task_cost_in_its_transaction(db: Database) -> None:
    store = MemorySecretStore()
    await store.put("acc", {"api_key": "x"})
    async with db.write() as tx:
        acc = await accounts.create_account(
            tx,
            "acc",
            AccountCreate(
                name="acc", provider="openai", kind="api_key", secret_ref="sops:acc"
            ),
            "sops:acc",
            "admin:test",
            store,
        )
    task = await create(db, budget_usd=2)
    task = await claim(db)
    async with db.write() as tx:
        await accounts.lease(
            tx, A, ["openai"], store, "engine", task_id=task.id, attempt=task.attempt
        )
    async with db.write() as tx:
        await accounts.record_usage(
            tx,
            acc.id,
            A,
            UsageReport(
                task_id=task.id,
                attempt=task.attempt,
                input_tokens=int(1.7 * DOLLAR),
                model=OPUS,
                run_key="r1",
            ),
        )
        [event] = [e for e in tx.transient if e.kind == "task.cost"]
        assert not [e for e in tx.events if e.kind == "task.cost"]  # not stored
    assert event.data == {
        "task_id": task.id,
        "attempt": task.attempt,
        "estimated_cost_usd": pytest.approx(1.7),
        "partial": False,
        "budget_usd": 2.0,
        "budget_state": "warning",
    }


async def test_crossing_the_budget_steers_once_per_attempt(db: Database) -> None:
    task = await create(db, budget_usd=1, max_attempts=3)
    await claim(db)
    await allow_steers(db)
    await spend(db, task.id, 1, 0.9)
    assert await budget_steers(db, task.id) == []
    await spend(db, task.id, 1, 0.2)
    await spend(db, task.id, 1, 0.2)
    assert await budget_steers(db, task.id) == [(1, "system:budget")]
    async with db.read() as conn:
        [made] = (await steers.list_steers(conn, task.id, 1)).items
    assert made.message.startswith("The task's budget of $1.00 is spent (estimate $")


async def test_no_steer_when_the_assignee_cannot_take_one(db: Database) -> None:
    task = await create(db, budget_usd=1)
    await claim(db)
    await spend(db, task.id, 1, 2.0)
    assert await budget_steers(db, task.id) == []
    async with db.read() as conn:
        row = await (
            await conn.execute(
                "SELECT state, budget_steered_attempt FROM tasks WHERE id = ?",
                (task.id,),
            )
        ).fetchone()
    assert (row["state"], row["budget_steered_attempt"]) == ("claimed", 1)


async def test_a_refused_steer_is_logged_and_changes_nothing(db: Database) -> None:
    task = await create(db, budget_usd=1)
    await claim(db)
    await allow_steers(db)
    async with db.write() as tx:
        await steers.create(tx, task.id, "operator first", "admin:test")
    await spend(db, task.id, 1, 2.0)  # same second: the steer rate limit refuses it
    assert await budget_steers(db, task.id) == [(1, "admin:test")]
    async with db.read() as conn:
        row = await (
            await conn.execute(
                "SELECT budget_steered_attempt FROM tasks WHERE id = ?", (task.id,)
            )
        ).fetchone()
    assert row["budget_steered_attempt"] is None


# -- no new attempt over budget (3.6) ------------------------------------------------


async def _over_budget(db: Database, **kw):
    task = await create(db, budget_usd=1, max_attempts=5, **kw)
    await claim(db)
    await spend(db, task.id, 1, 1.5)
    return task


async def _fail(db: Database, task_id: str) -> None:
    async with db.write() as tx:
        # A failure that retries (failure classes 3.2), not a task-class give-up.
        await tasks.fail(tx, A, task_id, 1, "boom", "session_lost")


async def _release(db: Database, task_id: str) -> None:
    async with db.write() as tx:
        await tasks.release(tx, task_id, "admin:test", "operator")


async def _expire(db: Database, task_id: str) -> None:
    async with db.write() as tx:
        await tasks.expire_agent_leases(tx, A)


@pytest.mark.parametrize("end", [_fail, _release, _expire])
async def test_an_attempt_ending_over_budget_blocks_instead_of_requeueing(
    db: Database, end
) -> None:
    task = await _over_budget(db)
    await end(db, task.id)
    async with db.read() as conn:
        got = await tasks.get_task(conn, task.id)
    assert (got.state, got.block_reason, got.attempt) == (
        "blocked",
        "budget_exhausted",
        1,
    )
    assert got.progress_message.startswith("budget of $1.00 is spent (estimate $1.50)")


async def test_claim_blocks_a_ready_task_over_budget(db: Database) -> None:
    task = await create(db, budget_usd=1)
    await spend(db, task.id, None, 1.0)  # an earlier, unattributed row
    async with db.write() as tx:
        assert await tasks.claim(tx, A, []) is None
    async with db.read() as conn:
        got = await tasks.get_task(conn, task.id)
    assert (got.state, got.block_reason, got.attempt) == (
        "blocked",
        "budget_exhausted",
        0,
    )


async def test_a_partial_task_is_judged_on_its_known_figure(db: Database) -> None:
    task = await _over_budget(db)
    await spend(db, task.id, 1, model="no-such-model")
    await _fail(db, task.id)
    async with db.read() as conn:
        got = await tasks.get_task(conn, task.id)
    assert got.block_reason == "budget_exhausted"
    assert "partial estimate $1.50 (unpriced or unreported runs not counted)" in (
        got.progress_message or ""
    )


async def test_under_budget_requeues_as_before(db: Database) -> None:
    task = await create(db, budget_usd=10, max_attempts=3)
    await claim(db)
    await spend(db, task.id, 1, 1.0)
    await _fail(db, task.id)
    async with db.read() as conn:
        assert (await tasks.get_task(conn, task.id)).state == "ready"


async def test_complete_over_budget_is_accepted(db: Database) -> None:
    task = await _over_budget(db)
    async with db.write() as tx:
        done = await tasks.complete(tx, A, task.id, 1, DONE)
    assert done.state == "succeeded"


async def test_unblock_is_refused_until_the_budget_is_raised(db: Database) -> None:
    task = await _over_budget(db)
    await _release(db, task.id)
    with pytest.raises(ConflictError) as refused:
        async with db.write() as tx:
            await tasks.unblock(tx, task.id, "admin:test")
    assert refused.value.code == "budget_exhausted"
    async with db.read() as conn:
        blocked = await tasks.get_task(conn, task.id)
    assert "budget_usd" in blocked.editable_fields
    async with db.write() as tx:
        raised = await tasks.edit_task(
            tx, task.id, TaskChanges(budget_usd=5), blocked.version, "admin:test"
        )
    assert (raised.state, raised.block_reason, raised.budget_usd) == ("ready", None, 5)
    async with db.write() as tx:
        again = await tasks.claim(tx, A, [])
    assert again is not None and again.attempt == 2


async def test_clearing_the_budget_also_continues(db: Database) -> None:
    task = await _over_budget(db)
    await _release(db, task.id)
    async with db.read() as conn:
        blocked = await tasks.get_task(conn, task.id)
    async with db.write() as tx:
        cleared = await tasks.edit_task(
            tx,
            task.id,
            TaskChanges.model_validate({"budget_usd": None}),
            blocked.version,
            "admin:test",
        )
    assert (cleared.state, cleared.budget_usd) == ("ready", None)


async def test_raising_too_little_leaves_it_blocked(db: Database) -> None:
    task = await _over_budget(db)
    await _release(db, task.id)
    async with db.read() as conn:
        blocked = await tasks.get_task(conn, task.id)
    async with db.write() as tx:
        edited = await tasks.edit_task(
            tx, task.id, TaskChanges(budget_usd=1.2), blocked.version, "admin:test"
        )
    assert (edited.state, edited.block_reason) == ("blocked", "budget_exhausted")


# -- setting a budget (3.4) -------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, 10_000.01])
def test_budget_bounds(bad: float) -> None:
    with pytest.raises(ValidationError):
        TaskCreate(project="demo", title="t", budget_usd=bad)
    with pytest.raises(ValidationError):
        TaskChanges(budget_usd=bad)
    assert TaskCreate(project="demo", title="t", budget_usd=10_000).budget_usd == 10_000


def test_default_budget_explicit_null_and_project_override(
    client: TestClient,
) -> None:
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    def made(**kw) -> float | None:
        res = client.post(
            "/api/tasks", json={"project": "demo", "title": "t", **kw}, headers=admin
        )
        assert res.status_code == 201, res.text
        return res.json()["budget_usd"]

    assert made() is None
    res = client.put(
        "/api/settings/task_default_budget_usd", json={"value": 3}, headers=admin
    )
    assert res.status_code == 200, res.text
    assert made() == 3
    assert made(budget_usd=None) is None
    assert made(budget_usd=7) == 7
    res = client.put(
        "/api/projects/demo/settings/task_default_budget_usd",
        json={"value": 4.5},
        headers=admin,
    )
    assert res.status_code == 200, res.text
    assert made() == 4.5
    bad = client.put(
        "/api/settings/task_default_budget_usd", json={"value": 0}, headers=admin
    )
    assert bad.status_code == 422


# -- reading over REST and MCP (3.2) -------------------------------------------------


def test_rest_and_mcp_read_with_the_task_scope(client: TestClient) -> None:
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    created = tool(
        client, ADMIN_TOKEN, "create_task", title="t", project="demo", budget_usd=2.5
    )
    assert created["budget_usd"] == 2.5
    worker = register(client)
    other = register(client)
    res = client.get(f"/api/tasks/{created['id']}/cost", headers=admin)
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {
        "task_id",
        "pricing",
        "totals",
        "partial",
        "by_attempt",
        "budget_usd",
        "budget_state",
    }
    assert (body["totals"]["runs"], body["budget_state"]) == (0, "ok")
    assert body["pricing"]["estimate"] is True
    assert tool(client, ADMIN_TOKEN, "get_task_cost", task_id=created["id"]) == body
    # A worker reads only the tasks it has leased.
    assert "error" in tool(client, worker, "get_task_cost", task_id=created["id"])
    tool(client, worker, "claim_task", wait_s=0)
    assert tool(client, worker, "get_task_cost", task_id=created["id"]) == body
    refused = client.get(
        f"/api/tasks/{created['id']}/cost",
        headers={"Authorization": f"Bearer {other}"},
    )
    assert refused.status_code in (403, 404)
    missing = client.get("/api/tasks/nope/cost", headers=admin)
    assert missing.status_code == 404


def test_over_budget_over_rest_blocks_and_the_inbox_offers_a_raise(
    client: TestClient,
) -> None:
    from argus_api.main import app

    app.state.secrets = MemorySecretStore()
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    worker = {"Authorization": f"Bearer {register(client)}"}
    account = client.post(
        "/api/accounts",
        json={
            "name": "acc",
            "provider": "openai",
            "credential": {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-test-not-real"},
        },
        headers=admin,
    )
    assert account.status_code == 201, account.text
    acc = account.json()["id"]
    task = client.post(
        "/api/tasks",
        json={"project": "demo", "title": "t", "budget_usd": 1},
        headers=admin,
    ).json()
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    lease = client.post(
        "/api/accounts/lease",
        json={
            "runtime_providers": ["openai"],
            "role": "engine",
            "task_id": task["id"],
            "attempt": claimed["attempt"],
        },
        headers=worker,
    )
    assert lease.status_code == 200, lease.text
    usage_report = client.post(
        f"/api/accounts/{acc}/usage",
        json={
            "task_id": task["id"],
            "attempt": claimed["attempt"],
            "run_key": "r1",
            "input_tokens": 2 * DOLLAR,
            "model": OPUS,
        },
        headers=worker,
    )
    assert usage_report.status_code == 204, usage_report.text
    cost = client.get(f"/api/tasks/{task['id']}/cost", headers=worker).json()
    assert cost["budget_state"] == "exhausted"
    assert [a["attempt"] for a in cost["by_attempt"]] == [1]
    released = client.post(
        f"/api/tasks/{task['id']}/release", json={"reason": "operator"}, headers=admin
    )
    assert released.json()["block_reason"] == "budget_exhausted", released.text
    page = client.get("/api/inbox", params={"view": "actionable"}, headers=admin).json()
    [item] = [i for i in page["items"] if i["subject_id"] == task["id"]]
    hint = item["actionable"]["next"]
    assert (hint["action"], hint["args"]) == (
        "raise_task_budget",
        {"task_id": task["id"]},
    )
    assert "budget_usd" in hint["reason"] and "cancel_task" in hint["reason"]
    refused = client.post(f"/api/tasks/{task['id']}/unblock", headers=admin)
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "budget_exhausted"
    raised = tool(
        client,
        ADMIN_TOKEN,
        "edit_task",
        task_id=task["id"],
        expected_version=released.json()["version"],
        changes={"budget_usd": 5},
    )
    assert (raised["state"], raised["budget_usd"]) == ("ready", 5)
