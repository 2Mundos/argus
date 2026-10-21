from collections.abc import Generator
from datetime import timedelta
from pathlib import Path

import pytest
from api_support import FakeClock, insert_agent
from argus_api.auth import Identity
from argus_api.db import Database
from argus_api.db.migrate import load_migrations
from argus_api.decisions import service as decisions
from argus_api.errors import ConflictError, NotFoundError
from argus_api.knowledge import service as kn
from argus_api.main import app
from argus_api.tasks import service as tasks
from argus_core.auth import Scope
from argus_core.decisions import Ask, DecisionAnswer
from argus_core.knowledge import (
    PageCreate,
    PageEdit,
    PageRestore,
    ProjectUpsert,
    SaveToKnowledge,
)
from argus_core.tasks import TaskCreate
from fastapi.testclient import TestClient

ADMIN = Identity("t-admin", Scope.ADMIN, None, "test")
A = "agent-a"


async def create_page(db: Database, **kw) -> str:
    async with db.write() as tx:
        return await kn.create_page(
            tx, PageCreate(**{"title": "t", **kw}), "admin:test"
        )


async def page(db: Database, page_id: str, clock: FakeClock, **kw):
    async with db.read() as conn:
        return await kn.get_page(conn, page_id, now=clock.now, **kw)


async def search(db: Database, q: str, clock: FakeClock, **kw):
    async with db.read() as conn:
        return await kn.search(conn, q, now=clock.now, **kw)


async def edit(db: Database, page_id: str, base: str, **kw) -> None:
    async with db.write() as tx:
        await kn.edit_page(
            tx,
            page_id,
            PageEdit(base_revision_id=base, **{"title": "t", **kw}),
            "admin:test",
        )


# -- pages and revisions -------------------------------------------------------------


async def test_create_page_is_searchable(db: Database, clock: FakeClock) -> None:
    page_id = await create_page(
        db, title="Deploying to staging", body="Run make deploy-stage.", tags=["ops"]
    )
    p = await page(db, page_id, clock)
    assert (p.slug, p.project, p.revision_number) == (
        "deploying-to-staging",
        "global",
        1,
    )
    [hit] = await search(db, "deploy staging", clock)  # porter stemming: deploying
    assert hit.page_id == page_id
    assert await search(db, "ops", clock)  # tags are indexed


async def test_slug_is_unique_per_project(db: Database) -> None:
    await create_page(db, title="Runbook")
    with pytest.raises(ConflictError) as err:
        await create_page(db, title="Runbook")
    assert err.value.code == "knowledge_slug_taken"


async def test_edit_is_compare_and_swap(db: Database, clock: FakeClock) -> None:
    page_id = await create_page(db, body="v1")
    first = (await page(db, page_id, clock)).revision_id
    await edit(db, page_id, first, body="v2")
    with pytest.raises(ConflictError) as err:  # a concurrent editor still on v1
        await edit(db, page_id, first, body="v2-other")
    assert err.value.code == "knowledge_conflict"
    p = await page(db, page_id, clock)
    assert (p.body, p.revision_number) == ("v2", 2)
    assert (await page(db, page_id, clock, revision=1)).body == "v1"


async def test_restore_is_a_new_revision(db: Database, clock: FakeClock) -> None:
    page_id = await create_page(db, body="original")
    first = (await page(db, page_id, clock)).revision_id
    await edit(db, page_id, first, body="vandalized")
    current = (await page(db, page_id, clock)).revision_id
    async with db.write() as tx:
        await kn.restore(
            tx,
            page_id,
            PageRestore(revision_id=first, base_revision_id=current, reason="revert"),
            "admin:test",
        )
    p = await page(db, page_id, clock)
    assert (p.body, p.revision_number) == ("original", 3)
    assert p.reason.startswith("restore revision 1")
    async with db.read() as conn:
        assert [r.number for r in await kn.list_revisions(conn, page_id)] == [3, 2, 1]


async def test_archive_hides_from_search_and_blocks_edits(
    db: Database, clock: FakeClock
) -> None:
    page_id = await create_page(db, title="Old deploy process")
    async with db.write() as tx:
        await kn.set_archived(tx, page_id, True, "admin:test")
    assert await search(db, "deploy", clock) == []
    with pytest.raises(ConflictError) as err:
        await edit(db, page_id, (await page(db, page_id, clock)).revision_id)
    assert err.value.code == "knowledge_page_archived"
    async with db.write() as tx:
        await kn.set_archived(tx, page_id, False, "admin:test")
    assert await search(db, "deploy", clock)


# -- search ----------------------------------------------------------------------------


async def test_search_input_is_never_fts_syntax(db: Database, clock: FakeClock) -> None:
    await create_page(db, title="Quoting rules")
    for q in ['quoting" OR', "quoting AND (", "NEAR(quoting", "*", "rules^"]:
        await search(db, q, clock)  # must not raise


async def test_stale_pages_rank_after_fresh_ones(
    db: Database, clock: FakeClock
) -> None:
    old = await create_page(db, title="Docker build cache docker docker")
    clock.advance(timedelta(days=31).total_seconds())
    fresh = await create_page(db, title="Docker notes")
    hits = await search(db, "docker", clock)
    assert [h.page_id for h in hits] == [fresh, old]
    assert [h.stale for h in hits] == [False, True]
    async with db.write() as tx:
        await kn.mark_reviewed(tx, old, "admin:test")
    assert not (await page(db, old, clock)).stale


async def test_search_by_project(db: Database, clock: FakeClock) -> None:
    async with db.write() as tx:
        await kn.upsert_project(tx, "web", ProjectUpsert(name="Web"), "admin:test")
    web = await create_page(db, project="web", title="Deploy")
    await create_page(db, title="Deploy everything")
    assert [h.page_id for h in await search(db, "deploy", clock, project="web")] == [
        web
    ]


# -- tasks: projects, pins, references on claim -------------------------------------------


async def test_claim_refs_pinned_first_then_matches(
    db: Database, clock: FakeClock
) -> None:
    await insert_agent(db, A)
    async with db.write() as tx:
        await kn.upsert_project(tx, "web", ProjectUpsert(name="Web"), "admin:test")
    pinned = await create_page(db, title="Coding conventions")
    in_project = await create_page(db, project="web", title="Deploying the web app")
    in_global = await create_page(db, title="Deploy checklist")
    archived = await create_page(db, title="Old deploy")
    async with db.write() as tx:
        await kn.set_archived(tx, archived, True, "admin:test")
        await tasks.create_task(
            tx,
            TaskCreate(title="Deploy the web app", project="web", knowledge=[pinned]),
            "admin:test",
        )
        task = await tasks.claim(tx, A, [])
    assert task is not None
    assert task.knowledge == [pinned]
    async with db.read() as conn:
        refs = await kn.refs_for_task(conn, task, now=clock.now)
    assert [(r.page_id, r.why) for r in refs] == [
        (pinned, "pinned"),
        (in_project, "match"),
        (in_global, "match"),
    ]


async def test_task_project_and_pins_must_exist(db: Database) -> None:
    for body in (
        TaskCreate(title="x", project="nope"),
        TaskCreate(project="global", title="x", knowledge=["nope"]),
    ):
        with pytest.raises(NotFoundError):
            async with db.write() as tx:
                await tasks.create_task(tx, body, "admin:test")


# -- answers saved to knowledge ----------------------------------------------------------------


async def ask_and_answer(db: Database, save: SaveToKnowledge, **task_kw) -> None:
    await insert_agent(db, A)
    async with db.write() as tx:
        await tasks.create_task(
            tx, TaskCreate(project="global", title="t", **task_kw), "admin:test"
        )
        task = await tasks.claim(tx, A, [])
        assert task is not None
        asked = await decisions.ask(
            tx, A, task.id, Ask(attempt=task.attempt, question="Which region?")
        )
        await decisions.answer(
            tx,
            asked.id,
            DecisionAnswer(text="eu-west-1", save_to_knowledge=save),
            ADMIN,
        )


async def test_answer_saved_as_new_page(db: Database, clock: FakeClock) -> None:
    await ask_and_answer(db, SaveToKnowledge(title="Deployment region"))
    [hit] = await search(db, "region", clock)
    p = await page(db, hit.page_id, clock)
    assert p.title == "Deployment region"
    assert "eu-west-1" in p.body
    assert {s.type for s in p.sources} == {"decision", "task"}


async def test_answer_appended_to_existing_page(db: Database, clock: FakeClock) -> None:
    page_id = await create_page(db, title="Infra facts", body="Intro.")
    await ask_and_answer(db, SaveToKnowledge(page_id=page_id))
    p = await page(db, page_id, clock)
    assert p.revision_number == 2
    assert p.body.startswith("Intro.")
    assert "## Which region?" in p.body


# -- migration -----------------------------------------------------------------------------


async def test_0007_keeps_existing_decisions(tmp_path: Path) -> None:
    path, migrations = tmp_path / "argus.db", load_migrations()
    db = await Database.open(path, migrations=migrations[:6])
    async with db.write() as tx:
        await tx.conn.execute(
            "INSERT INTO decisions (id, subject_type, subject_id, kind, question,"
            " audience, state, requested_by, created_at)"
            " VALUES ('d1', 'task', 't', 'question', '?', 'any', 'open', 'x', 'now')"
        )
    await db.close()
    db = await Database.open(path, migrations=migrations)
    async with db.read() as conn, conn.execute("SELECT id FROM decisions") as cur:
        assert [r[0] for r in await cur.fetchall()] == ["d1"]
    await db.close()


# -- HTTP: worker writes -------------------------------------------------------------------------

ADMIN_TOKEN = "test-admin-token-" + "k" * 32
ADMIN_H = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writes: str):
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ARGUS_KNOWLEDGE_WORKER_WRITES", writes)
    return TestClient(app)


def worker(client: TestClient) -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={}, headers=ADMIN_H).json()
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    return {"Authorization": f"Bearer {reg.json()['token']}"}


@pytest.fixture
def proposing(tmp_path, monkeypatch) -> Generator[TestClient]:
    with make_client(tmp_path, monkeypatch, "propose") as c:
        yield c


def test_worker_proposal_goes_through_the_inbox(proposing: TestClient) -> None:
    client, w = proposing, worker(proposing)
    res = client.post(
        "/api/knowledge/pages",
        json={"title": "Flaky test list", "body": "test_x", "reason": "seen twice"},
        headers=w,
    )
    assert res.status_code == 201
    assert res.json()["status"] == "proposed"
    assert client.get("/api/knowledge/pages", headers=ADMIN_H).json() == []

    [decision] = client.get("/api/decisions", headers=ADMIN_H).json()["items"]
    assert decision["kind"] == "knowledge"
    assert decision["subject_title"] == "Flaky test list"
    client.post(
        f"/api/decisions/{decision['id']}/answer",
        json={"choice": "accept"},
        headers=ADMIN_H,
    )
    [listed] = client.get("/api/knowledge/pages", headers=ADMIN_H).json()
    assert listed["title"] == "Flaky test list"
    page = client.get(f"/api/knowledge/pages/{listed['id']}", headers=ADMIN_H).json()
    assert page["author"].startswith("agent:")  # credited to the proposer


def test_accepting_a_stale_edit_proposal_conflicts(proposing: TestClient) -> None:
    client, w = proposing, worker(proposing)
    created = client.post(
        "/api/knowledge/pages", json={"title": "Page", "body": "v1"}, headers=ADMIN_H
    ).json()["page"]
    base = created["revision_id"]
    client.post(
        f"/api/knowledge/pages/{created['id']}/revisions",
        json={"base_revision_id": base, "title": "Page", "body": "worker"},
        headers=w,
    )
    client.post(  # meanwhile an admin edits directly
        f"/api/knowledge/pages/{created['id']}/revisions",
        json={"base_revision_id": base, "title": "Page", "body": "admin"},
        headers=ADMIN_H,
    )
    [decision] = client.get("/api/decisions", headers=ADMIN_H).json()["items"]
    res = client.post(
        f"/api/decisions/{decision['id']}/answer",
        json={"choice": "accept"},
        headers=ADMIN_H,
    )
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "knowledge_conflict"
    assert (
        client.get("/api/decisions", headers=ADMIN_H).json()["total"] == 1
    )  # still open


def test_publish_setting_lets_workers_publish(tmp_path, monkeypatch) -> None:
    with make_client(tmp_path, monkeypatch, "publish") as client:
        res = client.post(
            "/api/knowledge/pages", json={"title": "Direct"}, headers=worker(client)
        ).json()
        assert res["status"] == "published"
        assert res["page"]["title"] == "Direct"


def test_knowledge_worker_writes_defaults_to_propose_when_env_unset(
    tmp_path, monkeypatch
) -> None:
    """Negative: unset ARGUS_KNOWLEDGE_WORKER_WRITES must not publish (stays propose)."""
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.delenv("ARGUS_KNOWLEDGE_WORKER_WRITES", raising=False)
    with TestClient(app) as client:
        res = client.post(
            "/api/knowledge/pages",
            json={"title": "Should propose", "body": "x", "reason": "default"},
            headers=worker(client),
        )
        assert res.status_code == 201
        assert res.json()["status"] == "proposed"
        assert client.get("/api/knowledge/pages", headers=ADMIN_H).json() == []


async def test_kind_pins_merge_with_task_pins(db: Database, clock: FakeClock) -> None:
    from argus_api.kinds import service as kinds
    from argus_core.kinds import TaskKindUpsert

    await insert_agent(db, A)
    runbook = await create_page(db, title="Deploy runbook")
    extra = await create_page(db, title="Release notes template")
    async with db.write() as tx:
        await kinds.upsert_kind(
            tx, "deploy", TaskKindUpsert(default_knowledge=[runbook]), "admin:test"
        )
        task = await tasks.create_task(
            tx,
            TaskCreate(
                project="global", title="Ship it", kind="deploy", knowledge=[extra]
            ),
            "admin:test",
        )
    assert task.knowledge == sorted([runbook, extra])
    with pytest.raises(NotFoundError):
        async with db.write() as tx:
            await kinds.upsert_kind(
                tx, "bad", TaskKindUpsert(default_knowledge=["nope"]), "admin:test"
            )
