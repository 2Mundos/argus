"""OneCLI cleanup (docs/specs/onecli-cleanup.spec.md)."""

import json

import httpx2 as httpx
from argus_api.db import Database, ts
from argus_api.fleet.onecli_cleanup import OneCliCleaner, plan

DEAD = "deadbeef-0000-4000-8000-000000000001"
LIVE = "11223344-0000-4000-8000-000000000002"
# Same pool and first 8 characters as DEAD: the names collide.
TWIN = "deadbeef-9999-4000-8000-000000000003"


def test_plan_matches_only_terminated_instances_by_exact_name() -> None:
    instances = [(DEAD, "w", "terminated"), (LIVE, "w", "running")]
    agents = [
        {"id": "a-dead", "name": "w-deadbeef", "identifier": "ag-1-dead"},
        {"id": "a-live", "name": "w-11223344", "identifier": "ag-2-live"},
        {"id": "a-manual", "name": "my-laptop", "identifier": "ag-3"},
    ]
    secrets = [
        {"id": "s1", "name": "argus-ag-1-dead"},
        {"id": "s2", "name": "argus-ag-1-dead-repository-git"},
        {"id": "s3", "name": "argus-ag-1-deadly"},  # another identifier
        {"id": "s4", "name": "argus-ag-2-live"},
        {"id": "s5", "name": "argus-ag-3"},
    ]
    stale = plan(instances, agents, secrets)
    assert [(a.id, a.name, a.secret_ids) for a in stale] == [
        ("a-dead", "w-deadbeef", ["s1", "s2"])
    ]
    # A colliding name of an instance that is not terminated keeps both.
    assert plan([*instances, (TWIN, "w", "running")], agents, secrets) == []


async def _seed(db: Database) -> None:
    async with db.write() as tx:
        stamp = ts(tx.now)
        await tx.conn.execute(
            "INSERT INTO pools (name, target, runtime, image, capabilities,"
            " desired_replicas, max_replicas, config, status, created_at, updated_at)"
            " VALUES ('w', 'docker', 'plain', 'img', '[]', 1, 5, '{}', 'ok', ?, ?)",
            (stamp, stamp),
        )
        for iid, state in ((DEAD, "terminated"), (LIVE, "running")):
            await tx.conn.execute(
                "INSERT INTO instances"
                " (id, pool, provider_ref, state, created_at, terminated_at)"
                " VALUES (?, 'w', 'ref', ?, ?, ?)",
                (iid, state, stamp, stamp if state == "terminated" else None),
            )


def _vault(fail_secret: str | None = None):
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/agents":
            return httpx.Response(
                200,
                json=[
                    {"id": "a-dead", "name": "w-deadbeef", "identifier": "ag-1"},
                    {"id": "a-live", "name": "w-11223344", "identifier": "ag-2"},
                ],
            )
        if request.method == "GET" and path == "/api/secrets":
            return httpx.Response(
                200,
                json=[
                    {"id": "s1", "name": "argus-ag-1"},
                    {"id": "s2", "name": "argus-ag-1-openai"},
                    {"id": "s3", "name": "argus-ag-2"},
                ],
            )
        if request.method == "DELETE":
            if fail_secret and path.endswith(fail_secret):
                return httpx.Response(500)
            deleted.append(path)
            return httpx.Response(204)
        return httpx.Response(404)

    return httpx.MockTransport(handler), deleted


async def _event(db: Database) -> dict:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT data FROM events WHERE kind = 'onecli.cleanup' ORDER BY id DESC"
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    return json.loads(row[0])


async def test_dry_run_reports_and_deletes_nothing(db: Database) -> None:
    await _seed(db)
    transport, deleted = _vault()
    cleaner = OneCliCleaner(db, "http://onecli.test", transport=transport)
    await cleaner.run_once()
    assert deleted == []
    assert await _event(db) == {
        "dry_run": True,
        "agents": 1,
        "secrets": 2,
        "failed": 0,
        "names": ["w-deadbeef"],
    }


async def test_a_real_run_deletes_secrets_then_the_agent(db: Database) -> None:
    await _seed(db)
    transport, deleted = _vault()
    cleaner = OneCliCleaner(
        db, "http://onecli.test", dry_run=False, transport=transport
    )
    await cleaner.run_once()
    assert deleted == ["/api/secrets/s1", "/api/secrets/s2", "/api/agents/a-dead"]
    assert (await _event(db))["agents"] == 1


async def test_a_failed_secret_delete_keeps_the_agent(db: Database) -> None:
    await _seed(db)
    transport, deleted = _vault(fail_secret="s2")
    cleaner = OneCliCleaner(
        db, "http://onecli.test", dry_run=False, transport=transport
    )
    await cleaner.run_once()
    assert deleted == ["/api/secrets/s1"]  # retried next run, agent kept
    event = await _event(db)
    assert (event["agents"], event["secrets"], event["failed"]) == (0, 1, 1)
