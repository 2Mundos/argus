"""The read-only route diagnostic (task models spec 3.5)."""

import pytest
import test_mcp
from api_support import insert_agent
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


async def failed_attempt(db: Database, error: str, error_code: str, **kw):
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
        await tasks.fail(tx, A, task.id, claimed.attempt, error)
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
        None,
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
