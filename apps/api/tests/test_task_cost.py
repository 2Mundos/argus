"""Task cost and budget (docs/specs/task-cost.spec.md)."""

import pytest
import test_mcp
from api_support import DONE, insert_agent
from argus_api.accounts import pricing, usage
from argus_api.accounts import service as accounts
from argus_api.db import Database
from argus_api.errors import ConflictError, ForbiddenError
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import cost as task_cost
from argus_api.tasks import service as tasks
from argus_api.tasks import steers
from argus_core.accounts import AccountCreate, ProvisionalUsageReport, UsageReport
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
        "provisional_cost_usd": None,
        "known_cost_usd": pytest.approx(1.7),
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


# -- provisional executor usage (3.8) -----------------------------------------------


STORE = MemorySecretStore()


async def held_account(db: Database, task) -> str:
    """An account the agent leases for the claimed task; returns its id."""
    await STORE.put("acc", {"api_key": "x"})
    async with db.write() as tx:
        acc = await accounts.create_account(
            tx,
            "acc",
            AccountCreate(
                name="acc", provider="openai", kind="api_key", secret_ref="sops:acc"
            ),
            "sops:acc",
            "admin:test",
            STORE,
        )
    await lease(db, task)
    return acc.id


async def lease(db: Database, task) -> None:
    async with db.write() as tx:
        await accounts.lease(
            tx, A, ["openai"], STORE, "engine", task_id=task.id, attempt=task.attempt
        )


async def provisional(
    db: Database,
    acc: str,
    task_id: str,
    revision: int,
    dollars: float,
    *,
    attempt: int = 1,
    run: str = "r1:t:run-a",
    agent: str = A,
):
    async with db.write() as tx:
        await accounts.record_provisional_usage(
            tx,
            acc,
            agent,
            ProvisionalUsageReport(
                task_id=task_id,
                attempt=attempt,
                run_key=run,
                revision=revision,
                input_tokens=int(dollars * DOLLAR),
                output_tokens=0,
                cached_input_tokens=0,
                model=OPUS,
                engine="claude",
            ),
        )
    return tx


async def final(
    db: Database, acc: str, task_id: str, dollars: float, run: str = "r1:t:run-a"
) -> None:
    async with db.write() as tx:
        await accounts.record_usage(
            tx,
            acc,
            A,
            UsageReport(
                task_id=task_id,
                attempt=1,
                input_tokens=int(dollars * DOLLAR),
                model=OPUS,
                role="executor",
                run_key=run,
            ),
        )


async def test_provisional_revisions_are_monotonic_and_idempotent(
    db: Database,
) -> None:
    task = await create(db, budget_usd=10)
    acc = await held_account(db, await claim(db))
    tx = await provisional(db, acc, task.id, 1, 0.5)
    [event] = [e for e in tx.transient if e.kind == "task.cost"]
    assert event.data["provisional_cost_usd"] == pytest.approx(0.5)
    await provisional(db, acc, task.id, 3, 0.8)
    for stale in (2, 3):  # out of order, then a replay: no write, no event
        tx = await provisional(db, acc, task.id, stale, 2.0)
        assert not [e for e in tx.transient if e.kind == "task.cost"]
    cost = await figures(db, task.id)
    assert cost.totals.runs == 0 and cost.totals.estimated_cost_usd is None
    assert cost.provisional is not None
    assert (cost.provisional.runs, cost.provisional.in_flight_runs) == (1, 1)
    assert cost.provisional.input_tokens == int(0.8 * DOLLAR)
    assert cost.known_cost_usd == pytest.approx(0.8)
    assert (cost.partial, cost.budget_state) == (False, "ok")
    [attempt] = cost.by_attempt
    assert (attempt.attempt, attempt.runs, attempt.models) == (1, 0, [OPUS])
    assert attempt.provisional == cost.provisional


async def test_the_final_row_replaces_the_provisional_one(db: Database) -> None:
    task = await create(db)
    acc = await held_account(db, await claim(db))
    await provisional(db, acc, task.id, 4, 0.8)
    await provisional(db, acc, task.id, 1, 0.3, run="r1:t:run-b")
    await final(db, acc, task.id, 1.0)
    await provisional(db, acc, task.id, 9, 5.0)  # late: the final row stands
    cost = await figures(db, task.id)
    assert cost.totals.estimated_cost_usd == pytest.approx(1.0)
    assert cost.provisional is not None and cost.provisional.runs == 1  # run-b only
    assert cost.known_cost_usd == pytest.approx(1.3)
    await final(db, acc, task.id, 0.4, run="r1:t:run-b")
    cost = await figures(db, task.id)
    assert cost.provisional is None and cost.totals.runs == 2
    assert cost.known_cost_usd == pytest.approx(1.4)


async def test_a_run_with_no_final_row_stays_as_an_interrupted_lower_bound(
    db: Database,
) -> None:
    task = await create(db, max_attempts=3)
    acc = await held_account(db, await claim(db))
    await provisional(db, acc, task.id, 2, 0.6)
    await _release(db, task.id)
    cost = await figures(db, task.id)
    assert cost.provisional is not None
    assert (cost.provisional.in_flight_runs, cost.provisional.interrupted_runs) == (
        0,
        1,
    )
    assert cost.totals.not_reported_runs == 1  # the attempt's placeholder
    assert cost.partial and cost.known_cost_usd == pytest.approx(0.6)
    # A late measured final still replaces it, and upgrades the placeholder.
    async with db.write() as tx:
        await usage.insert_usage(
            tx,
            acc,
            A,
            task.id,
            attempt=1,
            input_tokens=DOLLAR,
            model=OPUS,
            run_key="r1:t:run-a",
        )
    cost = await figures(db, task.id)
    assert cost.provisional is None and cost.totals.measured_runs == 1
    assert (cost.partial, cost.known_cost_usd) == (False, pytest.approx(1.0))


async def test_a_not_reported_final_marks_the_run_interrupted(db: Database) -> None:
    task = await create(db)
    acc = await held_account(db, await claim(db))
    await provisional(db, acc, task.id, 1, 0.6)
    async with db.write() as tx:
        await accounts.record_usage(
            tx,
            acc,
            A,
            UsageReport(
                task_id=task.id,
                attempt=1,
                run_key="r1:t:run-a",
                measurement="not_reported",
                reason="no_usage_in_output",
            ),
        )
    await provisional(db, acc, task.id, 2, 0.7)  # still counted, still interrupted
    cost = await figures(db, task.id)
    assert cost.provisional is not None and cost.provisional.interrupted_runs == 1
    assert cost.partial and cost.known_cost_usd == pytest.approx(0.7)


async def test_provisional_reports_are_fenced_to_the_claim(db: Database) -> None:
    task = await create(db, max_attempts=3)
    acc = await held_account(db, await claim(db))
    await insert_agent(db, "agent-b")
    with pytest.raises(ForbiddenError):  # holds no lease on the account
        await provisional(db, acc, task.id, 1, 0.1, agent="agent-b")
    with pytest.raises(ConflictError) as stale:
        await provisional(db, acc, task.id, 1, 0.1, attempt=2)
    assert stale.value.code == "stale_attempt"
    other = await create(db)
    with pytest.raises(ConflictError) as foreign:
        await provisional(db, acc, other.id, 1, 0.1)
    assert foreign.value.code == "not_claimed"
    async with db.write() as tx:
        await tasks.cancel(tx, other.id, "admin:test")
    await provisional(db, acc, task.id, 1, 0.1)
    await _release(db, task.id)
    retry = await claim(db)
    await lease(db, retry)
    with pytest.raises(ConflictError) as mismatch:  # a reused key, another attempt
        await provisional(db, acc, task.id, 5, 0.1, attempt=retry.attempt)
    assert mismatch.value.code == "run_mismatch"


async def test_provisional_spend_steers_once_and_blocks_a_new_attempt(
    db: Database,
) -> None:
    task = await create(db, budget_usd=1, max_attempts=3)
    acc = await held_account(db, await claim(db))
    await allow_steers(db)
    await provisional(db, acc, task.id, 1, 0.9)
    assert await budget_steers(db, task.id) == []
    await provisional(db, acc, task.id, 2, 1.1)
    await provisional(db, acc, task.id, 3, 1.2)
    await final(db, acc, task.id, 1.3)
    assert await budget_steers(db, task.id) == [(1, "system:budget")]
    assert (await figures(db, task.id)).known_cost_usd == pytest.approx(1.3)
    other = await create(db, budget_usd=1, max_attempts=3)
    await _release(db, task.id)
    await lease(db, await claim(db))
    await provisional(db, acc, other.id, 1, 1.5, run="r1:o:run-a")
    await _release(db, other.id)
    async with db.read() as conn:
        got = await tasks.get_task(conn, other.id)
    assert (got.state, got.block_reason) == ("blocked", "budget_exhausted")


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
        "provisional",
        "known_cost_usd",
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
    running = client.post(
        f"/api/accounts/{acc}/usage/provisional",
        json={
            "task_id": task["id"],
            "attempt": claimed["attempt"],
            "run_key": "r1:t:run",
            "revision": 1,
            "input_tokens": DOLLAR // 2,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "model": OPUS,
        },
        headers=worker,
    )
    assert running.status_code == 204, running.text
    cost = client.get(f"/api/tasks/{task['id']}/cost", headers=worker).json()
    assert cost["provisional"]["in_flight_runs"] == 1
    assert cost["known_cost_usd"] == pytest.approx(0.5)
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
