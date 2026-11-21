"""The read-only route diagnostic (task models spec 3.5)."""

import pytest
import test_mcp
from api_support import DONE, insert_agent
from argus_api.accounts import service as accounts
from argus_api.db import Database
from argus_api.modeltrace import service as modeltrace
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import route
from argus_api.tasks import service as tasks
from argus_core.accounts import AccountCreate
from argus_core.model_trace import ExecutionReport
from argus_core.tasks import ExecutorModel, TaskCreate
from fastapi.testclient import TestClient
from test_mcp import ADMIN_TOKEN, listed, register, tool

client = test_mcp.client

A = "agent-a"
WHO = "admin:test"
KEY = "sk-ant-api03-" + "Q" * 40


@pytest.fixture(autouse=True)
async def agent(db: Database) -> None:
    await insert_agent(db, A)


async def account(db: Database, account_id: str, provider: str) -> None:
    store = MemorySecretStore()
    await store.put(account_id, {"api_key": KEY})
    async with db.write() as tx:
        await accounts.create_account(
            tx,
            account_id,
            AccountCreate(
                name=account_id, provider=provider, secret_ref=f"sops:{account_id}"
            ),
            f"sops:{account_id}",
            WHO,
            store,
        )


async def failed_attempt(
    db: Database, error: str, error_code: str, reason: str = "worker_fail", **kw
):
    """A task whose attempt ran the delegate on Anthropic and failed with `error`."""
    async with db.write() as tx:
        task = await tasks.create_task(
            tx, TaskCreate(project="demo", title="t", **kw), WHO
        )
    async with db.write() as tx:
        claimed = await tasks.claim(tx, A, [])
    assert claimed is not None and claimed.id == task.id
    async with db.write() as tx:
        await modeltrace.record_execution(
            tx,
            A,
            task.id,
            claimed.attempt,
            ExecutionReport(
                key="d1",
                layer="delegate",
                state="failed",
                executor="claude",
                provider="anthropic",
                error_code=error_code,
            ),
        )
        await tasks.fail(tx, A, task.id, claimed.attempt, error, reason)
    return task


async def diagnose(db: Database, task_id: str) -> route.TaskRoute:
    async with db.snapshot() as snap:
        return await route.diagnose_route(snap.conn, snap.now, task_id)


async def test_an_available_account_is_not_the_one_the_attempt_used(
    db: Database,
) -> None:
    await account(db, "cur", "cursor")
    task = await failed_attempt(
        db,
        f"argus-delegate: auth_missing for anthropic (key {KEY})",
        "auth_missing",
        executor_model=ExecutorModel(executor="agent", model="composer-2"),
    )
    got = await diagnose(db, task.id)
    providers = {p.provider: p for p in got.providers}
    assert [(a.id, a.status) for a in providers["cursor"].accounts] == [
        ("cur", "usable")
    ]
    assert got.resolution.executor_provider == "cursor"
    assert got.attempt is not None and got.attempt.agent_id == A
    [run] = got.attempt.executions
    assert (run.provider, run.executor, run.error_code) == (
        "anthropic",
        "claude",
        "auth_missing",
    )
    # A usable Cursor account says nothing about whether this agent may use it.
    assert got.worker is not None
    assert got.worker.gateway_access.status == "unknown"
    assert got.worker.executor_binary.status == "unknown"
    assert "does not prove" in got.note
    dumped = got.model_dump_json()
    assert KEY not in dumped and "sops:" not in dumped and "api_key" not in dumped


@pytest.mark.parametrize(
    ("error", "code", "field", "status"),
    [
        (
            "OneCLI gateway 401 access_restricted: agent may not use cursor",
            "auth_missing",
            "gateway_access",
            "denied",
        ),
        ("agent: command not found", "not_installed", "executor_binary", "missing"),
    ],
)
async def test_reported_gateway_and_binary_failures_are_observed(
    db: Database, error: str, code: str, field: str, status: str
) -> None:
    await account(db, "cur", "cursor")
    task = await failed_attempt(db, error, code)
    got = await diagnose(db, task.id)
    assert got.worker is not None
    seen = getattr(got.worker, field)
    assert seen.status == status and seen.evidence
    other = "executor_binary" if field == "gateway_access" else "gateway_access"
    assert getattr(got.worker, other).status == "unknown"


async def test_copilot_is_unsupported_and_restriction_applies_to_engines(
    db: Database,
) -> None:
    await account(db, "cop", "copilot")
    async with db.write() as tx:
        task = await tasks.create_task(
            tx,
            TaskCreate(project="demo", title="t", account_providers=["openai"]),
            WHO,
        )
    got = await diagnose(db, task.id)
    providers = {p.provider: p for p in got.providers}
    copilot = providers["copilot"]
    assert (copilot.engine, copilot.executor, copilot.engine_allowed) == (
        "unsupported",
        "copilot",
        False,
    )
    assert [a.id for a in copilot.accounts] == ["cop"]
    assert providers["openai"].engine_allowed
    assert not providers["anthropic"].engine_allowed
    assert providers["cursor"].executor == "agent"
    assert got.resolution.engine_providers == ["openai"]
    assert got.attempt is None and got.worker is None


def test_route_is_manager_only_on_rest_and_mcp(client: TestClient) -> None:
    created = client.post(
        "/api/tasks",
        json={"project": "demo", "title": "t"},
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    ).json()
    path = f"/api/tasks/{created['id']}/route"
    worker = register(client, "worker")
    denied = client.get(path, headers={"Authorization": f"Bearer {worker}"})
    assert denied.status_code == 403
    assert "get_task_route" not in listed(client, worker)
    orchestrator = register(client, "orchestrator")
    rest = client.get(path, headers={"Authorization": f"Bearer {orchestrator}"})
    assert rest.status_code == 200, rest.text
    assert rest.json()["state"] == "ready"
    via_mcp = tool(client, orchestrator, "get_task_route", task_id=created["id"])
    assert via_mcp == rest.json()
    missing = client.get(
        "/api/tasks/ghost/route", headers={"Authorization": f"Bearer {orchestrator}"}
    )
    assert missing.status_code == 404


async def test_a_blocking_credential_error_is_current(db: Database) -> None:
    task = await failed_attempt(db, f"auth_missing (key {KEY})", "auth_missing")
    async with db.snapshot() as snap:
        blocked = await tasks.get_task(snap.conn, task.id)
    assert (blocked.state, blocked.error_attempt, blocked.error_scope) == (
        "blocked",
        1,
        "current",
    )
    got = await diagnose(db, task.id)
    assert got.error and got.previous_failure is None


async def test_a_previous_attempts_error_is_not_presented_as_current(
    db: Database,
) -> None:
    task = await failed_attempt(
        db,
        "connection reset by peer",
        "channel_error",
        "session_lost",
        max_attempts=5,
    )
    # Failed once and requeued: the error is the last attempt's, not a current one.
    async with db.snapshot() as snap:
        ready = await tasks.get_task(snap.conn, task.id)
    assert ready.state == "ready"
    assert (ready.error, ready.error_attempt, ready.error_scope) == (
        "connection reset by peer",
        1,
        "previous",
    )
    assert ready.error_at is not None
    got = await diagnose(db, task.id)
    assert got.error is None and got.failure_class is None
    assert got.previous_failure is not None
    assert got.previous_failure.attempt == 1
    assert got.previous_failure.error == "connection reset by peer"
    assert got.previous_failure.at == ready.error_at
    # A new claim: attempt 2 has not failed; attempt 1's evidence is still there.
    async with db.write() as tx:
        claimed = await tasks.claim(tx, A, [])
    assert claimed is not None and claimed.attempt == 2
    assert (claimed.error_attempt, claimed.error_scope) == (1, "previous")
    got = await diagnose(db, task.id)
    assert got.state == "claimed"
    assert got.error is None and got.failure_class is None
    assert got.worker is not None and got.worker.gateway_access.status == "unknown"
    assert got.previous_failure is not None and got.previous_failure.attempt == 1
    # Attempt 2 fails for good: its own error is the current one, attributed to it.
    async with db.write() as tx:
        failed = await tasks.fail(
            tx, A, task.id, 2, "second attempt: out of memory", "session_lost"
        )
    assert (failed.state, failed.error_attempt, failed.error_scope) == (
        "failed",
        2,
        "current",
    )
    got = await diagnose(db, task.id)
    assert got.error == "second attempt: out of memory"
    assert got.previous_failure is None


async def test_a_later_attempt_that_completes_keeps_the_old_error_previous(
    db: Database,
) -> None:
    task = await failed_attempt(
        db, "connection reset by peer", "channel_error", "session_lost", max_attempts=5
    )
    async with db.write() as tx:
        claimed = await tasks.claim(tx, A, [])
    assert claimed is not None and claimed.attempt == 2
    async with db.write() as tx:
        done = await tasks.complete(tx, A, task.id, 2, DONE)
    assert done.attempt == 2 and done.error_attempt == 1
    assert done.error_scope == "previous"
    got = await diagnose(db, task.id)
    assert got.error is None and got.failure_class is None
    assert got.previous_failure is not None and got.previous_failure.attempt == 1


async def second_attempt(db: Database, error: str):
    """A task whose attempt 1 failed with `error` and whose attempt 2 is claimed."""
    task = await failed_attempt(
        db, error, "channel_error", "session_lost", max_attempts=5
    )
    async with db.write() as tx:
        claimed = await tasks.claim(tx, A, [])
    assert claimed is not None and claimed.attempt == 2
    return task


async def test_an_unrelated_block_keeps_the_earlier_error_previous(
    db: Database,
) -> None:
    task = await second_attempt(db, f"connection reset, key {KEY}")
    async with db.write() as tx:
        blocked = await tasks.block(tx, A, task.id, 2, "needs_input")
    assert (blocked.state, blocked.error_attempt) == ("blocked", 1)
    assert blocked.error_scope == "previous"
    got = await diagnose(db, task.id)
    assert got.error is None and got.failure_class is None
    assert got.previous_failure is not None and got.previous_failure.attempt == 1
    assert KEY not in got.previous_failure.error  # redaction survives


async def test_a_block_that_reports_its_own_error_is_current(db: Database) -> None:
    task = await second_attempt(db, "connection reset by peer")
    async with db.write() as tx:
        blocked = await tasks.block(tx, A, task.id, 2, "needs_input", "need creds")
    assert (blocked.error_attempt, blocked.error_scope) == (2, "current")
    got = await diagnose(db, task.id)
    assert got.error == "need creds" and got.previous_failure is None


async def test_an_error_without_provenance_is_current_unless_waiting(
    db: Database,
) -> None:
    task = await second_attempt(db, "connection reset by peer")
    async with db.write() as tx:
        await tasks.complete(tx, A, task.id, 2, DONE)
        await tx.conn.execute(
            "UPDATE tasks SET error_attempt = NULL WHERE id = ?", (task.id,)
        )
    async with db.snapshot() as snap:
        got = await tasks.get_task(snap.conn, task.id)
    assert got.error_attempt is None and got.error_scope == "current"
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET state = 'ready' WHERE id = ?", (task.id,)
        )
    async with db.snapshot() as snap:
        got = await tasks.get_task(snap.conn, task.id)
    assert got.error_scope == "previous"


async def test_a_task_without_errors_has_no_failure_to_distinguish(
    db: Database,
) -> None:
    async with db.write() as tx:
        task = await tasks.create_task(tx, TaskCreate(project="demo", title="t"), WHO)
    async with db.write() as tx:
        claimed = await tasks.claim(tx, A, [])
    assert claimed is not None and claimed.id == task.id
    assert (claimed.error, claimed.error_attempt, claimed.error_scope) == (
        None,
        None,
        None,
    )
    got = await diagnose(db, task.id)
    assert got.error is None and got.previous_failure is None
