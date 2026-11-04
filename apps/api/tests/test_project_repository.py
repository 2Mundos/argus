"""A project's repository over REST: who may set it, what the stats report, and
what a blocked task suggests (docs/specs/knowledge.spec.md, project code
availability)."""

from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api.db import Database
from argus_api.fleet import service as fleet
from argus_api.knowledge import service as kn
from argus_api.main import app
from argus_api.tasks import delivery
from argus_core.fleet import NanoclawOptions, PoolUpsert
from argus_core.knowledge import ProjectUpsert
from fastapi.testclient import TestClient
from test_list_reads import create, ok, register

ADMIN_TOKEN = "test-admin-token-" + "r" * 32
URL = "https://git.example/acme/api"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        yield c


def stats(client: TestClient, slug: str, **kw) -> dict:
    return next(
        s for s in ok(client.get("/api/projects/stats", **kw)) if s["slug"] == slug
    )


def test_only_an_admin_changes_the_repository_but_a_manager_keeps_it(
    client: TestClient,
) -> None:
    orchestrator = register(client, "orchestrator")
    put = client.put
    ok(put("/api/projects/acme", json={"name": "Acme", "repository_url": URL}))
    # An orchestrator may edit the name and description while the URL is unchanged
    # (or omitted), but not change or clear it.
    renamed = {"name": "Acme 2", "description": "d"}
    for body in (renamed, {**renamed, "repository_url": URL}):
        done = ok(put("/api/projects/acme", json=body, headers=orchestrator))
        assert done["repository_url"] == URL and done["name"] == "Acme 2"
    for url in ("https://git.example/acme/other", None):
        res = put(
            "/api/projects/acme",
            json={**renamed, "repository_url": url},
            headers=orchestrator,
        )
        assert res.status_code == 403
        assert res.json()["detail"]["code"] == "repository_admin_only"
    worker = register(client)
    assert put("/api/projects/acme", json=renamed, headers=worker).status_code == 403
    # The admin's change keeps name and description it was sent, and is audited.
    done = ok(put("/api/projects/acme", json={**renamed, "repository_url": None}))
    assert done["repository_url"] is None and done["description"] == "d"


@pytest.mark.parametrize(
    "url",
    [
        "http://git.example/acme/api",
        "https://user:tok@git.example/acme/api",
        "https://git.example/acme",
        "https://git.example/acme/api?token=1",
    ],
)
def test_a_repository_with_credentials_or_a_bad_shape_is_refused(
    client: TestClient, url: str
) -> None:
    res = client.put("/api/projects/acme", json={"name": "A", "repository_url": url})
    assert res.status_code == 422
    assert "acme" not in {p["slug"] for p in client.get("/api/projects").json()}


def test_stats_count_what_the_tables_hold_and_say_where_the_repository_came_from(
    client: TestClient,
) -> None:
    ok(client.put("/api/projects/acme", json={"name": "A", "repository_url": URL}))
    ok(client.put("/api/projects/bare", json={"name": "B"}))
    empty = stats(client, "bare")
    assert empty == {
        "slug": "bare",
        "repository_url": None,
        "repository_source": None,
        "tasks": {},
        "archived_tasks": 0,
        "last_task_activity_at": None,
        "knowledge_pages": 0,
    }
    create(client, "acme")
    done = create(client, "acme")
    ok(client.post(f"/api/tasks/{done['id']}/cancel"))
    ok(client.post(f"/api/tasks/{done['id']}/archive"))
    acme = stats(client, "acme")
    assert acme["repository_url"] == URL and acme["repository_source"] == "project"
    assert acme["tasks"] == {"ready": 1}  # the archived one is counted apart
    assert acme["archived_tasks"] == 1
    assert acme["last_task_activity_at"] is not None
    assert stats(client, "bare") == empty  # other projects are untouched


def test_stats_are_for_managers_and_the_pools_repository_is_labelled(
    client: TestClient,
) -> None:
    worker = register(client)
    assert client.get("/api/projects/stats", headers=worker).status_code == 403
    pool = {
        "runtime": "nanoclaw",
        "image": "i",
        "max_replicas": 1,
        "docker": {"runtime": "runsc"},
        "nanoclaw": {
            "install_dir": "/a",
            "instances_root": "/b",
            "agent_image": "x",
            "repositories": {"web": "https://git.example/a/web"},
        },
    }
    assert client.put("/api/pools/p", json=pool).status_code in (200, 201)
    # An opted-out project (explicit null by an admin) has no record URL, so the
    # pools' agreement is what a worker resolves, and the stats say so.
    ok(client.put("/api/projects/web", json={"name": "W", "repository_url": None}))
    web = stats(client, "web")
    assert web["repository_url"] == "https://git.example/a/web"
    assert web["repository_source"] == "pools"


def test_unconfigured_repository_block_points_an_admin_at_the_project(
    client: TestClient,
) -> None:
    ok(client.put("/api/projects/acme", json={"name": "A"}))
    worker = register(client)
    task = create(client, "acme")
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    res = client.post(
        f"/api/tasks/{task['id']}/block",
        json={"attempt": claimed["attempt"], "reason": "env_repository_unconfigured"},
        headers=worker,
    )
    assert res.status_code == 200, res.text

    def next_for(headers: dict | None) -> dict:
        page = ok(
            client.get("/api/inbox", params={"view": "actionable"}, headers=headers)
            if headers
            else client.get("/api/inbox", params={"view": "actionable"})
        )
        return next(i for i in page["items"] if i["subject_id"] == task["id"])[
            "actionable"
        ]["next"]

    admin = next_for(None)
    assert admin["action"] == "set_project_repository"
    assert admin["args"] == {"task_id": task["id"], "project": "acme"}
    orchestrator = next_for(register(client, "orchestrator"))
    assert orchestrator["action"] == "escalate_to_human"
    assert orchestrator["args"]["project"] == "acme"
    # Fixing the project and unblocking is the whole way back.
    ok(client.put("/api/projects/acme", json={"name": "A", "repository_url": URL}))
    assert ok(client.post(f"/api/tasks/{task['id']}/unblock"))["state"] == "ready"


def test_repository_change_is_admin_only_atomic_and_leaves_name_and_description(
    client: TestClient,
) -> None:
    ok(client.put("/api/projects/acme", json={"name": "Acme", "description": "d"}))
    path = "/api/projects/acme/repository"
    for who in (register(client, "orchestrator"), register(client)):
        res = client.patch(path, json={"repository_url": URL}, headers=who)
        assert res.status_code == 403
    # Someone renames the project; a repository-only change cannot undo that.
    ok(client.put("/api/projects/acme", json={"name": "Renamed", "description": "new"}))
    done = ok(client.patch(path, json={"repository_url": URL}))
    assert (done["name"], done["description"], done["repository_url"]) == (
        "Renamed",
        "new",
        URL,
    )
    cleared = ok(client.patch(path, json={"repository_url": None}))
    assert cleared["repository_url"] is None and cleared["name"] == "Renamed"


@pytest.mark.parametrize(
    ("slug", "body", "status"),
    [
        ("acme", {"repository_url": "https://u:p@git.example/acme/api"}, 422),
        ("acme", {"repository_url": "http://git.example/acme/api"}, 422),
        ("acme", {}, 422),  # omitted is not a silent clear
        ("nope", {"repository_url": URL}, 404),
        ("global", {"repository_url": URL}, 422),
    ],
)
def test_repository_change_refuses_bad_input_without_writing(
    client: TestClient, slug: str, body: dict, status: int
) -> None:
    ok(client.put("/api/projects/acme", json={"name": "A", "repository_url": URL}))
    res = client.patch(f"/api/projects/{slug}/repository", json=body)
    assert res.status_code == status, res.text
    [acme] = [p for p in client.get("/api/projects").json() if p["slug"] == "acme"]
    assert acme["repository_url"] == URL


def pool(repos: dict[str, str]) -> PoolUpsert:
    return PoolUpsert(
        runtime="nanoclaw",
        image="i",
        max_replicas=1,
        nanoclaw=NanoclawOptions(
            install_dir="/a", instances_root="/b", agent_image="x", repositories=repos
        ),
    )


async def test_stats_tell_absent_conflicting_invalid_and_pool_supplied_apart(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db.write() as tx:
        await fleet.upsert_pool(
            tx,
            "one",
            pool(
                {
                    "agreed": "https://Git.example/A/Agreed.git",  # canonicalised
                    "split": "https://git.example/a/x",
                    "legacy": "https://git.example/a/legacy",
                    "both": "https://git.example/a/pool-both",
                }
            ),
            "t",
        )
        await fleet.upsert_pool(
            tx,
            "two",
            pool(
                {
                    "agreed": "https://git.example/a/agreed",
                    "split": "https://git.example/a/y",
                }
            ),
            "t",
        )
        for slug in ("agreed", "split", "legacy", "both", "absent", "own"):
            await kn.upsert_project(
                tx, slug, ProjectUpsert(name=slug, repository_url=None), "t", admin=True
            )
        # Legacy rows that predate validation: the record cannot be used.
        for slug, raw in (("legacy", "https://git.example/a/.git"), ("own", URL)):
            await tx.conn.execute(
                "UPDATE projects SET repository_url = ? WHERE slug = ?", (raw, slug)
            )
        await tx.conn.execute(
            "UPDATE projects SET repository_url = 'https://git.example/b/.git'"
            " WHERE slug = 'both'"
        )

    reads = 0
    real = fleet.list_pools

    async def counting(conn):
        nonlocal reads
        reads += 1
        return await real(conn)

    monkeypatch.setattr(fleet, "list_pools", counting)
    async with db.read() as conn:
        found = {s.slug: s for s in await kn.project_stats(conn)}
    assert reads == 1  # one pool read for every project, not one per project

    def got(slug: str) -> tuple[str | None, str | None]:
        return found[slug].repository_url, found[slug].repository_source

    assert got("agreed") == ("https://git.example/a/agreed", "pools")
    assert got("split") == (None, "conflict")  # pools disagree: Argus picks none
    # An unusable stored value with no pool behind it is invalid, not "absent";
    # one the pools can stand in for resolves from them, like a worker's claim.
    assert got("legacy") == ("https://git.example/a/legacy", "pools")
    assert got("both") == ("https://git.example/a/pool-both", "pools")
    assert got("absent") == (None, None)
    assert got("own") == (URL, "project")
    assert got("global") == (None, None)


async def test_stats_flag_an_invalid_record_nothing_can_replace(db: Database) -> None:
    async with db.write() as tx:
        await kn.upsert_project(
            tx, "old", ProjectUpsert(name="old", repository_url=None), "t", admin=True
        )
        await tx.conn.execute(
            "UPDATE projects SET repository_url = 'https://git.example/a/.git'"
            " WHERE slug = 'old'"
        )
    async with db.read() as conn:
        old = next(s for s in await kn.project_stats(conn) if s.slug == "old")
    assert (old.repository_url, old.repository_source) == (None, "invalid")


LEGACY = "https://git.example/a/.git"  # passes the shape rule, names no repository
CAPS = ["repository:write"]


def force_record(client: TestClient, slug: str, raw: str | None) -> None:
    """Store `raw` as a project's repository, as data from before validation could."""

    async def write() -> None:
        async with client.app.state.db.write() as tx:  # ty: ignore[unresolved-attribute]
            await tx.conn.execute(
                "UPDATE projects SET repository_url = ? WHERE slug = ?", (raw, slug)
            )

    assert client.portal is not None
    client.portal.call(write)


def code_task(client: TestClient, project: str):
    return client.post(
        "/api/tasks",
        json={
            "title": "t",
            "project": project,
            "deliverable": "branch",
            "required_capabilities": CAPS,
        },
    )


def claimed_repository(client: TestClient, project: str) -> str | None:
    task = ok_created(
        client.post("/api/tasks", json={"title": "c", "project": project})
    )
    claimed = client.post("/api/tasks/claim", json={}, headers=register(client))
    assert claimed.json()["id"] == task["id"], claimed.text
    return claimed.json()["project_repository"]


def ok_created(res) -> dict:
    assert res.status_code == 201, res.text
    return res.json()


def test_create_and_claim_agree_that_an_unusable_record_names_no_repository(
    client: TestClient,
) -> None:
    for slug in ("legacy", "neighbor"):
        ok(client.put(f"/api/projects/{slug}", json={"name": slug}))
    force_record(client, "legacy", LEGACY)
    force_record(client, "neighbor", "https://Git.example/A/Neighbor.git")

    refused = code_task(client, "legacy")
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "deliverable_repository"
    assert claimed_repository(client, "legacy") is None
    # A valid record next to it is accepted by both, in one canonical form.
    ok_created(code_task(client, "neighbor"))
    done = client.post(
        "/api/tasks/claim", json={"capabilities": CAPS}, headers=register(client)
    ).json()
    assert done["project_repository"] == "https://git.example/a/neighbor"


def test_a_valid_pool_mapping_still_stands_in_for_an_unusable_record(
    client: TestClient,
) -> None:
    for slug in ("legacy", "pooled"):
        ok(client.put(f"/api/projects/{slug}", json={"name": slug}))
    force_record(client, "legacy", LEGACY)
    force_record(client, "pooled", None)
    pool_body = {
        "runtime": "nanoclaw",
        "image": "i",
        "max_replicas": 1,
        "docker": {"runtime": "runsc"},
        "nanoclaw": {
            "install_dir": "/a",
            "instances_root": "/b",
            "agent_image": "x",
            "repositories": {
                "legacy": "https://git.example/a/fallback",
                "pooled": LEGACY,  # an unusable pool value names nothing either
            },
        },
    }
    assert client.put("/api/pools/p", json=pool_body).status_code in (200, 201)

    ok_created(code_task(client, "legacy"))  # record unusable, pool valid
    claim = client.post(
        "/api/tasks/claim", json={"capabilities": CAPS}, headers=register(client)
    ).json()
    assert claim["project_repository"] == "https://git.example/a/fallback"
    refused = code_task(client, "pooled")
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "deliverable_repository"
    assert claimed_repository(client, "pooled") is None


async def test_delivery_resolves_repositories_only_from_usable_values(
    db: Database,
) -> None:
    async with db.write() as tx:
        await fleet.upsert_pool(
            tx,
            "one",
            pool(
                {
                    "legacy": "https://git.example/a/fallback",
                    "pooled": LEGACY,
                    "both": "https://git.example/a/pool-both",
                }
            ),
            "t",
        )
        for slug in ("legacy", "pooled", "neighbor", "both"):
            await kn.upsert_project(
                tx, slug, ProjectUpsert(name=slug, repository_url=None), "t", admin=True
            )
        for slug, raw in (
            ("legacy", LEGACY),
            ("neighbor", "https://Git.example/A/Neighbor.git"),
            ("both", "https://git.example/b/record"),
        ):
            await tx.conn.execute(
                "UPDATE projects SET repository_url = ? WHERE slug = ?", (raw, slug)
            )
    async with db.read() as conn:
        found = await delivery.repositories(conn)
        urls = {
            slug: await delivery.project_repository_url(conn, slug)
            for slug in ("legacy", "pooled", "neighbor", "both")
        }
    assert found == {
        "legacy": ("git.example", "a", "fallback"),
        "neighbor": ("git.example", "a", "neighbor"),
        "both": ("git.example", "b", "record"),  # the record outranks the pool
    }
    # What delivery trusts is what a claim hands out.
    assert urls == {
        "legacy": "https://git.example/a/fallback",
        "pooled": None,
        "neighbor": "https://git.example/a/neighbor",
        "both": "https://git.example/b/record",
    }
