"""Conversations and chat transport (docs/specs/internal-orchestrator.spec.md)."""

import os
from collections.abc import AsyncGenerator, Generator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from argus_api import orchestrator
from argus_api.conversations import service as conversations
from argus_api.db import Database, ts
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from argus_core.conversations import ConversationCreate, MessageCreate
from fastapi.testclient import TestClient
from test_launch import ADMIN, agent_token, bearer

ADMIN_TOKEN = ADMIN["Authorization"].split()[1]


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    for name in list(os.environ):
        if name.startswith(("ARGUS_", "DOCKER_HOST")) and not name.startswith(
            "ARGUS_TEST_"
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        app.state.secrets = MemorySecretStore()
        yield c


def test_admin_creates_and_posts_messages(client: TestClient) -> None:
    created = client.post("/api/conversations", json={}, headers=ADMIN)
    assert created.status_code == 200, created.text
    conv = created.json()
    assert conv["kind"] == "chat"
    assert conv["title_source"] == "orchestrator"

    posted = client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"body": "  Fix the login bug  "},
        headers=ADMIN,
    )
    assert posted.status_code == 200, posted.text
    msg = posted.json()
    assert msg["body"] == "Fix the login bug"
    assert msg["state"] == "queued"
    assert msg["author"].startswith("admin:")

    listed = client.get(
        f"/api/conversations/{conv['id']}/messages", headers=ADMIN
    ).json()
    assert len(listed["items"]) == 1


def test_worker_cannot_write_or_list_conversations(client: TestClient) -> None:
    worker = bearer(agent_token(client, "worker"))
    assert client.get("/api/conversations", headers=worker).status_code == 403
    assert client.post("/api/conversations", json={}, headers=worker).status_code == 403


def test_message_limits(client: TestClient) -> None:
    conv = client.post("/api/conversations", json={}, headers=ADMIN).json()
    empty = client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"body": "   "},
        headers=ADMIN,
    )
    assert empty.status_code == 422

    # Rate limit: second message within a second.
    first = client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"body": "one"},
        headers=ADMIN,
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"body": "two"},
        headers=ADMIN,
    )
    assert second.status_code == 429


def test_archive_and_rename(client: TestClient) -> None:
    conv = client.post(
        "/api/conversations", json={"title": "Plan"}, headers=ADMIN
    ).json()
    assert conv["title"] == "Plan"
    assert conv["title_source"] == "admin"

    renamed = client.patch(
        f"/api/conversations/{conv['id']}",
        json={"title": "Plan v2"},
        headers=ADMIN,
    ).json()
    assert renamed["title"] == "Plan v2"

    archived = client.post(
        f"/api/conversations/{conv['id']}/archive", headers=ADMIN
    ).json()
    assert archived["archived_at"] is not None
    assert all(
        c["id"] != conv["id"]
        for c in client.get("/api/conversations", headers=ADMIN).json()["items"]
    )


# -- service-level: claiming, spend cap, task origin ----------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
async def svc(tmp_path: Path) -> AsyncGenerator[tuple[Database, Clock, Path]]:
    clock = Clock()
    db = await Database.open(tmp_path / "argus.db", clock=clock)
    yield db, clock, tmp_path / "models.json"
    await db.close()


async def chat(db: Database, clock: Clock, title: str, text: str = "hi") -> str:
    async with db.write() as tx:
        conv = await conversations.create_conversation(
            tx, ConversationCreate(title=title), "admin:t"
        )
        await conversations.post_human_message(
            tx, conv.id, MessageCreate(body=text), "admin:t"
        )
    return conv.id


async def claim(db: Database, path: Path):
    async with db.write() as tx:
        return await conversations.claim_next(tx, agent_id="a1", path=path)


async def test_body_keeps_line_breaks() -> None:
    text, _ = conversations._clean_body(
        "- a  \r\n- b\n\n\n\n```\nx = 1\n```", limit=100
    )
    assert text == "- a\n- b\n\n```\nx = 1\n```"


async def test_blocked_conversation_does_not_block_others(svc) -> None:
    db, clock, path = svc
    a = await chat(db, clock, "a")
    clock.now += timedelta(seconds=2)
    async with db.write() as tx:
        await conversations.post_human_message(
            tx, a, MessageCreate(body="again"), "admin:t"
        )
    b = await chat(db, clock, "b")
    first = await claim(db, path)
    second = await claim(db, path)
    assert first is not None and second is not None
    assert [first[0].id, second[0].id] == [a, b]  # "again" waits behind a's turn
    assert await claim(db, path) is None


async def test_max_concurrent_turns_and_updates_exempt(svc) -> None:
    db, clock, path = svc
    for name in "abc":
        await chat(db, clock, name)
    async with db.write() as tx:
        updates = await orchestrator.ensure_updates_conversation(tx, "admin:t")
        await conversations.post_human_message(
            tx, updates, MessageCreate(body="inbox"), "admin:t"
        )
    claimed = [await claim(db, path) for _ in range(4)]
    kinds = [c[0].kind if c else None for c in claimed]
    assert kinds.count("chat") == 2  # default max_concurrent_turns
    assert "updates" in kinds
    assert await claim(db, path) is None


async def test_stale_turn_is_given_up_and_unblocks(svc) -> None:
    db, clock, path = svc
    a = await chat(db, clock, "a")
    assert await claim(db, path) is not None
    clock.now += timedelta(seconds=2)
    async with db.write() as tx:
        await conversations.post_human_message(
            tx, a, MessageCreate(body="next"), "admin:t"
        )
    assert await claim(db, path) is None
    clock.now += conversations.TURN_TIMEOUT
    got = await claim(db, path)
    assert got is not None and got[1].body == "next"
    msgs = [m.body for m in await _messages(db, a)]
    assert "The orchestrator did not answer this turn." in msgs


async def _messages(db: Database, conv: str):
    async with db.read() as conn:
        return await conversations.list_messages(conn, conv)


async def _spend(db: Database, conv: str, cost: float | None, measurement: str) -> None:
    async with db.write() as tx:
        await tx.conn.execute(
            "INSERT INTO conversation_usage (conversation_id, cost_usd, model, at,"
            " measurement) VALUES (?, ?, 'm', ?, ?)",
            (conv, cost, ts(tx.now), measurement),
        )


async def test_unpriced_usage_does_not_stop_delivery(svc) -> None:
    db, clock, path = svc
    a = await chat(db, clock, "a")
    await _spend(db, a, None, "unpriced")
    async with db.read() as conn:
        assert not await conversations.daily_cap_reached(conn, clock.now, path)
    assert await claim(db, path) is not None


async def test_cap_queues_and_announces_once_in_updates(svc) -> None:
    db, clock, path = svc
    async with db.write() as tx:
        updates = await orchestrator.ensure_updates_conversation(tx, "admin:t")
    a = await chat(db, clock, "a")
    await _spend(db, a, 5.0, "measured")
    async with db.read() as conn:
        assert await conversations.has_claimable(conn, clock.now, path)  # notice due
    assert await claim(db, path) is None
    async with db.read() as conn:
        assert not await conversations.has_claimable(conn, clock.now, path)
    assert await claim(db, path) is None
    notes = [m for m in await _messages(db, updates) if m.author == "system"]
    assert len(notes) == 1 and notes[0].body.startswith("Daily budget reached")


async def test_task_origin(svc, monkeypatch: pytest.MonkeyPatch) -> None:
    db, clock, path = svc
    async with db.write() as tx:
        updates = await orchestrator.ensure_updates_conversation(tx, "admin:t")

    async def origin() -> str | None:
        async with db.read() as conn:
            return await orchestrator.task_origin(conn, clock.now, "a1")

    assert await origin() is None  # not an internal-orchestrator agent

    async def system_pool(conn, agent_id):
        return "orchestrator"

    monkeypatch.setattr(orchestrator, "agent_system_pool", system_pool)
    assert await origin() == updates  # no turn in flight: the Updates loop
    a = await chat(db, clock, "a")
    assert await claim(db, path) is not None
    assert await origin() == a  # exactly one turn in flight
    await chat(db, clock, "b")
    assert await claim(db, path) is not None
    assert await origin() is None  # ambiguous: no origin rather than a wrong one


def test_enable_and_reset_converge_the_system_pool(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install = tmp_path / "nanoclaw"
    install.mkdir()
    monkeypatch.setattr(
        client.app.state,
        "settings",
        replace(
            client.app.state.settings,
            nanoclaw_install_dir=str(install),
            nanoclaw_instances_root="/tmp/argus-nc-instances",
            nanoclaw_agent_image="nc-agent:1",
        ),
    )

    async def ready(conn, settings, targets) -> bool:
        return False

    monkeypatch.setattr(orchestrator, "pick_sandboxed", ready)

    def desired() -> int:
        pools = client.get("/api/pools", headers=ADMIN).json()
        return next(p for p in pools if p["system"] == "orchestrator")[
            "desired_replicas"
        ]

    put = client.put(
        "/api/settings/orchestrator.enabled", json={"value": True}, headers=ADMIN
    )
    assert put.status_code == 200, put.text
    assert desired() == 1
    reset = client.delete("/api/settings/orchestrator.enabled", headers=ADMIN)
    assert reset.status_code == 200, reset.text
    assert reset.json()["value"] is False
    assert desired() == 0
