"""Task opt-in and worker delivery of Kubernetes integrations
(docs/specs/integrations.spec.md 3.6, 3.7): only the worker holding the exact
attempt of a task that selected an active, validated, unexpired integration of its
own project gets the token, and every other case fails closed with a code."""

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from api_support import FakeClock, insert_agent, insert_project
from argus_api.accounts import service as accounts
from argus_api.db import Database
from argus_api.errors import (
    ConflictError,
    DomainError,
    ForbiddenError,
    ValidationFailedError,
)
from argus_api.integrations import delivery, service
from argus_api.integrations.delivery import DeliveryRequest, TaskIntegrations
from argus_api.secrets import MemorySecretStore, SecretStoreError
from argus_api.tasks import service as tasks
from argus_api.tasks.service import LeaseConflictError
from argus_core.accounts import AccountCreate
from argus_core.tasks import TaskCreate

A, B = "agent-a", "agent-b"
TOKEN = "k8s-sentinel-Qm4Tz8-never-shown"
ROTATED = "k8s-sentinel-rotated-Hc2Vw6-never-shown"
K8S = {"server": "https://k8s.example.test:6443", "namespace": "team-a"}


@pytest.fixture
def store() -> MemorySecretStore:
    return MemorySecretStore()


async def account(db: Database, store, account_id: str, provider: str) -> None:
    await store.put(account_id, {"api_key": f"key-{account_id}"})
    body = AccountCreate(
        name=account_id, provider=provider, secret_ref=f"sops:{account_id}"
    )
    async with db.write() as tx:
        await accounts.create_account(
            tx, account_id, body, f"sops:{account_id}", "admin:test", store
        )


async def integration(
    db: Database,
    store,
    project: str = "demo",
    *,
    kind: str = "kubernetes",
    name: str = "cluster",
    validated: bool = True,
    **extra,
) -> str:
    config = K8S if kind == "kubernetes" else {"url": "https://mcp.example.test/mcp"}
    body = service.IntegrationCreate.model_validate(
        {
            "kind": kind,
            "name": name,
            "config": config,
            "secret": {"token": TOKEN},
            **extra,
        }
    )
    item = await service.create(db, store, project, body, "admin:test")
    if validated:  # validation itself is covered in test_integrations.py
        async with db.write() as tx:
            await tx.conn.execute(
                "UPDATE integrations SET validation = 'ok' WHERE id = ?", (item.id,)
            )
    return item.id


@pytest.fixture
async def world(db: Database, store: MemorySecretStore) -> str:
    """Agents A and B, projects demo and other, a Claude account, and a validated
    Kubernetes integration of demo; returns its id."""
    for agent in (A, B):
        await insert_agent(db, agent)
    await insert_project(db, "other")  # the db fixture seeds demo
    await account(db, store, "acc-claude", "anthropic")
    return await integration(db, store)


async def make_task(db: Database, *ids: str, project: str = "demo") -> str:
    async with db.write() as tx:
        task = await tasks.create_task(
            tx,
            TaskCreate(project=project, title="deploy", integrations=list(ids)),
            "admin:test",
        )
    return task.id


async def start(db: Database, store, agent: str = A, runtime=("anthropic",)):
    """Claim the next task and lease its engine, as the channel does."""
    async with db.write() as tx:
        task = await tasks.claim(tx, agent, [], runtime_providers=list(runtime))
        assert task is not None
        await accounts.lease(
            tx, agent, list(runtime), store, task_id=task.id, attempt=task.attempt
        )
    return task


async def deliver(
    db: Database,
    store,
    task_id: str,
    integration_id: str,
    attempt: int = 1,
    agent: str = A,
    *,
    version: int | None = None,
    available: bool = True,
):
    async with db.write() as tx:
        return await delivery.deliver(
            tx,
            task_id,
            integration_id,
            DeliveryRequest(attempt=attempt, secret_version=version),
            agent,
            f"agent:{agent}",
            store,
            store_available=available,
        )


async def status(db, store, task_id, attempt=1, agent=A, *, available=True):
    async with db.snapshot() as snap:
        return await delivery.status(
            snap, task_id, attempt, agent, store, store_available=available
        )


async def events(db: Database) -> str:
    async with db.read() as conn, conn.execute("SELECT kind, data FROM events") as cur:
        return json.dumps([tuple(r) for r in await cur.fetchall()])


async def test_the_holder_of_an_opted_in_attempt_gets_the_token(
    db: Database, store: MemorySecretStore, world: str
) -> None:
    task_id = await make_task(db, world)
    await start(db, store)
    [state] = await status(db, store, task_id)
    assert (state.readiness, state.secret_version, state.namespace) == (
        "ready",
        1,
        "team-a",
    )
    cred = await deliver(db, store, task_id, world, version=1)
    assert (cred.token, cred.server, cred.namespace) == (
        TOKEN,
        K8S["server"],
        "team-a",
    )
    assert cred.access == "read_only" and cred.ca_data is None
    audit = await events(db)
    assert "task.integration_delivered" in audit and TOKEN not in audit
    async with db.read() as conn:
        task = await tasks.get_task(conn, task_id)
    assert task.integrations == [world]
    assert TOKEN not in task.model_dump_json()


async def test_without_opt_in_nothing_is_delivered(
    db: Database, store: MemorySecretStore, world: str
) -> None:
    # A capability, title or label naming the cluster is not a grant.
    async with db.write() as tx:
        task = await tasks.create_task(
            tx,
            TaskCreate(
                project="demo",
                title="kubectl get pods in cluster",
                required_capabilities=["kubernetes"],
            ),
            "admin:test",
        )
    assert task.integrations == []
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE agents SET capabilities = '[\"kubernetes\"]' WHERE id = ?", (A,)
        )
        await tasks.claim(tx, A, ["kubernetes"], runtime_providers=["anthropic"])
    assert await status(db, store, task.id) == []
    with pytest.raises(ForbiddenError) as err:
        await deliver(db, store, task.id, world)
    assert err.value.code == "integration_not_selected"


async def test_another_projects_integration_is_never_selected_or_delivered(
    db: Database, store: MemorySecretStore, world: str
) -> None:
    foreign = await integration(db, store, "other")
    with pytest.raises(ValidationFailedError) as err:
        await make_task(db, foreign)
    assert err.value.code == "integration_invalid"
    # Even a row written behind the API's back is filtered by project.
    task_id = await make_task(db)
    async with db.write() as tx:
        await tx.conn.execute(
            "INSERT INTO task_integrations (task_id, integration_id) VALUES (?, ?)",
            (task_id, foreign),
        )
    await start(db, store)
    assert await status(db, store, task_id) == []
    with pytest.raises(ForbiddenError):
        await deliver(db, store, task_id, foreign)


@pytest.mark.parametrize(
    ("kind", "detail"),
    [("mcp", "only kubernetes integrations"), ("revoked", "revoked")],
)
async def test_selection_takes_active_kubernetes_ids_only(
    db: Database, store: MemorySecretStore, world: str, kind: str, detail: str
) -> None:
    if kind == "mcp":
        other = await integration(db, store, kind="mcp", name="docs")
    else:
        other = await integration(db, store, name="old")
        await service.revoke(db, store, "demo", other, "admin:test")
    with pytest.raises(ValidationFailedError) as err:
        await make_task(db, world, other)
    assert err.value.code == "integration_invalid"
    assert detail in json.dumps(err.value.details)
    with pytest.raises(ValidationFailedError):
        await make_task(db, "no-such-id")


async def test_selection_changes_only_before_an_attempt_runs(
    db: Database, store: MemorySecretStore, world: str
) -> None:
    task_id = await make_task(db)
    async with db.read() as conn:
        task = await tasks.get_task(conn, task_id)
    body = TaskIntegrations(expected_version=task.version, integrations=[world])
    async with db.write() as tx:
        task = await delivery.set_selection(tx, task_id, body, "admin:test")
    assert task.integrations == [world]
    assert "task.integrations_changed" in await events(db)
    async with db.write() as tx:  # the version moved: a stale write is refused
        with pytest.raises(ConflictError) as err:
            await delivery.set_selection(tx, task_id, body, "admin:test")
    assert err.value.code == "stale_version"
    await start(db, store)
    async with db.read() as conn:
        task = await tasks.get_task(conn, task_id)
    cleared = TaskIntegrations(expected_version=task.version, integrations=[])
    async with db.write() as tx:
        with pytest.raises(ConflictError) as err:
            await delivery.set_selection(tx, task_id, cleared, "admin:test")
    assert err.value.code == "task_not_editable"


@pytest.mark.parametrize("case", ["other_worker", "old_attempt", "released", "failed"])
async def test_only_the_current_attempt_of_the_assignee_gets_it(
    db: Database, store: MemorySecretStore, world: str, case: str
) -> None:
    task_id = await make_task(db, world)
    await start(db, store)
    agent, attempt, code = A, 1, None
    if case == "other_worker":
        agent, code = B, "not_assignee"
    elif case == "old_attempt":
        async with db.write() as tx:
            await tasks.release(tx, task_id, "admin:test", "moved")
        await start(db, store)
        code = "stale_attempt"
    elif case == "released":
        async with db.write() as tx:
            await tasks.release(tx, task_id, "admin:test", "moved")
        code = "not_claimed"
    else:
        async with db.write() as tx:
            await tasks.fail(tx, A, task_id, 1, "boom")
        code = "not_claimed"
    for call in (
        deliver(db, store, task_id, world, attempt, agent),
        status(db, store, task_id, attempt, agent),
    ):
        with pytest.raises(LeaseConflictError) as err:
            await call
        assert err.value.code == code
    assert "task.integration_delivered" not in await events(db)


@pytest.mark.parametrize(
    ("case", "readiness", "code"),
    [
        ("expired", "expired", "integration_expired"),
        ("revoked", "revoked", "integration_revoked"),
        ("unvalidated", "unvalidated", "integration_unvalidated"),
        ("missing", "secret_missing", "integration_secret_missing"),
        ("store_down", "store_unavailable", "secret_store_unavailable"),
        # The status read may race a rotation, so only delivery checks the version.
        ("store_behind", "ready", "secret_store_unavailable"),
    ],
)
async def test_an_unusable_credential_fails_closed(
    db: Database,
    store: MemorySecretStore,
    clock: FakeClock,
    world: str,
    case: str,
    readiness: str,
    code: str,
) -> None:
    item = world
    if case == "expired":
        await service.rotate(
            db,
            store,
            "demo",
            world,
            service.IntegrationRotate.model_validate(
                {
                    "secret": {"token": TOKEN},
                    "expires_at": clock.now + timedelta(seconds=60),
                }
            ),
            "admin:test",
        )
    if case == "unvalidated":
        item = await integration(db, store, name="fresh", validated=False)
    task_id = await make_task(db, item)
    await start(db, store)
    if case == "expired":
        clock.advance(60)  # inside the two-minute lease
    elif case == "revoked":
        await service.revoke(db, store, "demo", item, "admin:test")
    elif case == "missing":
        await store.delete(f"integration-{item}")
    elif case == "store_behind":
        await store.put(f"integration-{item}", {"token": TOKEN, "version": 7})
    available = case != "store_down"
    [state] = await status(db, store, task_id, available=available)
    assert state.readiness == readiness
    with pytest.raises(DomainError) as err:
        await deliver(db, store, task_id, item, available=available)
    assert err.value.code == code
    assert TOKEN not in str(err.value) and TOKEN not in json.dumps(err.value.details)
    if case == "store_down":
        assert isinstance(err.value, SecretStoreError)
    assert "task.integration_delivered" not in await events(db)


async def test_a_rotation_between_status_and_delivery_is_refused(
    db: Database, store: MemorySecretStore, world: str
) -> None:
    task_id = await make_task(db, world)
    await start(db, store)
    [seen] = await status(db, store, task_id)
    await service.rotate(
        db,
        store,
        "demo",
        world,
        service.IntegrationRotate.model_validate({"secret": {"token": ROTATED}}),
        "admin:test",
    )
    # Rotation clears validation: the new token is not delivered until checked.
    [now] = await status(db, store, task_id)
    assert (now.secret_version, now.readiness) == (2, "unvalidated")
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE integrations SET validation = 'ok' WHERE id = ?", (world,)
        )
    with pytest.raises(ConflictError) as err:
        await deliver(db, store, task_id, world, version=seen.secret_version)
    assert err.value.code == "integration_changed"
    cred = await deliver(db, store, task_id, world, version=2)
    assert (cred.token, cred.secret_version) == (ROTATED, 2)


async def test_a_config_edit_fences_the_delivered_attempt(
    db: Database, store: MemorySecretStore, world: str
) -> None:
    task_id = await make_task(db, world)
    await start(db, store)
    first = await deliver(db, store, task_id, world)
    assert (first.config_version, first.namespace) == (1, "team-a")
    item = await service.get(db, "demo", world)
    await service.update(
        db,
        "demo",
        world,
        service.IntegrationUpdate(
            expected_version=item.version, config={**K8S, "namespace": "team-b"}
        ),
        "admin:test",
    )
    # Not deliverable until validated again; the token and its version are kept.
    [now] = await status(db, store, task_id)
    assert (now.readiness, now.secret_version, now.config_version) == (
        "unvalidated",
        1,
        2,
    )
    with pytest.raises(ConflictError) as err:
        await deliver(db, store, task_id, world, version=1)
    assert err.value.code == "integration_unvalidated"
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE integrations SET validation = 'ok' WHERE id = ?", (world,)
        )
    # Re-validated at the same secret_version, the new config_version still tells
    # the channel that what it installed is stale.
    [now] = await status(db, store, task_id)
    assert (now.readiness, now.config_version, now.namespace) == ("ready", 2, "team-b")
    cred = await deliver(db, store, task_id, world, version=1)
    assert (cred.token, cred.config_version, cred.namespace) == (TOKEN, 2, "team-b")
    assert '\\"config_version\\": 2' in await events(db)


async def test_engines_that_cannot_be_confined_are_refused(
    db: Database, store: MemorySecretStore, world: str
) -> None:
    await account(db, store, "acc-codex", "openai")
    task_id = await make_task(db, world)
    await start(db, store, runtime=("openai",))
    with pytest.raises(ConflictError) as err:
        await deliver(db, store, task_id, world)
    assert err.value.code == "integration_engine_unsupported"
    assert "task.integration_delivered" not in await events(db)


def _jwt(exp: datetime) -> str:
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part({'exp': int(exp.timestamp())})}.sig"


@pytest.mark.parametrize(
    ("given", "token_exp", "expected"),
    [
        (None, None, None),
        (timedelta(days=30), None, timedelta(days=30)),
        (None, timedelta(days=10), timedelta(days=10)),
        (timedelta(days=30), timedelta(days=10), timedelta(days=10)),
        (timedelta(days=5), timedelta(days=10), timedelta(days=5)),
        (timedelta(days=-1), None, "expires_at_past"),
        (None, timedelta(days=-1), "expires_at_past"),
    ],
)
def test_expiry_is_the_earlier_of_given_and_the_tokens_own(
    given, token_exp, expected
) -> None:
    now = datetime(2026, 11, 1, tzinfo=UTC)
    token = _jwt(now + token_exp) if token_exp is not None else TOKEN
    at = now + given if given is not None else None
    if isinstance(expected, str):
        with pytest.raises(ValidationFailedError) as err:
            service.expiry(at, token, now)
        assert err.value.code == expected
    else:
        want = None if expected is None else now + expected
        got = service.expiry(at, token, now)
        assert (datetime.fromisoformat(got) if got else None) == want
    with pytest.raises(ValidationFailedError) as err:
        service.expiry(datetime(2026, 12, 1), TOKEN, now)  # naive
    assert err.value.code == "expires_at_invalid"
