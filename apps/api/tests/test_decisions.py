from collections.abc import Generator
from pathlib import Path

import pytest
from api_support import DONE, FakeClock, insert_agent
from argus_api.auth import Identity
from argus_api.db import Database
from argus_api.decisions import service as decisions
from argus_api.decisions import views as decision_views
from argus_api.errors import ConflictError, ForbiddenError, ValidationFailedError
from argus_api.main import app
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
            tx, TaskCreate(project="global", title="t", **kw), actor, hourly_limit=limit
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
        "/api/tasks", json={"project": "global", "title": "t"}, headers=ADMIN_H
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
            json={"project": "global", "title": "t", "requires_approval": True},
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
    assert results[3]["decision"]["answer_choice"] == "reject"

    again = client.post("/api/decisions/answers", json=batch, headers=ADMIN_H).json()
    assert [r["ok"] for r in again["results"]] == [True, False, False, True]
    assert all(again["results"][i]["decision"]["replayed"] for i in (0, 3))

    too_many = {"answers": [{"decision_id": a}] * 21}
    res = client.post("/api/decisions/answers", json=too_many, headers=ADMIN_H)
    assert res.status_code == 422
