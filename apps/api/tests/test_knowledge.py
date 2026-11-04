import json
from collections.abc import Generator
from datetime import timedelta
from pathlib import Path

import pytest
from api_support import FakeClock, insert_agent
from argus_api.auth import Identity
from argus_api.db import Database
from argus_api.db.migrate import load_migrations
from argus_api.decisions import service as decisions
from argus_api.errors import ConflictError, NotFoundError, ValidationFailedError
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
        TaskCreate(project="demo", title="x", knowledge=["nope"]),
    ):
        with pytest.raises(NotFoundError):
            async with db.write() as tx:
                await tasks.create_task(tx, body, "admin:test")


async def test_a_task_pins_only_its_project_or_global(db: Database) -> None:
    async with db.write() as tx:
        await kn.upsert_project(tx, "web", ProjectUpsert(name="Web"), "admin:test")
    foreign = await create_page(db, project="web", title="Web internals")
    shared = await create_page(db, title="Conventions")
    with pytest.raises(ValidationFailedError) as err:
        async with db.write() as tx:
            await tasks.create_task(
                tx,
                TaskCreate(project="demo", title="x", knowledge=[shared, foreign]),
                "admin:test",
            )
    assert err.value.details == [f"knowledge: {foreign}: not in demo or global"]
    async with db.write() as tx:
        task = await tasks.create_task(
            tx,
            TaskCreate(project="web", title="x", knowledge=[shared, foreign]),
            "admin:test",
        )
    assert task.knowledge == sorted([shared, foreign])


# -- answers saved to knowledge ----------------------------------------------------------------


async def ask_and_answer(db: Database, save: SaveToKnowledge, **task_kw) -> None:
    await insert_agent(db, A)
    async with db.write() as tx:
        await tasks.create_task(
            tx, TaskCreate(project="demo", title="t", **task_kw), "admin:test"
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
    async with db.write() as tx:
        await kn.upsert_project(tx, "ops", ProjectUpsert(name="Ops"), "admin:test")
    # The kind's pins are the admin's choice and may live in any project.
    runbook = await create_page(db, project="ops", title="Deploy runbook")
    extra = await create_page(db, title="Release notes template")
    async with db.write() as tx:
        await kinds.upsert_kind(
            tx, "deploy", TaskKindUpsert(default_knowledge=[runbook]), "admin:test"
        )
        task = await tasks.create_task(
            tx,
            TaskCreate(
                project="demo", title="Ship it", kind="deploy", knowledge=[extra]
            ),
            "admin:test",
        )
    assert task.knowledge == sorted([runbook, extra])
    with pytest.raises(NotFoundError):
        async with db.write() as tx:
            await kinds.upsert_kind(
                tx, "bad", TaskKindUpsert(default_knowledge=["nope"]), "admin:test"
            )


@pytest.mark.parametrize(
    "url",
    [
        "http://git.example/acme/api",
        "https://user:tok@git.example/acme/api",
        "https://git.example/acme",
        "https://git.example/acme/api?x=1",
        "git@git.example:acme/api.git",
    ],
)
def test_project_repository_must_be_a_credential_free_https_owner_repo(
    url: str,
) -> None:
    with pytest.raises(ValueError):
        ProjectUpsert(name="P", repository_url=url)


async def test_project_repository_is_kept_unless_set_or_cleared(
    db: Database,
) -> None:
    async def put(**kw) -> object:
        async with db.write() as tx:
            return await kn.upsert_project(
                tx, "p", ProjectUpsert(name="P", **kw), "t", admin=True
            )

    url = "https://git.example/acme/api"
    assert (await put(repository_url=url)).repository_url == url
    assert (await put(description="renamed")).repository_url == url  # omitted: kept
    assert (await put(repository_url=None)).repository_url is None  # explicit: cleared


async def test_non_admin_json_null_on_update_does_not_clear_repository(
    db: Database,
) -> None:
    url = "https://git.example/acme/api"
    async with db.write() as tx:
        await kn.upsert_project(
            tx, "p", ProjectUpsert(name="P", repository_url=url), "t", admin=True
        )
        kept = await kn.upsert_project(
            tx, "p", ProjectUpsert(name="P 2", repository_url=None), "t", admin=False
        )
    assert kept.repository_url == url
    assert kept.name == "P 2"


async def test_0071_inherits_only_repositories_the_pools_agree_on(
    tmp_path: Path,
) -> None:
    from argus_api.tasks import delivery

    path, migrations = tmp_path / "argus.db", load_migrations()
    db = await Database.open(path, migrations=migrations[:70])
    spell_canonical = "https://git.example/a/spell"
    t0 = "2026-10-01T00:00:00+00:00"
    repos = {
        "p1": {
            "agreed": "https://git.example/a/agreed",
            "split": "https://git.example/a/x",
        },
        "p2": {
            "agreed": "https://git.example/a/agreed",
            "split": "https://git.example/a/y",
        },
        "p3": {
            "global": "https://git.example/a/knowledge",
            "solo": "https://git.example/a/solo",
        },
        "p4": None,  # not a NanoClaw pool
        "p5": {"spell": "https://Git.example/A/Spell.git"},
        "p6": {"spell": "https://git.example/a/spell"},
    }
    async with db.write() as tx:
        for slug in (
            "agreed",
            "split",
            "solo",
            "unnamed",
            "global",
            "spell",
        ):
            await tx.conn.execute(
                "INSERT OR IGNORE INTO projects (slug, name, description, created_at, updated_at)"
                " VALUES (?, ?, '', 'now', 'now')",
                (slug, slug),
            )
        for name, mapping in repos.items():
            if mapping is None:
                await tx.conn.execute(
                    "INSERT INTO pools (name, target, runtime, image, capabilities,"
                    " agents_per_instance, desired_replicas, max_replicas, config,"
                    " created_at, updated_at) VALUES (?, 'docker', 'plain', 'i', '[]',"
                    " 1, 0, 1, '{}', ?, ?)",
                    (name, t0, t0),
                )
                continue
            config = {
                "nanoclaw": {
                    "install_dir": "/a",
                    "instances_root": "/b",
                    "agent_image": "x",
                    "repositories": mapping,
                }
            }
            await tx.conn.execute(
                "INSERT INTO pools (name, target, runtime, image, capabilities,"
                " agents_per_instance, desired_replicas, max_replicas, config,"
                " created_at, updated_at) VALUES (?, 'docker', 'nanoclaw', 'i', '[]',"
                " 1, 0, 1, ?, ?, ?)",
                (name, json.dumps(config), t0, t0),
            )
    await db.close()
    db = await Database.open(path, migrations=migrations)
    async with (
        db.read() as conn,
        conn.execute("SELECT slug, repository_url FROM projects") as cur,
    ):
        got = dict(await cur.fetchall())
        for slug, url in got.items():
            if url is None:
                continue
            assert url == delivery.canonical_repository_url(url)
            assert url == await delivery.pool_repository(conn, slug)
    await db.close()
    assert got == {
        "agreed": "https://git.example/a/agreed",
        "solo": "https://git.example/a/solo",
        "spell": spell_canonical,
        "split": None,  # the pools disagree: not guessed
        "unnamed": None,
        "global": None,  # knowledge scope never gets a code repository
    }


async def test_0071_skips_invalid_legacy_pool_urls(tmp_path: Path) -> None:
    path, migrations = tmp_path / "argus.db", load_migrations()
    t0 = "2026-10-01T00:00:00+00:00"
    db = await Database.open(path, migrations=migrations[:70])
    async with db.write() as tx:
        for slug in ("invalid", "creds"):
            await tx.conn.execute(
                "INSERT OR IGNORE INTO projects (slug, name, description, created_at, updated_at)"
                " VALUES (?, ?, '', ?, ?)",
                (slug, slug, t0, t0),
            )
        for name, mapping in (
            ("bad-scheme", {"invalid": "http://git.example/a/b"}),
            ("bad-creds", {"creds": "https://user:tok@git.example/a/b"}),
        ):
            config = json.dumps(
                {
                    "nanoclaw": {
                        "install_dir": "/a",
                        "instances_root": "/b",
                        "agent_image": "x",
                        "repositories": mapping,
                    }
                }
            )
            await tx.conn.execute(
                "INSERT INTO pools (name, target, runtime, image, capabilities,"
                " agents_per_instance, desired_replicas, max_replicas, config,"
                " created_at, updated_at) VALUES (?, 'docker', 'nanoclaw', 'i', '[]',"
                " 1, 0, 1, ?, ?, ?)",
                (name, config, t0, t0),
            )
    await db.close()
    db = await Database.open(path, migrations=migrations)
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT slug, repository_url FROM projects WHERE slug IN ('invalid', 'creds')"
        ) as cur,
    ):
        got = dict(await cur.fetchall())
    await db.close()
    assert got == {"invalid": None, "creds": None}


async def test_0071_semantic_spellings_agree_but_raw_distinct_would_not(
    tmp_path: Path,
) -> None:
    """Regression: raw COUNT(DISTINCT url) treats .git suffix as conflict."""
    from argus_api.tasks import delivery

    path, migrations = tmp_path / "argus.db", load_migrations()
    t0 = "2026-10-01T00:00:00+00:00"
    db = await Database.open(path, migrations=migrations[:70])
    bare = "https://git.example/a/spell"
    suffixed = f"{bare}.git"
    async with db.write() as tx:
        await tx.conn.execute(
            "INSERT OR IGNORE INTO projects (slug, name, description, created_at, updated_at)"
            " VALUES ('spell', 'spell', '', ?, ?)",
            (t0, t0),
        )
        for name, url in (("one", bare), ("two", suffixed)):
            config = json.dumps(
                {
                    "nanoclaw": {
                        "install_dir": "/a",
                        "instances_root": "/b",
                        "agent_image": "x",
                        "repositories": {"spell": url},
                    }
                }
            )
            await tx.conn.execute(
                "INSERT INTO pools (name, target, runtime, image, capabilities,"
                " agents_per_instance, desired_replicas, max_replicas, config,"
                " created_at, updated_at) VALUES (?, 'docker', 'nanoclaw', 'i', '[]',"
                " 1, 0, 1, ?, ?, ?)",
                (name, config, t0, t0),
            )
    await db.close()
    db = await Database.open(path, migrations=migrations)
    async with db.read() as conn:
        async with conn.execute(
            "SELECT repository_url FROM projects WHERE slug = 'spell'"
        ) as cur:
            (url,) = await cur.fetchone()
        assert url == bare
        assert url == await delivery.pool_repository(conn, "spell")
    await db.close()


async def test_a_new_project_inherits_the_pools_agreed_repository_but_never_guesses(
    db: Database,
) -> None:
    from argus_api.fleet import service as fleet
    from argus_core.fleet import NanoclawOptions, PoolUpsert

    def pool(repos: dict[str, str]) -> PoolUpsert:
        return PoolUpsert(
            runtime="nanoclaw",
            image="i",
            max_replicas=1,
            nanoclaw=NanoclawOptions(
                install_dir="/a",
                instances_root="/b",
                agent_image="x",
                repositories=repos,
            ),
        )

    async with db.write() as tx:
        await fleet.upsert_pool(
            tx,
            "one",
            pool(
                {
                    "web": "https://git.example/a/web",
                    "ops": "https://git.example/a/ops",
                    "boot": "https://git.example/a/boot",
                    "legacy": "https://git.example/a/legacy",
                }
            ),
            "t",
        )
        await fleet.upsert_pool(
            tx, "two", pool({"ops": "https://git.example/b/ops"}), "t"
        )
        web = await kn.upsert_project(tx, "web", ProjectUpsert(name="Web"), "t")
        ops = await kn.upsert_project(tx, "ops", ProjectUpsert(name="Ops"), "t")
        none = await kn.upsert_project(tx, "docs", ProjectUpsert(name="Docs"), "t")
        # Explicit null on create bootstraps like an omitted URL; only an admin can
        # opt out on create or clear a stored URL on update.
        boot = await kn.upsert_project(
            tx,
            "boot",
            ProjectUpsert(name="Boot", repository_url=None),
            "t",
            admin=False,
        )
        opted = await kn.upsert_project(
            tx,
            "legacy",
            ProjectUpsert(name="Legacy", repository_url=None),
            "t",
            admin=True,
        )
        cleared = await kn.upsert_project(
            tx,
            "web",
            ProjectUpsert(name="Web", repository_url=None),
            "t",
            admin=True,
        )
    assert web.repository_url == "https://git.example/a/web"
    assert ops.repository_url is None  # pools disagree
    assert none.repository_url is None
    assert boot.repository_url == "https://git.example/a/boot"
    assert opted.repository_url is None
    assert cleared.repository_url is None
