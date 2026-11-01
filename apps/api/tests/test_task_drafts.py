"""Task drafts and their publication (docs/specs/task-drafts.spec.md)."""

import sqlite3
import threading
from collections.abc import Generator
from pathlib import Path

import pytest
from api_support import FakeClock, insert_agent, insert_project
from argus_api.db import Database
from argus_api.db.migrate import load_migrations
from argus_api.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationFailedError,
)
from argus_api.main import app
from argus_api.mcp_server import audiences
from argus_api.tasks import drafts
from argus_api.tasks import service as tasks
from argus_core.auth import Scope
from argus_core.task_drafts import PublishItem, PublishRequest, TaskDraftBody
from argus_core.tasks import TaskCreate
from argus_core.tasks import TaskState as S
from fastapi.testclient import TestClient
from test_mcp import ADMIN_TOKEN, listed, register, tool

A = "agent-a"
ACTOR = "admin:test"
DRAFT_TOOLS = {
    "create_task_draft",
    "list_task_drafts",
    "get_task_draft",
    "edit_task_draft",
    "discard_task_draft",
    "publish_task_drafts",
}


def complete(**kw) -> dict:
    return {
        "project": "demo",
        "title": "t",
        "spec": "do it",
        "acceptance": ["done"],
        **kw,
    }


async def new(db: Database, **body):
    async with db.write() as tx:
        return await drafts.create_draft(tx, TaskDraftBody(**body), ACTOR)


async def publish(db: Database, key: str, *batch, mode: str = "atomic"):
    req = PublishRequest(
        idempotency_key=key,
        mode=mode,  # type: ignore[arg-type]
        drafts=[PublishItem(draft_id=d.id, expected_version=d.version) for d in batch],
    )
    async with db.write() as tx:
        return await drafts.publish(tx, req, ACTOR)


async def all_tasks(db: Database) -> list[str]:
    async with db.read() as conn, conn.execute("SELECT id FROM tasks") as cur:
        return [r[0] for r in await cur.fetchall()]


async def deps_of(db: Database, task_id: str) -> list[str]:
    async with db.read() as conn:
        return (await tasks.get_task(conn, task_id)).depends_on


@pytest.fixture(autouse=True)
async def agent(db: Database) -> None:
    await insert_agent(db, A)


# -- drafts are not tasks ------------------------------------------------------


async def test_nothing_is_claimable_or_listed_before_publish(db: Database) -> None:
    root = await new(db, **complete(title="root"))
    await new(db, **complete(title="child", depends_on_drafts=[root.id]))
    await new(db)  # an empty draft is fine until publish
    async with db.write() as tx:
        assert await tasks.claim(tx, A, []) is None
    assert await all_tasks(db) == []
    async with db.read() as conn:
        assert (await drafts.list_drafts(conn)).total == 3


async def test_publish_maps_draft_refs_to_new_task_ids(db: Database) -> None:
    root = await new(db, **complete(title="root"))
    mid = await new(db, **complete(title="mid", depends_on_drafts=[root.id]))
    leaf = await new(db, **complete(title="leaf", depends_on_drafts=[mid.id, root.id]))
    # Listed leaf first: publication still follows dependency order.
    result = await publish(db, "k1", leaf, mid, root)
    by_draft = {o.draft_id: o for o in result.items}
    assert [o.draft_id for o in result.items] == [leaf.id, mid.id, root.id]
    assert {o.outcome for o in result.items} == {"published"}
    assert by_draft[root.id].state is S.READY
    assert by_draft[mid.id].state is S.PENDING
    assert by_draft[leaf.id].state is S.PENDING
    root_t, mid_t = by_draft[root.id].task_id, by_draft[mid.id].task_id
    assert root_t and mid_t
    assert await deps_of(db, mid_t) == [root_t]
    assert sorted(await deps_of(db, by_draft[leaf.id].task_id or "")) == sorted(
        [mid_t, root_t]
    )
    async with db.read() as conn:
        assert (await drafts.list_drafts(conn)).total == 0
        published = await drafts.get_draft(conn, root.id)
    assert published.published_task_id == root_t
    async with db.write() as tx:
        claimed = await tasks.claim(tx, A, [])
        assert claimed is not None and claimed.id == root_t
        assert await tasks.claim(tx, A, []) is None


async def test_depending_on_an_already_published_draft(db: Database) -> None:
    root = await new(db, **complete())
    [first] = (await publish(db, "k1", root)).items
    later = await new(db, **complete(depends_on_drafts=[root.id]))
    [second] = (await publish(db, "k2", later)).items
    assert await deps_of(db, second.task_id or "") == [first.task_id]


async def test_kind_defaults_still_apply_at_publish(db: Database) -> None:
    d = await new(db, **complete())
    [out] = (await publish(db, "k", d)).items
    async with db.read() as conn:
        task = await tasks.get_task(conn, out.task_id or "")
    assert (task.max_attempts, task.kind) == (3, "generic")


# -- idempotency ---------------------------------------------------------------


async def test_a_retry_with_the_same_key_creates_nothing_new(db: Database) -> None:
    root = await new(db, **complete())
    child = await new(db, **complete(depends_on_drafts=[root.id]))
    first = await publish(db, "same", root, child)
    again = await publish(db, "same", root, child)
    assert (first.replayed, again.replayed) == (False, True)
    assert again.items == first.items
    assert len(await all_tasks(db)) == 2


async def test_a_reused_key_for_another_request_is_refused(db: Database) -> None:
    one, two = await new(db, **complete()), await new(db, **complete())
    await publish(db, "key", one)
    with pytest.raises(ConflictError) as exc:
        await publish(db, "key", two)
    assert exc.value.code == "idempotency_key_reused"
    assert len(await all_tasks(db)) == 1


async def test_a_refused_atomic_publish_can_be_retried_with_its_key(
    db: Database,
) -> None:
    d = await new(db, **complete(spec=""))
    with pytest.raises(ValidationFailedError):
        await publish(db, "k", d)
    async with db.write() as tx:
        fixed = await drafts.edit_draft(tx, d.id, {"spec": "s"}, d.version, ACTOR)
    [out] = (await publish(db, "k", fixed)).items
    assert out.outcome == "published"


async def test_publishing_a_published_draft_again_under_a_new_key_is_refused(
    db: Database,
) -> None:
    d = await new(db, **complete())
    await publish(db, "k1", d)
    with pytest.raises(ConflictError) as exc:
        await publish(db, "k2", d)
    assert exc.value.code == "draft_published"
    assert len(await all_tasks(db)) == 1


# -- validation ----------------------------------------------------------------


async def test_cycles_are_refused_with_their_path(db: Database) -> None:
    a = await new(db, **complete())
    b = await new(db, **complete(depends_on_drafts=[a.id]))
    async with db.write() as tx:
        a = await drafts.edit_draft(
            tx, a.id, {"depends_on_drafts": [b.id]}, a.version, ACTOR
        )
    with pytest.raises(ValidationFailedError) as exc:
        await publish(db, "k", a, b)
    assert exc.value.code == "dependency_cycle"
    assert any(f"{a.id} -> {b.id} -> {a.id}" in d for d in exc.value.details)
    assert await all_tasks(db) == []


async def test_a_self_reference_is_a_cycle(db: Database) -> None:
    d = await new(db, **complete())
    async with db.write() as tx:
        d = await drafts.edit_draft(
            tx, d.id, {"depends_on_drafts": [d.id]}, d.version, ACTOR
        )
    result = await publish(db, "k", d, mode="partial")
    assert (result.items[0].outcome, result.items[0].code) == (
        "rejected",
        "dependency_cycle",
    )


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"project": "demo", "title": "t", "acceptance": ["a"]}, "validation_failed"),
        ({"project": "demo", "title": "t", "spec": "s"}, "validation_failed"),
        (complete(acceptance=[" "]), "validation_failed"),
        (complete(title=""), "validation_failed"),
        (complete(depends_on_drafts=["no-such-draft"]), "validation_failed"),
        (complete(depends_on=["no-such-task"]), "not_found"),
        (complete(project="nope"), "unknown_project"),
        (complete(required_capabilities=["gpu"]), "unknown_capability"),
    ],
)
async def test_incomplete_or_invalid_drafts_are_refused(
    db: Database, body: dict, code: str
) -> None:
    d = await new(db, **body)
    result = await publish(db, "p", d, mode="partial")
    [out] = result.items
    assert (out.outcome, out.task_id) == ("rejected", None)
    assert out.code == code, out
    assert out.errors
    assert await all_tasks(db) == []
    with pytest.raises(DomainError):  # atomic refuses the same draft
        await publish(db, "a", d)


async def test_a_declared_capability_is_accepted(db: Database) -> None:
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE agents SET capabilities = '[\"gpu\"]' WHERE id = ?", (A,)
        )
    d = await new(db, **complete(required_capabilities=["gpu"]))
    [out] = (await publish(db, "k", d)).items
    assert out.outcome == "published"


async def test_non_coding_tasks_need_no_repository_fields(db: Database) -> None:
    d = await new(db, **complete(title="Summarize the weekly report"))
    [out] = (await publish(db, "k", d)).items
    assert out.outcome == "published"


def test_unknown_or_invalid_fields_are_refused_on_save() -> None:
    with pytest.raises(ValueError, match="extra"):
        TaskDraftBody.model_validate({"title": "t", "repository": "x"})
    with pytest.raises(ValueError, match="max_attempts"):
        TaskDraftBody.model_validate({"max_attempts": 0})
    with pytest.raises(ValueError, match="at most 100"):
        TaskDraftBody(depends_on=["x"] * 60, depends_on_drafts=["y"] * 41)
    with pytest.raises(ValueError, match="once"):
        PublishRequest(
            idempotency_key="k",
            drafts=[PublishItem(draft_id="d", expected_version="v")] * 2,
        )


# -- atomic and partial --------------------------------------------------------


async def test_atomic_publishes_nothing_when_one_draft_fails(db: Database) -> None:
    good = await new(db, **complete())
    bad = await new(db, **complete(project="nope"))
    with pytest.raises(ValidationFailedError) as exc:
        await publish(db, "k", good, bad)
    assert exc.value.details == [f"{bad.id}: unknown_project: unknown project: nope"]
    assert await all_tasks(db) == []
    async with db.read() as conn:
        assert (await drafts.get_draft(conn, good.id)).published_at is None


async def test_atomic_reports_the_highest_precedence_refusal(db: Database) -> None:
    stale = await new(db, **complete())
    async with db.write() as tx:
        await drafts.edit_draft(tx, stale.id, {"spec": "new"}, stale.version, ACTOR)
    bad = await new(db, **complete(spec=""))
    with pytest.raises(ConflictError) as exc:
        await publish(db, "k", bad, stale)
    assert exc.value.code == "stale_version"
    assert len(exc.value.details) == 2


async def test_partial_publishes_the_valid_and_skips_dependents(
    db: Database,
) -> None:
    good = await new(db, **complete())
    bad = await new(db, **complete(project="nope"))
    child = await new(db, **complete(depends_on_drafts=[bad.id]))
    result = await publish(db, "k", good, bad, child, mode="partial")
    outcomes = [(o.outcome, o.code) for o in result.items]
    assert outcomes == [
        ("published", None),
        ("rejected", "unknown_project"),
        ("skipped", None),
    ]
    assert await all_tasks(db) == [result.items[0].task_id]
    async with db.read() as conn:
        assert (await drafts.list_drafts(conn)).total == 2  # still open, fixable


async def test_unknown_draft_ids_are_reported(db: Database) -> None:
    req = PublishRequest(
        idempotency_key="k",
        drafts=[PublishItem(draft_id="missing", expected_version="v")],
    )
    async with db.write() as tx:
        with pytest.raises(NotFoundError):
            await drafts.publish(tx, req, ACTOR)


# -- versions ------------------------------------------------------------------


async def test_stale_versions_are_refused(db: Database, clock: FakeClock) -> None:
    d = await new(db, title="first")
    async with db.write() as tx:
        edited = await drafts.edit_draft(
            tx, d.id, {"title": "second"}, d.version, ACTOR
        )
    assert edited.version > d.version
    for call in (
        lambda tx: drafts.edit_draft(tx, d.id, {"title": "x"}, d.version, ACTOR),
        lambda tx: drafts.discard_draft(tx, d.id, d.version),
    ):
        async with db.write() as tx:
            with pytest.raises(ConflictError) as exc:
                await call(tx)
        assert (exc.value.code, exc.value.details) == (
            "stale_version",
            [edited.version],
        )
    with pytest.raises(ConflictError) as exc:
        await publish(db, "k", d)
    assert exc.value.code == "stale_version"


async def test_edits_merge_reset_and_skip_no_ops(db: Database) -> None:
    d = await new(db, **complete(priority=5))
    async with db.write() as tx:
        same = await drafts.edit_draft(tx, d.id, {"priority": 5}, d.version, ACTOR)
        assert same.version == d.version  # nothing changed: no write
        reset = await drafts.edit_draft(tx, d.id, {"priority": None}, d.version, ACTOR)
    assert (reset.body.priority, reset.body.spec) == (0, "do it")
    async with db.write() as tx:
        with pytest.raises(ValidationFailedError):
            await drafts.edit_draft(tx, d.id, {"bogus": 1}, reset.version, ACTOR)


async def test_published_drafts_cannot_be_edited_or_discarded(db: Database) -> None:
    d = await new(db, **complete())
    [out] = (await publish(db, "k", d)).items
    for call in (
        lambda tx: drafts.edit_draft(tx, d.id, {"title": "x"}, d.version, ACTOR),
        lambda tx: drafts.discard_draft(tx, d.id, d.version),
    ):
        async with db.write() as tx:
            with pytest.raises(ConflictError) as exc:
                await call(tx)
        assert (exc.value.code, exc.value.details) == ("draft_published", [out.task_id])


async def test_open_drafts_are_limited(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(drafts, "MAX_OPEN_DRAFTS", 2)
    await new(db)
    await new(db)
    with pytest.raises(ConflictError) as exc:
        await new(db)
    assert exc.value.code == "too_many_drafts"


# -- durability and migration --------------------------------------------------


async def test_drafts_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "restart.db"
    db = await Database.open(path)
    d = await new(db, **complete(title="kept"))
    await db.close()
    db = await Database.open(path)
    try:
        async with db.read() as conn:
            again = await drafts.get_draft(conn, d.id)
        assert (again.body.title, again.version) == ("kept", d.version)
        [out] = (await publish(db, "k", again)).items
        assert out.outcome == "published"
    finally:
        await db.close()


async def test_0065_upgrade_keeps_data_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"  # not the `db` fixture's file
    migrations = load_migrations()
    assert migrations[64].name == "0065_task_drafts.sql"
    db = await Database.open(path, migrations=migrations[:64])
    await insert_agent(db, A)
    await insert_project(db)
    async with db.write() as tx:
        # create_task writes a column that a later migration adds; borrow it for the setup
        await tx.conn.execute(
            "ALTER TABLE tasks ADD COLUMN deliverable TEXT NOT NULL DEFAULT 'response'"
        )
        old = await tasks.create_task(
            tx, TaskCreate(project="demo", title="old"), ACTOR
        )
        await tx.conn.execute("ALTER TABLE tasks DROP COLUMN deliverable")
    await db.close()

    for _ in range(2):  # the upgrade, then a reopen that must change nothing
        db = await Database.open(path, migrations=migrations)
        async with db.read() as conn, conn.execute("PRAGMA user_version") as cur:
            assert (await cur.fetchone())[0] == len(migrations)
        assert await all_tasks(db) == [old.id]
        await db.close()

    with sqlite3.connect(path) as raw:  # the SQL itself is safe to run again
        raw.executescript(migrations[64].sql)
    db = await Database.open(path, migrations=migrations)
    try:
        d = await new(db, **complete(depends_on=[old.id]))
        [out] = (await publish(db, "k", d)).items
        assert await deps_of(db, out.task_id or "") == [old.id]
    finally:
        await db.close()


# -- concurrency ---------------------------------------------------------------


async def test_publish_and_claim_interleaved_never_expose_a_partial_batch(
    db: Database,
) -> None:
    """Claims racing publishes (asyncio) never see a child without its edge."""
    import asyncio

    pairs = []
    for i in range(10):
        root = await new(db, **complete(title=f"root {i}"))
        child = await new(
            db, **complete(title=f"child {i}", depends_on_drafts=[root.id])
        )
        pairs.append((root, child))
    claimed: list[str] = []

    async def publisher() -> None:
        for i, (root, child) in enumerate(pairs):
            await publish(db, f"k{i}", child, root)
            await asyncio.sleep(0)

    async def claimer() -> None:
        for _ in range(40):
            async with db.write() as tx:
                task = await tasks.claim(tx, A, [])
            if task is not None:
                claimed.append(task.title)
            await asyncio.sleep(0)

    await asyncio.gather(publisher(), claimer())
    assert all(t.startswith("root") for t in claimed), claimed


# -- HTTP and MCP --------------------------------------------------------------

ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        yield c


def test_draft_tools_are_for_managers_only(client: TestClient) -> None:
    assert set(audiences) >= DRAFT_TOOLS
    for name in DRAFT_TOOLS:
        assert Scope.WORKER not in audiences[name]
    assert not DRAFT_TOOLS & listed(client, register(client))
    assert listed(client, register(client, "orchestrator")) >= DRAFT_TOOLS
    assert listed(client, ADMIN_TOKEN) >= DRAFT_TOOLS


def test_workers_are_refused_every_draft_endpoint(client: TestClient) -> None:
    worker = {"Authorization": f"Bearer {register(client)}"}
    d = client.post("/api/task-drafts", json=complete(), headers=ADMIN).json()
    body = {"idempotency_key": "k", "drafts": [{"draft_id": d["id"]}]}
    for method, url, kw in [
        ("POST", "/api/task-drafts", {"json": complete()}),
        ("GET", "/api/task-drafts", {}),
        ("GET", f"/api/task-drafts/{d['id']}", {}),
        ("PATCH", f"/api/task-drafts/{d['id']}", {"json": {}}),
        ("DELETE", f"/api/task-drafts/{d['id']}", {}),
        ("POST", "/api/task-drafts/publish", {"json": body}),
    ]:
        res = client.request(method, url, headers=worker, **kw)
        assert res.status_code == 403, (method, url, res.text)
    unauth = client.get("/api/task-drafts")
    assert unauth.status_code == 401


def test_rest_round_trip_as_admin(client: TestClient) -> None:
    root = client.post("/api/task-drafts", json=complete(title="r"), headers=ADMIN)
    assert root.status_code == 201, root.text
    r = root.json()
    child = client.post(
        "/api/task-drafts",
        json=complete(title="c", depends_on_drafts=[r["id"]]),
        headers=ADMIN,
    ).json()
    stale = client.patch(
        f"/api/task-drafts/{child['id']}",
        json={"expected_version": "2000-01-01T00:00:00+00:00", "changes": {"x": 1}},
        headers=ADMIN,
    )
    assert (stale.status_code, stale.json()["detail"]["code"]) == (
        409,
        "stale_version",
    )
    body = {
        "idempotency_key": "rest-1",
        "drafts": [
            {"draft_id": child["id"], "expected_version": child["version"]},
            {"draft_id": r["id"], "expected_version": r["version"]},
        ],
    }
    first = client.post("/api/task-drafts/publish", json=body, headers=ADMIN)
    assert first.status_code == 200, first.text
    again = client.post("/api/task-drafts/publish", json=body, headers=ADMIN).json()
    assert again["replayed"] is True
    assert again["items"] == first.json()["items"]
    child_task, root_task = (o["task_id"] for o in first.json()["items"])
    task = client.get(f"/api/tasks/{child_task}", headers=ADMIN).json()
    assert (task["state"], task["depends_on"]) == ("pending", [root_task])
    tasks_page = client.get("/api/tasks", headers=ADMIN).json()
    assert tasks_page["total"] == 2

    loose = client.post("/api/task-drafts", json={}, headers=ADMIN).json()
    gone = client.delete(
        f"/api/task-drafts/{loose['id']}",
        params={"expected_version": loose["version"]},
        headers=ADMIN,
    )
    assert gone.status_code == 204
    assert client.get(f"/api/task-drafts/{loose['id']}", headers=ADMIN).status_code == (
        404
    )


def test_mcp_round_trip_as_orchestrator(client: TestClient) -> None:
    orch = register(client, "orchestrator")
    root = tool(client, orch, "create_task_draft", draft=complete(title="r"))
    child = tool(
        client,
        orch,
        "create_task_draft",
        draft={"title": "c", "depends_on_drafts": [root["id"]]},
    )
    child = tool(
        client,
        orch,
        "edit_task_draft",
        draft_id=child["id"],
        expected_version=child["version"],
        changes={"project": "demo", "spec": "s", "acceptance": ["a"]},
    )
    assert tool(client, orch, "list_task_drafts")["total"] == 2
    items = [
        {"draft_id": d["id"], "expected_version": d["version"]} for d in (root, child)
    ]
    out = tool(
        client, orch, "publish_task_drafts", idempotency_key="mcp-1", drafts=items
    )
    assert [o["outcome"] for o in out["items"]] == ["published", "published"]
    replay = tool(
        client, orch, "publish_task_drafts", idempotency_key="mcp-1", drafts=items
    )
    assert replay["replayed"] is True
    again = tool(client, orch, "get_task_draft", draft_id=child["id"])
    assert again["published_task_id"] == out["items"][1]["task_id"]
    refused = tool(
        client,
        orch,
        "discard_task_draft",
        draft_id=child["id"],
        expected_version=child["version"],
    )
    assert "draft_published" in refused["error"]


def test_concurrent_publish_and_claim_threads(client: TestClient) -> None:
    """Workers claiming in threads while batches publish only ever get roots, and
    a claimed root's batch is whole: its child exists with its edge."""
    workers = [{"Authorization": f"Bearer {register(client)}"} for _ in range(3)]
    batches = []
    for i in range(8):
        root = client.post(
            "/api/task-drafts", json=complete(title=f"root {i}"), headers=ADMIN
        ).json()
        child = client.post(
            "/api/task-drafts",
            json=complete(title=f"child {i}", depends_on_drafts=[root["id"]]),
            headers=ADMIN,
        ).json()
        batches.append((root, child))
    claimed: list[dict] = []
    errors: list[str] = []
    done = threading.Event()

    def publisher() -> None:
        for i, (root, child) in enumerate(batches):
            res = client.post(
                "/api/task-drafts/publish",
                json={
                    "idempotency_key": f"t{i}",
                    "drafts": [
                        {"draft_id": d["id"], "expected_version": d["version"]}
                        for d in (child, root)
                    ],
                },
                headers=ADMIN,
            )
            if res.status_code != 200:
                errors.append(res.text)
        done.set()

    def claimer(headers: dict) -> None:
        while True:
            finished = done.is_set()
            res = client.post("/api/tasks/claim", json={"wait_s": 0}, headers=headers)
            if res.status_code == 200:
                claimed.append(res.json())
            elif res.status_code != 204:
                errors.append(res.text)
            if finished:
                return

    threads = [threading.Thread(target=publisher)] + [
        threading.Thread(target=claimer, args=(w,)) for w in workers
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert claimed, "the claimers saw no task"
    assert all(c["title"].startswith("root") for c in claimed)
    for c in claimed:
        i = int(c["title"].split()[1])
        child = client.get(
            f"/api/task-drafts/{batches[i][1]['id']}", headers=ADMIN
        ).json()
        task = client.get(
            f"/api/tasks/{child['published_task_id']}", headers=ADMIN
        ).json()
        assert task["depends_on"] == [c["id"]]
