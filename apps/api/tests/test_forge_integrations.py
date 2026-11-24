"""Forge integrations (docs/specs/forge-integrations.spec.md): one per project,
bound to the host of its repository, validated against that repository, delivered
to every repository-backed attempt without selection, and moved from the old
`gitea` and `github` accounts at start."""

import json
from datetime import timedelta

import httpx2 as httpx
import pytest
from api_support import insert_agent, insert_project
from argus_api.accounts import service as accounts
from argus_api.db import Database, ts
from argus_api.errors import ConflictError, ValidationFailedError
from argus_api.integrations import delivery, forge_move, service
from argus_api.integrations.delivery import DeliveryRequest
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import service as tasks
from argus_api.tasks.service import LeaseConflictError
from argus_core.accounts import AccountCreate
from argus_core.tasks import TaskCreate

A = "agent-a"
GITEA = "git.example.test"
TOKEN = "forge-sentinel-Kp3Rw7-never-shown"


@pytest.fixture
def store() -> MemorySecretStore:
    return MemorySecretStore()


async def repository(db: Database, project: str, url: str | None) -> None:
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE projects SET repository_url = ? WHERE slug = ?", (url, project)
        )


def body(host: str = GITEA, **extra) -> service.IntegrationCreate:
    config = (
        {"host": host} if host == "github.com" else {"host": host, "username": "bot"}
    )
    return service.IntegrationCreate.model_validate(
        {
            "kind": "forge",
            "name": "forge",
            "config": extra.pop("config", config),
            "secret": {"token": TOKEN},
            **extra,
        }
    )


@pytest.fixture
async def demo(db: Database) -> None:
    """demo's repository is on GITEA; `other` has none."""
    await insert_project(db, "other")
    await repository(db, "demo", f"https://{GITEA}/team/argus")


async def test_create_binds_one_record_to_the_repository_host(
    db: Database, store: MemorySecretStore, demo: None
) -> None:
    for project, create, code in (
        ("other", body(), "project_repository_missing"),
        ("demo", body("github.com"), "forge_host_mismatch"),
        ("demo", body(config={"host": GITEA}), "config_invalid"),  # Gitea: username
        ("demo", body(config={"host": GITEA, "username": "a:b"}), "config_invalid"),
        (
            "demo",
            body(config={"host": "github.com", "username": "x"}),
            "config_invalid",
        ),
    ):
        with pytest.raises(ValidationFailedError) as err:
            await service.create(db, store, project, create, "admin:test")
        assert err.value.code == code, (project, create.config)
    item = await service.create(db, store, "demo", body(), "admin:test")
    assert (item.access, item.config) == (
        "read_write",
        {"host": GITEA, "username": "bot"},
    )
    with pytest.raises(ConflictError) as err:
        await service.create(db, store, "demo", body(name="second"), "admin:test")
    assert err.value.code == "forge_exists"
    with pytest.raises(ValidationFailedError) as err:
        await service.update(
            db,
            "demo",
            item.id,
            service.IntegrationUpdate(
                expected_version=item.version,
                config={"host": "other.example.test", "username": "bot"},
            ),
            "admin:test",
        )
    assert err.value.code == "config_invalid"
    # A revoked record frees the slot.
    await service.revoke(db, store, "demo", item.id, "admin:test")
    await service.create(db, store, "demo", body(name="again"), "admin:test")


def forge(
    status: int, answer: object, seen: list[httpx.Request]
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=answer)

    return httpx.MockTransport(handler)


async def _public(host: str, port: int) -> list[str]:
    return ["203.0.113.10"]


@pytest.mark.parametrize(
    ("access", "status", "answer", "result", "detail"),
    [
        (
            "read_write",
            200,
            {"full_name": "Team/Argus", "permissions": {"pull": True, "push": True}},
            "ok",
            "team/argus on git.example.test: read write access",
        ),
        (
            "read_only",
            200,
            {"full_name": "team/argus", "permissions": {"pull": True, "push": False}},
            "ok",
            "team/argus on git.example.test: read only access",
        ),
        (
            "read_write",
            200,
            {"full_name": "team/argus", "permissions": {"pull": True, "push": False}},
            "failed",
            "token lacks push access",
        ),
        (
            "read_only",
            200,
            {"full_name": "team/argus"},  # a public answer proves nothing
            "failed",
            "cannot prove the token was accepted",
        ),
        (
            "read_only",
            200,
            {"full_name": "team/other", "permissions": {"pull": True}},
            "failed",
            "the forge answered for another repository",
        ),
        ("read_only", 404, {}, "failed", "repository not found for this token"),
        ("read_only", 401, {"message": TOKEN}, "failed", "token rejected (HTTP 401)"),
    ],
)
async def test_validation_reads_the_projects_repository_with_the_token(
    db: Database,
    store: MemorySecretStore,
    demo: None,
    access: str,
    status: int,
    answer: object,
    result: str,
    detail: str,
) -> None:
    item = await service.create(db, store, "demo", body(access=access), "admin:test")
    seen: list[httpx.Request] = []
    checked = await service.validate(
        db, store, "demo", item.id, "admin:test", forge(status, answer, seen), _public
    )
    assert (checked.validation, checked.validation_detail) == (result, detail)
    [request] = seen
    assert request.headers["host"] == GITEA
    assert request.url.path == "/api/v1/repos/team/argus"
    assert request.headers["authorization"].startswith("Basic ")


async def test_github_validation_goes_to_the_rest_host_with_a_bearer_token(
    db: Database, store: MemorySecretStore, demo: None
) -> None:
    await repository(db, "demo", "https://github.com/2Mundos/argus")
    item = await service.create(db, store, "demo", body("github.com"), "admin:test")
    seen: list[httpx.Request] = []
    answer = {"full_name": "2Mundos/argus", "permissions": {"pull": True, "push": True}}
    checked = await service.validate(
        db, store, "demo", item.id, "admin:test", forge(200, answer, seen), _public
    )
    assert checked.validation == "ok"
    assert seen[0].headers["host"] == "api.github.com"
    assert seen[0].url.path == "/repos/2mundos/argus"
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    # The project moves to another forge: the record no longer fits it.
    await repository(db, "demo", f"https://{GITEA}/team/argus")
    seen.clear()
    checked = await service.validate(
        db, store, "demo", item.id, "admin:test", forge(200, answer, seen), _public
    )
    assert checked.validation == "failed" and seen == []


async def test_a_forge_is_never_a_task_selection(
    db: Database, store: MemorySecretStore, demo: None
) -> None:
    item = await service.create(db, store, "demo", body(), "admin:test")
    with pytest.raises(ValidationFailedError) as err:
        async with db.write() as tx:
            await tasks.create_task(
                tx,
                TaskCreate(project="demo", title="t", integrations=[item.id]),
                "admin:test",
            )
    assert err.value.code == "integration_invalid"


async def claim(db: Database, *capabilities: str) -> tuple[str, int]:
    await insert_agent(db, A)
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE agents SET capabilities = ? WHERE id = ?",
            (json.dumps(list(capabilities)), A),
        )
        created = await tasks.create_task(
            tx,
            TaskCreate(
                project="demo", title="t", required_capabilities=list(capabilities)
            ),
            "admin:test",
        )
        task = await tasks.claim(tx, A, list(capabilities), runtime_providers=["x"])
    assert task is not None and task.id == created.id
    return task.id, task.attempt


async def credential(db: Database, store, task_id: str, attempt: int, **kw):
    async with db.write() as tx:
        return await delivery.repository_credential(
            tx,
            task_id,
            DeliveryRequest(attempt=attempt, secret_version=kw.get("version")),
            A,
            f"agent:{A}",
            store,
            store_available=kw.get("available", True),
        )


async def test_every_repository_attempt_gets_the_projects_forge(
    db: Database, store: MemorySecretStore, demo: None
) -> None:
    task_id, attempt = await claim(db, "repository:write")
    with pytest.raises(ConflictError) as err:
        await credential(db, store, task_id, attempt)
    assert err.value.code == "forge_missing" and GITEA in err.value.message
    item = await service.create(db, store, "demo", body(), "admin:test")
    # Unchecked is delivered; the attempt's preflight proves access.
    cred = await credential(db, store, task_id, attempt, version=1)
    assert (cred.provider, cred.host, cred.username, cred.token, cred.access) == (
        "gitea",
        GITEA,
        "bot",
        TOKEN,
        "read_write",
    )
    async with db.read() as conn, conn.execute("SELECT kind, data FROM events") as cur:
        audit = json.dumps([tuple(r) for r in await cur.fetchall()])
    assert "task.integration_delivered" in audit and TOKEN not in audit
    with pytest.raises(LeaseConflictError):
        await credential(db, store, task_id, attempt + 1)
    with pytest.raises(ConflictError) as err:
        await credential(db, store, task_id, attempt, version=7)
    assert err.value.code == "integration_changed"
    # Each refusal, in the order the route checks.
    cases = [
        ("UPDATE integrations SET validation = 'failed'", "integration_unvalidated"),
        ("UPDATE integrations SET access = 'read_only'", "forge_read_only"),
    ]
    for sql, code in cases:
        async with db.write() as tx:
            await tx.conn.execute(sql)
        with pytest.raises(ConflictError) as err:
            await credential(db, store, task_id, attempt)
        assert err.value.code == code
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE integrations SET access = 'read_write', validation = 'ok',"
            " expires_at = ?",
            (ts(tx.now - timedelta(seconds=1)),),
        )
    with pytest.raises(ConflictError) as err:
        await credential(db, store, task_id, attempt)
    assert err.value.code == "integration_expired"
    await service.revoke(db, store, "demo", item.id, "admin:test")
    with pytest.raises(ConflictError) as err:
        await credential(db, store, task_id, attempt)
    assert err.value.code == "forge_missing"


async def test_a_read_only_forge_serves_read_tasks_and_follows_the_repository_host(
    db: Database, store: MemorySecretStore, demo: None
) -> None:
    await service.create(db, store, "demo", body(access="read_only"), "admin:test")
    task_id, attempt = await claim(db)
    assert (await credential(db, store, task_id, attempt)).access == "read_only"
    await repository(db, "demo", "https://other.example.test/team/argus")
    with pytest.raises(ConflictError) as err:
        await credential(db, store, task_id, attempt)
    assert err.value.code == "forge_missing"
    await repository(db, "demo", None)
    with pytest.raises(ConflictError) as err:
        await credential(db, store, task_id, attempt)
    assert err.value.code == "project_repository_missing"


async def forge_account(
    db: Database, store, name: str, provider: str, priority: int, **credential
) -> None:
    await store.put(name, credential)
    async with db.write() as tx:
        await accounts.create_account(
            tx,
            name,
            AccountCreate(
                name=name,
                provider=provider,
                priority=priority,
                secret_ref=f"sops:{name}",
            ),
            f"sops:{name}",
            "admin:test",
            store,
        )


async def test_start_moves_forge_accounts_into_each_projects_integration(
    db: Database, store: MemorySecretStore, demo: None
) -> None:
    await insert_project(db, "gh")
    await insert_project(db, "lonely")
    await repository(db, "gh", "https://github.com/2mundos/argus")
    await repository(db, "lonely", "https://elsewhere.example.test/a/b")
    await forge_account(db, store, "low", "gitea", 1, username="old", token="tok-low")
    await forge_account(db, store, "best", "gitea", 9, username="bot", token=TOKEN)
    await forge_account(db, store, "hub", "github", 0, token="tok-hub")
    await insert_agent(db, A)
    async with db.write() as tx:
        await tx.conn.execute(
            "INSERT INTO account_leases (id, account_id, agent_id, providers,"
            " leased_at, role) VALUES ('l-1', 'best', ?, '[\"gitea\"]', ?,"
            " 'repository')",
            (A, ts(tx.now)),
        )
    await forge_move.move_forge_accounts(db, store)
    await forge_move.move_forge_accounts(db, store)  # a re-run changes nothing
    moved = {i.project: i for i in await service.list_all(db)}
    # Every Gitea host gets the best Gitea token, as the account pool gave it.
    assert set(moved) == {"demo", "gh", "lonely"}
    assert moved["demo"].config == {"host": GITEA, "username": "bot"}
    assert moved["gh"].config == {"host": "github.com"}
    assert (moved["demo"].kind, moved["demo"].access, moved["demo"].validation) == (
        "forge",
        "read_write",
        "unchecked",
    )
    assert store.get(f"integration-{moved['demo'].id}")["token"] == TOKEN
    assert store.get(f"integration-{moved['gh'].id}")["token"] == "tok-hub"
    async with db.read() as conn:
        async with conn.execute(
            "SELECT state, disabled_reason FROM accounts ORDER BY name"
        ) as cur:
            assert {tuple(r) for r in await cur.fetchall()} == {
                ("disabled", forge_move.MOVED)
            }
        async with conn.execute(
            "SELECT released_at IS NOT NULL FROM account_leases"
        ) as cur:
            assert [r[0] for r in await cur.fetchall()] == [1]
        async with conn.execute(
            "SELECT data FROM events WHERE kind = 'integration.created'"
        ) as cur:
            sources = {
                json.loads(r[0]).get("migrated_from") for r in await cur.fetchall()
            }
    assert sources == {"best", "hub"}


async def test_without_a_matching_account_a_project_gets_nothing(
    db: Database, store: MemorySecretStore, demo: None
) -> None:
    await forge_account(db, store, "hub", "github", 0, token="tok-hub")
    await forge_move.move_forge_accounts(db, store)
    assert await service.list_all(db) == []
    async with db.read() as conn, conn.execute("SELECT state FROM accounts") as cur:
        assert [r[0] for r in await cur.fetchall()] == ["disabled"]
