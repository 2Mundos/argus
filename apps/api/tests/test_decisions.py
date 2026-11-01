from collections.abc import Generator
from pathlib import Path

import pytest
from api_support import DONE, FakeClock, insert_agent
from argus_api import orchestrator
from argus_api.auth import Identity
from argus_api.conversations import service as conversations
from argus_api.db import Database
from argus_api.decisions import service as decisions
from argus_api.decisions import store as decision_store
from argus_api.decisions import views as decision_views
from argus_api.errors import ConflictError, ForbiddenError, ValidationFailedError
from argus_api.main import app
from argus_api.settings import service as settings_service
from argus_api.tasks import service as tasks
from argus_core.auth import Scope
from argus_core.decisions import (
    Ask,
    Audience,
    DecisionAnswer,
    DecisionKind,
    DecisionState,
)
from argus_core.tasks import Review, TaskCreate
from argus_core.tasks import TaskState as S
from fastapi.testclient import TestClient

A = "agent-a"
ADMIN = Identity("t-admin", Scope.ADMIN, None, "test")
ORCH = Identity("t-orch", Scope.ORCHESTRATOR, "orch", None)
WORKER = Identity("t-worker", Scope.WORKER, A, None)


@pytest.fixture(autouse=True)
async def agents(db: Database) -> None:
    await insert_agent(db, A)


async def create(db: Database, actor: str = "admin:test", limit=None, **kw):
    async with db.write() as tx:
        return await tasks.create_task(
            tx, TaskCreate(project="demo", title="t", **kw), actor, hourly_limit=limit
        )


async def claim(db: Database):
    async with db.write() as tx:
        task = await tasks.claim(tx, A, [])
    assert task is not None
    return task


async def get_task(db: Database, task_id: str):
    async with db.read() as conn:
        return await tasks.get_task(conn, task_id)


async def open_for(db: Database, task_id: str):
    async with db.snapshot() as snap:
        page = await decision_views.list_decisions(
            snap, subject_id=task_id, view="detail"
        )
    return page.items


async def answer(db: Database, decision_id: str, caller=ADMIN, **kw):
    async with db.write() as tx:
        return await decisions.answer(tx, decision_id, DecisionAnswer(**kw), caller)


# -- questions ---------------------------------------------------------------------


async def test_question_blocks_task_and_answer_comes_back_in_the_spec(
    db: Database,
) -> None:
    await create(db, spec="Deploy it.")
    task = await claim(db)
    async with db.write() as tx:
        asked = await decisions.ask(
            tx, A, task.id, Ask(attempt=task.attempt, question="Which region?")
        )
    blocked = await get_task(db, task.id)
    assert blocked.state is S.BLOCKED
    assert blocked.block_reason == f"waiting for decision {asked.id}"
    assert asked.kind is DecisionKind.QUESTION

    done = await answer(db, asked.id, caller=ORCH, text="eu-west-1")
    assert done.state is DecisionState.ANSWERED
    after = await get_task(db, task.id)
    assert after.state is S.READY
    assert "## Clarifications" in after.spec
    assert "Which region?" in after.spec
    assert "eu-west-1" in after.spec
    assert after.failures == 0


async def test_answer_must_match_options(db: Database) -> None:
    await create(db)
    task = await claim(db)
    async with db.write() as tx:
        asked = await decisions.ask(
            tx,
            A,
            task.id,
            Ask(attempt=task.attempt, question="Size?", options=["small", "large"]),
        )
    with pytest.raises(ValidationFailedError):
        await answer(db, asked.id, choice="medium")
    await answer(db, asked.id, choice="large")
    assert "large" in (await get_task(db, task.id)).spec


async def test_ask_requires_the_lease(db: Database, clock: FakeClock) -> None:
    await create(db, lease_ttl_s=60)
    task = await claim(db)
    clock.advance(60)
    with pytest.raises(ConflictError):
        async with db.write() as tx:
            await decisions.ask(tx, A, task.id, Ask(attempt=task.attempt, question="?"))
    assert await open_for(db, task.id) == []  # rolled back with the failed block


async def test_audiences(db: Database) -> None:
    await create(db)
    task = await claim(db)
    async with db.write() as tx:
        asked = await decisions.ask(
            tx,
            A,
            task.id,
            Ask(attempt=task.attempt, question="?", audience=Audience.HUMAN),
        )
    for caller in (WORKER, ORCH):
        with pytest.raises(ForbiddenError):
            await answer(db, asked.id, caller=caller, text="x")
    await answer(db, asked.id, caller=ADMIN, text="x")
    with pytest.raises(ConflictError):  # already answered
        await answer(db, asked.id, text="again")


async def _inbox_wakes(db: Database) -> list[tuple[str, str]]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT body, state FROM messages WHERE author = 'system'"
            " AND substr(body, 1, ?) = ? ORDER BY created_at",
            (len(conversations.INBOX_WAKE_PREFIX), conversations.INBOX_WAKE_PREFIX),
        ) as cur,
    ):
        return [(r["body"], r["state"]) for r in await cur.fetchall()]


async def test_inbox_decision_wakes_the_internal_orchestrator(
    db: Database, tmp_path: Path
) -> None:
    path = tmp_path / "models.json"
    for _ in range(4):
        await create(db)
    question = "which-region-token-9f3a"

    task = await claim(db)
    async with db.write() as tx:
        await decisions.ask(
            tx, A, task.id, Ask(attempt=task.attempt, question=question)
        )
    assert await _inbox_wakes(db) == []  # orchestrator is off

    async with db.write() as tx:
        await settings_service.put_setting(
            tx, path, "global", "orchestrator.enabled", True, "admin:t"
        )
        await orchestrator.ensure_updates_conversation(tx, "admin:t")

    task = await claim(db)
    async with db.write() as tx:
        asked = await decisions.ask(
            tx,
            A,
            task.id,
            Ask(attempt=task.attempt, question=question, audience=Audience.ANY),
        )
    wakes = await _inbox_wakes(db)
    assert len(wakes) == 1
    body, state = wakes[0]
    assert state == "queued"
    assert asked.id in body and "kind: question" in body and "audience: any" in body
    assert question not in body

    task = await claim(db)
    async with db.write() as tx:
        await decisions.ask(
            tx,
            A,
            task.id,
            Ask(
                attempt=task.attempt,
                question="again",
                audience=Audience.ORCHESTRATOR,
            ),
        )
    assert len(await _inbox_wakes(db)) == 1  # one queued notice is enough

    task = await claim(db)
    async with db.write() as tx:
        human = await decisions.ask(
            tx,
            A,
            task.id,
            Ask(attempt=task.attempt, question=question, audience=Audience.HUMAN),
        )
    wakes = await _inbox_wakes(db)
    assert len(wakes) == 1 and human.id not in wakes[0][0]

    async with db.write() as tx:
        claimed = await conversations.claim_next(tx, agent_id="orch", path=path)
    assert claimed is not None and claimed[0].kind == "updates"
    assert claimed[1].state == "delivered"
    assert claimed[1].author == "system"
    assert all(state != "queued" for _, state in await _inbox_wakes(db))

    async with db.write() as tx:
        review_id = await decision_store.open_decision(
            tx,
            subject_type="task",
            subject_id=task.id,
            kind=DecisionKind.REVIEW,
            question="Review the secret result PLEASE_NO",
            options=["accept", "reject"],
            audience=Audience.ORCHESTRATOR,
            requested_by=f"agent:{A}",
        )
    queued = [body for body, state in await _inbox_wakes(db) if state == "queued"]
    assert len(queued) == 1
    assert review_id in queued[0] and "PLEASE_NO" not in queued[0]

    async with db.write() as tx:
        await decision_store.open_decision(
            tx,
            subject_type="task",
            subject_id=task.id,
            kind=DecisionKind.REVIEW,
            question="for a human",
            options=["accept", "reject"],
            audience=Audience.HUMAN,
            requested_by=f"agent:{A}",
        )
    assert len([b for b, s in await _inbox_wakes(db) if s == "queued"]) == 1


async def _open_review(db: Database, task_id: str, audience=Audience.ANY) -> str:
    async with db.write() as tx:
        return await decision_store.open_decision(
            tx,
            subject_type="task",
            subject_id=task_id,
            kind=DecisionKind.REVIEW,
            question="Review it",
            options=["accept", "reject"],
            audience=audience,
            requested_by=f"agent:{A}",
        )


async def test_inbox_wake_skipped_at_the_queue_cap_but_decision_opens(
    db: Database, tmp_path: Path
) -> None:
    path = tmp_path / "models.json"
    await create(db)
    task = await claim(db)
    async with db.write() as tx:
        await settings_service.put_setting(
            tx, path, "global", "orchestrator.enabled", True, "admin:t"
        )
        conv_id = await orchestrator.ensure_updates_conversation(tx, "admin:t")
        for i in range(conversations.MAX_QUEUED):
            await tx.conn.execute(
                "INSERT INTO messages (id, conversation_id, author, body, state,"
                " truncated, turn_id, created_at, delivered_at) VALUES (?, ?,"
                " 'admin:t', 'hi', 'queued', 0, NULL, ?, NULL)",
                (f"m{i}", conv_id, f"2026-01-01T00:00:{i:02d}Z"),
            )

    decision_id = await _open_review(db, task.id, Audience.ORCHESTRATOR)

    assert await _inbox_wakes(db) == []
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT count(*) FROM messages WHERE conversation_id = ?", (conv_id,)
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row is not None and row[0] == conversations.MAX_QUEUED
    async with (
        db.read() as conn,
        conn.execute("SELECT state FROM decisions WHERE id = ?", (decision_id,)) as cur,
    ):
        row = await cur.fetchone()
    assert row is not None and row["state"] == "open"


async def test_inbox_wake_skips_safely_without_an_updates_conversation(
    db: Database, tmp_path: Path
) -> None:
    path = tmp_path / "models.json"
    await create(db)
    task = await claim(db)
    async with db.write() as tx:
        await settings_service.put_setting(
            tx, path, "global", "orchestrator.enabled", True, "admin:t"
        )
        await tx.conn.execute("DELETE FROM conversations WHERE kind = 'updates'")

    decision_id = await _open_review(db, task.id)

    assert await _inbox_wakes(db) == []
    async with (
        db.read() as conn,
        conn.execute("SELECT state FROM decisions WHERE id = ?", (decision_id,)) as cur,
    ):
        row = await cur.fetchone()
    assert row is not None and row["state"] == "open"


async def test_decision_becomes_moot_when_task_moves_on(db: Database) -> None:
    await create(db)
    task = await claim(db)
    async with db.write() as tx:
        asked = await decisions.ask(
            tx, A, task.id, Ask(attempt=task.attempt, question="?")
        )
        await tasks.cancel(tx, task.id, "admin:test")
    async with db.snapshot() as snap:
        page = await decision_views.list_decisions(snap, state=None)
    assert page.items[0].state is DecisionState.CANCELLED
    with pytest.raises(ConflictError) as err:
        await answer(db, asked.id, text="too late")
    assert err.value.code == "decision_not_open"


# -- approval, review, policy ----------------------------------------------------------


async def test_approval_gate(db: Database) -> None:
    approved = await create(db, requires_approval=True)
    rejected = await create(db, requires_approval=True)
    [d1] = await open_for(db, approved.id)
    [d2] = await open_for(db, rejected.id)
    assert (d1.kind, d1.audience) == (DecisionKind.APPROVAL, Audience.HUMAN)
    await answer(db, d1.id, choice="approve")
    await answer(db, d2.id, choice="reject")
    assert (await get_task(db, approved.id)).state is S.READY
    assert (await get_task(db, rejected.id)).state is S.CANCELLED


async def test_review_gate(db: Database) -> None:
    task = await create(db, review=Review.ORCHESTRATOR)
    claimed = await claim(db)
    async with db.write() as tx:
        await tasks.complete(tx, A, task.id, claimed.attempt, DONE)
    [review] = await open_for(db, task.id)
    assert (review.kind, review.audience) == (
        DecisionKind.REVIEW,
        Audience.ORCHESTRATOR,
    )

    await answer(db, review.id, caller=ORCH, choice="reject", text="add tests")
    reopened = await get_task(db, task.id)
    assert reopened.state is S.READY
    assert "add tests" in reopened.spec

    claimed = await claim(db)
    async with db.write() as tx:
        await tasks.complete(tx, A, task.id, claimed.attempt, DONE)
    [review] = await open_for(db, task.id)
    await answer(db, review.id, caller=ORCH, choice="accept")
    assert (await get_task(db, task.id)).state is S.SUCCEEDED


async def test_policy_gate_holds_tasks_past_the_hourly_limit(
    db: Database, clock: FakeClock
) -> None:
    actor = f"agent:{A}"
    for _ in range(2):
        assert (await create(db, actor, limit=2)).state is S.READY
    held = await create(db, actor, limit=2)
    assert held.state is S.AWAITING_APPROVAL
    [policy] = await open_for(db, held.id)
    assert (policy.kind, policy.audience) == (DecisionKind.POLICY, Audience.HUMAN)
    await answer(db, policy.id, choice="approve")
    assert (await get_task(db, held.id)).state is S.READY
    clock.advance(3601)  # the window slides
    assert (await create(db, actor, limit=2)).state is S.READY


# -- retries -------------------------------------------------------------------------


async def test_a_retried_answer_is_replayed_not_applied_twice(db: Database) -> None:
    await create(db)
    task = await claim(db)
    async with db.write() as tx:
        asked = await decisions.ask(
            tx, A, task.id, Ask(attempt=task.attempt, question="?", options=["a", "b"])
        )
    first = await answer(db, asked.id, caller=ORCH, choice="a", request_id="r1")
    assert (first.replayed, first.request_id) == (False, "r1")
    spec = (await get_task(db, task.id)).spec
    again = await answer(db, asked.id, caller=ORCH, choice="a", request_id="r1")
    assert again.replayed
    assert again.decided_at == first.decided_at
    assert (await get_task(db, task.id)).spec == spec  # the clarification once
    for change in (
        {"caller": ORCH, "choice": "b", "request_id": "r1"},  # another answer
        {"caller": ORCH, "choice": "a", "request_id": "r2"},  # another request
        {"caller": ADMIN, "choice": "a", "request_id": "r1"},  # another caller
        {"caller": ORCH, "choice": "a", "text": "why", "request_id": "r1"},
        {"caller": ORCH, "choice": "a"},  # no request id: a plain duplicate
    ):
        with pytest.raises(ConflictError) as err:
            await answer(db, asked.id, **change)
        assert err.value.code == "decision_not_open"


async def test_a_cancelled_decision_is_never_replayed(db: Database) -> None:
    task = await create(db, requires_approval=True)
    [approval] = await open_for(db, task.id)
    async with db.write() as tx:
        await tasks.cancel(tx, task.id, "admin:test")
    with pytest.raises(ConflictError):
        await answer(db, approval.id, choice="approve", request_id="r1")


# -- HTTP ------------------------------------------------------------------------------


ADMIN_TOKEN = "test-admin-token-" + "d" * 32
ADMIN_H = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        yield c


def test_inbox_over_http(client: TestClient) -> None:
    boot = client.post("/api/agents/bootstrap-tokens", json={}, headers=ADMIN_H).json()
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    worker = {"Authorization": f"Bearer {reg.json()['token']}"}
    task = client.post(
        "/api/tasks", json={"project": "demo", "title": "t"}, headers=ADMIN_H
    ).json()
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()

    res = client.post(
        f"/api/tasks/{task['id']}/ask",
        json={"attempt": claimed["attempt"], "question": "Proceed?"},
        headers=worker,
    )
    assert res.status_code == 201
    [inbox] = client.get(
        "/api/decisions", params={"view": "detail"}, headers=ADMIN_H
    ).json()["items"]
    assert inbox["question"] == "Proceed?"
    assert (
        client.post(
            f"/api/decisions/{inbox['id']}/answer", json={"text": "yes"}, headers=worker
        ).status_code
        == 403
    )
    res = client.post(
        f"/api/decisions/{inbox['id']}/answer", json={"text": "yes"}, headers=ADMIN_H
    )
    assert res.json()["state"] == "answered"
    assert client.get("/api/decisions", headers=ADMIN_H).json()["total"] == 0
    assert (
        client.get(f"/api/tasks/{task['id']}", headers=ADMIN_H).json()["state"]
        == "ready"
    )


async def test_decisions_carry_the_task_title(db: Database) -> None:
    task = await create(db, requires_approval=True)
    [decision] = await open_for(db, task.id)
    assert decision.subject_title == "t"


def test_answer_many_applies_each_item_on_its_own(client: TestClient) -> None:
    def approval() -> str:
        task = client.post(
            "/api/tasks",
            json={"project": "demo", "title": "t", "requires_approval": True},
            headers=ADMIN_H,
        ).json()
        res = client.get(
            "/api/decisions", params={"subject_id": task["id"]}, headers=ADMIN_H
        )
        return res.json()["items"][0]["id"]

    a, b = approval(), approval()
    batch = {
        "answers": [
            {"decision_id": a, "choice": "approve", "request_id": "ra"},
            {"decision_id": "nope", "choice": "approve"},
            {"decision_id": b, "choice": "maybe"},
            {"decision_id": b, "choice": "reject", "request_id": "rb"},
        ]
    }
    res = client.post("/api/decisions/answers", json=batch, headers=ADMIN_H)
    assert res.status_code == 200
    results = res.json()["results"]
    assert [(r["decision_id"], r["ok"]) for r in results] == [
        (a, True),
        ("nope", False),
        (b, False),
        (b, True),
    ]
    assert results[1]["error"]["code"] == "not_found"
    assert results[2]["error"]["code"] == "invalid_choice"
    assert results[3]["answer_choice"] == "reject"
    assert "question" not in results[3]  # compact: what was decided

    again = client.post("/api/decisions/answers", json=batch, headers=ADMIN_H).json()
    assert [r["ok"] for r in again["results"]] == [True, False, False, True]
    assert all(again["results"][i]["replayed"] for i in (0, 3))

    too_many = {"answers": [{"decision_id": a}] * 21}
    res = client.post("/api/decisions/answers", json=too_many, headers=ADMIN_H)
    assert res.status_code == 422


def test_inbox_hides_what_the_caller_may_not_see(client: TestClient) -> None:
    """The inbox is manager-only: a worker gets no rows, groups or ids from it."""
    boot = client.post("/api/agents/bootstrap-tokens", json={}, headers=ADMIN_H).json()
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    worker = {"Authorization": f"Bearer {reg.json()['token']}"}
    client.post(
        "/api/tasks",
        json={"project": "demo", "title": "t", "requires_approval": True},
        headers=ADMIN_H,
    )
    assert client.get("/api/inbox", headers=worker).status_code == 403
    page = client.get("/api/inbox", params={"order": "impact"}, headers=ADMIN_H).json()
    assert page["total"] == 1 and len(page["groups"]) == 1
    # a worker still sees only the decisions of tasks it holds
    assert client.get("/api/decisions", headers=worker).json()["total"] == 0


def test_batch_partial_failure_retries_with_the_original_request_ids(
    client: TestClient,
) -> None:
    def approval() -> str:
        task = client.post(
            "/api/tasks",
            json={"project": "demo", "title": "t", "requires_approval": True},
            headers=ADMIN_H,
        ).json()
        res = client.get(
            "/api/decisions", params={"subject_id": task["id"]}, headers=ADMIN_H
        )
        return res.json()["items"][0]["id"]

    good, bad = approval(), approval()
    batch = {
        "answers": [
            {"decision_id": good, "choice": "approve", "request_id": "r-good"},
            {"decision_id": bad, "choice": "maybe", "request_id": "r-bad"},
        ]
    }
    first = client.post("/api/decisions/answers", json=batch, headers=ADMIN_H).json()
    assert [r["ok"] for r in first["results"]] == [True, False]
    assert first["results"][1]["error"]["code"] == "invalid_choice"

    # Retry resends the failed item (fixed) and, as a client may, the ok one too:
    # the same request_id replays instead of answering twice.
    retry = {
        "answers": [
            batch["answers"][0],
            {"decision_id": bad, "choice": "reject", "request_id": "r-bad"},
        ]
    }
    again = client.post("/api/decisions/answers", json=retry, headers=ADMIN_H).json()
    assert [r["ok"] for r in again["results"]] == [True, True]
    assert [r["replayed"] for r in again["results"]] == [True, False]
    third = client.post("/api/decisions/answers", json=retry, headers=ADMIN_H).json()
    assert [r["replayed"] for r in third["results"]] == [True, True]
    assert client.get("/api/decisions", headers=ADMIN_H).json()["total"] == 0
