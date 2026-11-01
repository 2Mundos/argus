"""Decision preview (MCP operations 3.2c): read-only, advisory, no drift."""

import json
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from api_support import DONE, insert_agent
from argus_api.auth import Identity
from argus_api.db import Database
from argus_api.decisions import preview as preview_module
from argus_api.decisions import service as decisions
from argus_api.decisions import store as decision_store
from argus_api.errors import ConflictError
from argus_api.main import app
from argus_api.tasks import continuation as cont
from argus_api.tasks import service as tasks
from argus_core.auth import Scope
from argus_core.continuation import (
    ArtifactKind,
    ArtifactState,
    Continuation,
    ContinuationArtifact,
    ContinuationProvenance,
)
from argus_core.decisions import (
    Ask,
    Audience,
    DecisionAnswer,
    DecisionKind,
)
from argus_core.tasks import Review, TaskCreate
from fastapi.testclient import TestClient

A = "agent-a"
ADMIN = Identity("t-admin", Scope.ADMIN, None, "test")
ORCH = Identity("t-orch", Scope.ORCHESTRATOR, "orch", None)
SECRET = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"
SHA_OLD, SHA_NEW = "abc1234def5678", "fed9876cba4321"


@pytest.fixture(autouse=True)
async def agents(db: Database) -> None:
    await insert_agent(db, A)


async def create(db: Database, **kw):
    async with db.write() as tx:
        return await tasks.create_task(
            tx, TaskCreate(project="demo", title="t", **kw), "admin:test"
        )


async def claim(db: Database):
    async with db.write() as tx:
        task = await tasks.claim(tx, A, [])
    assert task is not None
    return task


async def ask(db: Database, question="Which one?", options=("a", "b")):
    await create(db)
    task = await claim(db)
    async with db.write() as tx:
        asked = await decisions.ask(
            tx,
            A,
            task.id,
            Ask(attempt=task.attempt, question=question, options=list(options)),
        )
    return task, asked


async def preview(db: Database, decision_id: str, caller=ADMIN, **kw):
    async with db.snapshot() as snap:
        return await preview_module.preview(snap, decision_id, caller, **kw)


async def review_decision(db: Database, review=Review.ORCHESTRATOR):
    task = await create(db, review=review)
    claimed = await claim(db)
    async with db.write() as tx:
        await tasks.complete(tx, A, task.id, claimed.attempt, DONE)
    async with db.read() as conn:
        found = await decision_store.open_ids(conn, "task", task.id)
    return task, found[0]


def effects(p) -> dict[str, str]:
    return {e.kind: e.detail for e in p.transition.effects}


async def counts(db: Database) -> dict[str, int]:
    out = {}
    async with db.read() as conn:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ) as cur:
            names = [r[0] for r in await cur.fetchall()]
        for name in names:
            async with conn.execute(f'SELECT count(*) FROM "{name}"') as cur:
                out[name] = (await cur.fetchone())[0]
    return out


# -- decision kinds ---------------------------------------------------------------


async def test_question_preview_shows_the_unblock(db: Database) -> None:
    task, asked = await ask(db)
    p = await preview(db, asked.id, ORCH, choice="b")
    assert p.advisory.authorizes is False and p.advisory.executes is False
    assert (p.decision.kind, p.decision.state) == (DecisionKind.QUESTION, "open")
    assert p.question.text == "Which one?"
    assert p.question.reference == {"id": asked.id, "kind": "question"}
    assert (p.proposal.valid, p.proposal.offered_option) == (True, True)
    assert p.authorization.would_allow and p.answer_outcome.result == "would_apply"
    assert p.transition.decision == {"from": "open", "to": "answered", "choice": "b"}
    assert effects(p)["task_state"] == "blocked -> ready (unblock)"
    assert "requeue" in effects(p) and "clarification_added" in effects(p)
    assert effects(p)["knowledge_saved"] == "save_to_knowledge is not previewed"
    assert p.task.id == task.id and p.task.attempt == task.attempt
    assert p.task.attempt_id is not None and p.task.claimed_by == A


async def test_review_accept_and_reject(db: Database) -> None:
    _, decision_id = await review_decision(db)
    accept = await preview(db, decision_id, ORCH, choice="accept")
    assert effects(accept)["task_state"] == "in_review -> succeeded (accept)"
    reject = await preview(db, decision_id, ORCH, choice="reject", text="add tests")
    assert effects(reject)["task_state"] == "in_review -> ready (reject)"
    assert "requeue" in effects(reject) and "review_notes_added" in effects(reject)
    assert reject.proposal.text == "add tests"


async def test_human_review_needs_an_admin(db: Database) -> None:
    _, decision_id = await review_decision(db, Review.HUMAN)
    p = await preview(db, decision_id, ORCH, choice="accept")
    assert p.decision.audience == "human"
    assert not p.authorization.would_allow and p.authorization.error == "forbidden"
    assert (
        p.answer_outcome.result == "rejected"
        and p.answer_outcome.error["status"] == 403
    )
    assert (
        await preview(db, decision_id, ADMIN, choice="accept")
    ).authorization.would_allow


async def test_approval_approve_and_reject_with_moot_decisions(db: Database) -> None:
    task = await create(db, requires_approval=True)
    async with db.write() as tx:
        extra = await decision_store.open_decision(
            tx,
            subject_type="task",
            subject_id=task.id,
            kind=DecisionKind.POLICY,
            question="Extra gate",
            options=["approve", "reject"],
            audience=Audience.HUMAN,
            requested_by="system",
        )
    async with db.read() as conn:
        [approval] = [
            i
            for i in await decision_store.open_ids(conn, "task", task.id)
            if i != extra
        ]
    approve = await preview(db, approval, ADMIN, choice="approve")
    assert effects(approve)["task_state"] == "awaiting_approval -> ready (approve)"
    assert effects(approve)["decisions_cancelled"] == extra
    reject = await preview(db, approval, ADMIN, choice="reject")
    assert effects(reject)["task_state"] == "awaiting_approval -> cancelled (cancel)"
    assert (
        effects(reject)["decisions_cancelled"] == extra
    )  # moot, not the decision itself
    assert "requeue" not in effects(reject)
    # no drift: the real answer does what the preview said
    async with db.write() as tx:
        await decisions.answer(tx, approval, DecisionAnswer(choice="reject"), ADMIN)
    async with db.read() as conn:
        assert (await tasks.get_task(conn, task.id)).state.value == "cancelled"
        assert await decision_store.open_ids(conn, "task", task.id) == []
        async with conn.execute(
            "SELECT state FROM decisions WHERE id = ?", (extra,)
        ) as cur:
            assert (await cur.fetchone())[0] == "cancelled"


async def test_policy_gate_preview(db: Database) -> None:
    actor = f"agent:{A}"
    for _ in range(2):
        async with db.write() as tx:
            await tasks.create_task(
                tx, TaskCreate(project="demo", title="t"), actor, hourly_limit=2
            )
    async with db.write() as tx:
        held = await tasks.create_task(
            tx, TaskCreate(project="demo", title="held"), actor, hourly_limit=2
        )
    async with db.read() as conn:
        [policy] = await decision_store.open_ids(conn, "task", held.id)
    p = await preview(db, policy, ADMIN, choice="approve")
    assert p.decision.kind is DecisionKind.POLICY
    assert effects(p)["task_state"] == "awaiting_approval -> ready (approve)"


# -- states, validation, authorization ---------------------------------------------


async def test_invalid_choice_and_missing_text(db: Database) -> None:
    _, asked = await ask(db)
    bad = await preview(db, asked.id, ORCH, choice="zzz")
    assert (bad.proposal.valid, bad.proposal.error) == (False, "invalid_choice")
    assert bad.proposal.offered_option is False
    assert bad.transition == "unknown"
    assert bad.answer_outcome.result == "rejected"
    assert bad.answer_outcome.error["status"] == 422
    none = await preview(db, asked.id, ORCH)
    assert none.proposal.valid == "unknown" and none.answer_outcome.result == "unknown"
    assert none.transition == "unknown"


async def test_answered_and_cancelled_decisions_and_replay(db: Database) -> None:
    _, asked = await ask(db)
    async with db.write() as tx:
        await decisions.answer(
            tx, asked.id, DecisionAnswer(choice="a", request_id="r1"), ORCH
        )
    p = await preview(db, asked.id, ORCH, choice="b")
    assert p.decision.state == "answered"
    assert p.answer_outcome.result == "rejected"
    assert p.answer_outcome.error["code"] == "decision_not_open"
    assert p.answer_outcome.error["status"] == 409 and p.transition == "unknown"
    same = await preview(db, asked.id, ORCH, choice="a", request_id="r1")
    assert same.answer_outcome.result == "replay"
    other = await preview(db, asked.id, ORCH, choice="a", request_id="r2")
    assert other.answer_outcome.result == "rejected"

    task = await create(db, requires_approval=True)
    async with db.read() as conn:
        [approval] = await decision_store.open_ids(conn, "task", task.id)
    async with db.write() as tx:
        await tasks.cancel(tx, task.id, "admin:test")
    gone = await preview(db, approval, ADMIN, choice="approve", request_id="r1")
    assert gone.decision.state == "cancelled"
    assert gone.answer_outcome.result == "rejected"  # a cancelled one is never replayed
    assert gone.answer_outcome.error["code"] == "decision_not_open"


async def test_race_preview_then_answered_by_someone_else(db: Database) -> None:
    _, asked = await ask(db)
    first = await preview(db, asked.id, ORCH, choice="a")
    assert first.answer_outcome.result == "would_apply"
    async with db.write() as tx:
        await decisions.answer(tx, asked.id, DecisionAnswer(choice="b"), ADMIN)
    with pytest.raises(ConflictError) as err:  # the answer path revalidates
        async with db.write() as tx:
            await decisions.answer(tx, asked.id, DecisionAnswer(choice="a"), ORCH)
    assert err.value.code == "decision_not_open"
    second = await preview(db, asked.id, ORCH, choice="a")
    assert second.decision.state == "answered"
    assert second.answer_outcome.result == "rejected"
    assert second.snapshot.decision_version != first.snapshot.decision_version


# -- premises and redaction ---------------------------------------------------------


async def _store_pr(db: Database, task_id: str, head_sha: str) -> None:
    artifact = ContinuationArtifact(
        kind=ArtifactKind.PR,
        ref="42",
        head_sha=head_sha,
        state=ArtifactState.OPEN,
        provenance=ContinuationProvenance.RUNTIME,
        observed_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    async with db.write() as tx:
        await cont.store(
            tx, task_id, Continuation(), Continuation(artifacts=[artifact]), "t", "t"
        )


async def test_stale_pr_premise_uses_stored_observation_only(db: Database) -> None:
    task = await create(db, review=Review.ORCHESTRATOR)
    claimed = await claim(db)
    async with db.write() as tx:
        await tasks.complete(tx, A, task.id, claimed.attempt, DONE)
        async with tx.conn.execute(
            "SELECT id FROM decisions WHERE subject_id = ?", (task.id,)
        ) as cur:
            [(decision_id,)] = await cur.fetchall()
        await tx.conn.execute(
            "UPDATE decisions SET question = ? WHERE id = ?",
            (f"Review PR 42 at head {SHA_OLD}", decision_id),
        )
    await _store_pr(db, task.id, SHA_NEW)
    p = await preview(db, decision_id, ORCH, choice="accept")
    assert p.premise_checks.live_forge_checked is False
    [check] = p.premise_checks.checks
    assert (check.status, check.premise, check.recorded) == ("stale", SHA_OLD, SHA_NEW)
    assert check.provenance == "stored" and check.observed_at.year == 2026
    # the same head matches; no premise is unknown
    await _store_pr(db, task.id, SHA_OLD)
    async with db.write() as tx:
        await cont.store(
            tx,
            task.id,
            await cont.current(tx, task.id),
            Continuation(
                artifacts=[
                    ContinuationArtifact(
                        kind=ArtifactKind.PR,
                        ref="42",
                        head_sha=SHA_OLD,
                        state=ArtifactState.OPEN,
                        provenance=ContinuationProvenance.RUNTIME,
                        observed_at=datetime(2026, 1, 3, tzinfo=UTC),
                    )
                ]
            ),
            "t",
            "t",
        )
    ok = await preview(db, decision_id, ORCH, choice="accept")
    assert [c.status for c in ok.premise_checks.checks] == ["match"]
    _, plain = await ask(db)
    unknown = await preview(db, plain.id, ORCH, choice="a")
    assert [c.status for c in unknown.premise_checks.checks] == ["unknown"]


async def test_question_text_is_redacted_and_capped(db: Database) -> None:
    _, asked = await ask(db, question=f"Use key {SECRET}? " + "x" * 6000)
    p = await preview(
        db, asked.id, ORCH, choice="a", text=f"Authorization: Bearer {SECRET}"
    )
    assert SECRET not in p.model_dump_json()
    assert p.question.redacted and p.question.truncated
    assert len(p.question.text) <= preview_module.QUESTION_MAX
    assert p.question.full_length > preview_module.QUESTION_MAX


# -- no writes -------------------------------------------------------------------------


async def test_preview_writes_nothing(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, asked = await ask(db)
    _, review_id = await review_decision(db)
    before = await counts(db)

    def no_write(*_a, **_k):
        raise AssertionError("preview opened a write transaction")

    monkeypatch.setattr(db, "write", no_write)
    for decision_id, choice in ((asked.id, "a"), (review_id, "reject")):
        await preview(db, decision_id, ORCH, choice=choice, text="notes")
        await preview(db, decision_id, ORCH)
        await preview(db, decision_id, ORCH, choice="nope")
    monkeypatch.undo()
    assert await counts(db) == before  # no row, event or audit entry anywhere


# -- HTTP and MCP ------------------------------------------------------------------------


ADMIN_TOKEN = "test-admin-token-" + "p" * 32
ADMIN_H = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        yield c


def _token(client: TestClient, role: str) -> str:
    boot = client.post(
        "/api/agents/bootstrap-tokens", json={"role": role}, headers=ADMIN_H
    ).json()
    return client.post(
        "/api/agents/register", json={"bootstrap_token": boot["token"]}
    ).json()["token"]


def _mcp(client: TestClient, token: str, name: str, **arguments) -> dict:
    res = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "Host": "127.0.0.1",
            "Authorization": f"Bearer {token}",
        },
    )
    body = res.text
    if body.startswith("event:"):
        body = next(x[5:] for x in body.splitlines() if x.startswith("data:"))
    return json.loads(body)["result"]


def test_preview_over_http_and_mcp_and_rights(client: TestClient) -> None:
    worker, orch = _token(client, "worker"), _token(client, "orchestrator")
    H = {"Authorization": f"Bearer {worker}"}
    client.post("/api/tasks", json={"title": "t", "project": "demo"}, headers=ADMIN_H)
    claimed = client.post("/api/tasks/claim", json={}, headers=H).json()
    asked = client.post(
        f"/api/tasks/{claimed['id']}/ask",
        json={
            "attempt": claimed["attempt"],
            "question": "A or B?",
            "options": ["a", "b"],
        },
        headers=H,
    ).json()
    url = f"/api/decisions/{asked['id']}/preview"
    ok = client.get(url, params={"choice": "a"}, headers=ADMIN_H)
    assert ok.status_code == 200
    assert ok.json()["advisory"] == {"authorizes": False, "executes": False}
    assert client.get(url, headers=H).status_code == 403  # no manager rights
    assert client.get(url, headers={}).status_code == 401
    assert client.get("/api/decisions/nope/preview", headers=ADMIN_H).status_code == 404
    assert (
        client.get(url, params={"text": "x" * 10_001}, headers=ADMIN_H).status_code
        == 422
    )
    via_mcp = _mcp(
        client, orch, "preview_decision", decision_id=asked["id"], choice="b"
    )
    assert not via_mcp["isError"], via_mcp
    outcome = json.loads(via_mcp["content"][0]["text"])["answer_outcome"]
    assert outcome["result"] == "would_apply"
    tools = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers={
            "Accept": "application/json, text/event-stream",
            "Host": "127.0.0.1",
            "Authorization": f"Bearer {worker}",
        },
    )
    assert "preview_decision" not in tools.text
    # the preview did not answer it
    assert (
        client.get(f"/api/decisions/{asked['id']}", headers=ADMIN_H).json()["state"]
        == "open"
    )


async def _question_decision(db: Database, question: str) -> tuple[str, str]:
    task = await create(db, review=Review.ORCHESTRATOR)
    claimed = await claim(db)
    async with db.write() as tx:
        await tasks.complete(tx, A, task.id, claimed.attempt, DONE)
        async with tx.conn.execute(
            "SELECT id FROM decisions WHERE subject_id = ?", (task.id,)
        ) as cur:
            [(decision_id,)] = await cur.fetchall()
        await tx.conn.execute(
            "UPDATE decisions SET question = ? WHERE id = ?", (question, decision_id)
        )
    return task.id, decision_id


async def _statuses(db: Database, decision_id: str) -> list[str]:
    p = await preview(db, decision_id, ORCH, choice="accept")
    return [c.status for c in p.premise_checks.checks]


async def _store_ref(
    db: Database,
    task_id: str,
    ref: str,
    head_sha: str,
    *,
    kind: ArtifactKind = ArtifactKind.PR,
    url: str | None = None,
    observed_at: datetime = datetime(2026, 1, 2, tzinfo=UTC),
) -> None:
    artifact = ContinuationArtifact(
        kind=kind,
        ref=ref,
        url=url,
        head_sha=head_sha,
        state=ArtifactState.OPEN,
        provenance=ContinuationProvenance.RUNTIME,
        observed_at=observed_at,
    )
    async with db.write() as tx:
        before = await cont.current(tx, task_id)
        after = Continuation(artifacts=cont.upsert_artifact(before.artifacts, artifact))
        await cont.store(tx, task_id, before, after, "t", "t")


async def test_unrelated_pr_is_not_reported_stale(db: Database) -> None:
    task_id, did = await _question_decision(db, "Review PR 42 at head abc1234def5678")
    await _store_ref(db, task_id, "99", "fed9876cba4321")
    assert await _statuses(db, did) == ["unknown"]


async def test_same_pr_changed_head_same_head_and_abbreviation(db: Database) -> None:
    task_id, did = await _question_decision(db, "Review PR 42 at head abc1234def5678")
    await _store_ref(db, task_id, "42", "fed9876cba4321")
    assert await _statuses(db, did) == ["stale"]
    await _store_ref(db, task_id, "42", "abc1234def5678aaaa")  # question abbreviates
    assert await _statuses(db, did) == ["match"]


async def test_multiple_prs_and_ambiguous_references(db: Database) -> None:
    task_id, did = await _question_decision(
        db, "Compare PR 42 at abc1234 with PR 99 at fed9876"
    )
    await _store_ref(db, task_id, "42", "abc1234def5678")
    assert await _statuses(db, did) == ["match", "unknown"]
    task2, did2 = await _question_decision(db, "Is abc1234 still good?")
    await _store_ref(db, task2, "7", "fed9876cba4321")
    assert await _statuses(db, did2) == ["unknown"]


async def _checks(db: Database, decision_id: str) -> list[tuple[str, str, str | None]]:
    p = await preview(db, decision_id, ORCH, choice="accept")
    return [(c.status, c.subject, c.recorded) for c in p.premise_checks.checks]


async def test_named_pr_is_stale_despite_unrelated_pr_at_the_old_head(
    db: Database,
) -> None:
    task_id, did = await _question_decision(db, f"Review PR 42 at head {SHA_OLD}")
    await _store_ref(db, task_id, "42", SHA_NEW)
    await _store_ref(db, task_id, "99", SHA_OLD)  # unrelated, same SHA
    assert await _checks(db, did) == [("stale", "pr 42", SHA_NEW)]
    # an unrelated commit at the head does not mask it either
    await _store_ref(db, task_id, SHA_OLD, SHA_OLD, kind=ArtifactKind.COMMIT)
    assert await _checks(db, did) == [("stale", "pr 42", SHA_NEW)]


async def test_named_pr_at_the_same_head_matches(db: Database) -> None:
    task_id, did = await _question_decision(db, f"Review PR #42 at head {SHA_OLD}")
    await _store_ref(db, task_id, "99", SHA_NEW)
    await _store_ref(db, task_id, "42", SHA_OLD)
    assert await _checks(db, did) == [("match", "pr 42", SHA_OLD)]


async def test_named_pr_not_stored_is_unknown_even_if_another_matches(
    db: Database,
) -> None:
    task_id, did = await _question_decision(db, f"Review PR 42 at head {SHA_OLD}")
    await _store_ref(db, task_id, "99", SHA_OLD)
    assert await _statuses(db, did) == ["unknown"]


async def _two_repos(db: Database, question: str) -> str:
    task_id, did = await _question_decision(db, question)
    url = "https://git.example/team/argus/pulls/42"
    await _store_ref(db, task_id, "42", SHA_OLD, url=url)
    await _store_ref(db, task_id, "team/radar#42", SHA_NEW)
    return did


async def test_same_pr_number_in_two_repositories_is_unknown(db: Database) -> None:
    did = await _two_repos(db, f"Review PR 42 at head {SHA_OLD}")
    assert await _statuses(db, did) == ["unknown"]
    # naming the repository disambiguates
    did = await _two_repos(db, f"Review team/radar#42 at head {SHA_OLD}")
    assert await _checks(db, did) == [("stale", "pr team/radar#42", SHA_NEW)]
    did = await _two_repos(
        db, f"Review https://git.example/team/argus/pulls/42 at head {SHA_OLD}"
    )
    assert await _checks(db, did) == [("match", "pr 42", SHA_OLD)]


async def test_abbreviated_shas_match_either_way(db: Database) -> None:
    task_id, did = await _question_decision(db, "Review PR 42 at head abc1234")
    await _store_ref(db, task_id, "42", SHA_OLD)
    assert await _statuses(db, did) == ["match"]
    task2, did2 = await _question_decision(db, f"Review PR 42 at head {SHA_OLD}")
    await _store_ref(db, task2, "42", "abc1234")
    assert await _statuses(db, did2) == ["match"]
    task3, did3 = await _question_decision(db, "Review PR 42 at head abc1234")
    await _store_ref(db, task3, "42", "abc1235def")
    assert await _statuses(db, did3) == ["stale"]


async def test_sha_without_a_pr_matches_only_when_unambiguous(db: Database) -> None:
    task_id, did = await _question_decision(db, "Is abc1234 still good?")
    await _store_ref(db, task_id, SHA_OLD, SHA_OLD, kind=ArtifactKind.COMMIT)
    await _store_ref(db, task_id, "7", SHA_OLD)  # the same commit twice: one head
    assert set(await _statuses(db, did)) == {"match"}
    # a short SHA that prefixes two different stored heads is ambiguous
    await _store_ref(db, task_id, "8", "abc1234ffff")
    assert await _statuses(db, did) == ["unknown"]
    # no PR named and no stored head matches: never stale
    task2, did2 = await _question_decision(db, "Is 1234abcd still good?")
    await _store_ref(db, task2, "7", SHA_NEW)
    assert await _statuses(db, did2) == ["unknown"]


async def test_multiple_shas_and_prs_bind_to_their_own_pr(db: Database) -> None:
    task_id, did = await _question_decision(
        db, f"PR 42 at {SHA_OLD}, PR 99 at {SHA_NEW} and PR 7 at 1111aaa2222"
    )
    await _store_ref(db, task_id, "42", SHA_NEW)  # 42 moved to 99's head
    await _store_ref(db, task_id, "99", SHA_NEW)
    await _store_ref(db, task_id, "5", "1111aaa2222")  # unrelated PR at 7's head
    assert await _checks(db, did) == [
        ("stale", "pr 42", SHA_NEW),
        ("match", "pr 99", SHA_NEW),
        ("unknown", "premise cannot be tied to a stored PR or commit", None),
    ]


async def test_uuid_fragments_are_not_shas(db: Database) -> None:
    task_id, did = await _question_decision(
        db, "Retry run 3f2a9c1e-7b4d-4e8a-9c2b-1a2b3c4d5e6f on PR 42"
    )
    await _store_ref(db, task_id, "42", SHA_NEW)
    p = await preview(db, did, ORCH, choice="accept")
    assert [(c.status, c.premise) for c in p.premise_checks.checks] == [
        ("unknown", None)
    ]


async def test_observation_older_than_the_decision_is_not_asserted_stale(
    db: Database, clock
) -> None:
    clock.advance(86_400 * 5)  # the decision is asked after the observation
    task_id, did = await _question_decision(db, f"Review PR 42 at head {SHA_OLD}")
    await _store_ref(db, task_id, "42", SHA_NEW)  # observed 2026-01-02
    assert await _statuses(db, did) == ["unknown"]
    # a later observation can still prove the premise stale
    await _store_ref(
        db, task_id, "42", SHA_NEW, observed_at=datetime(2026, 2, 1, tzinfo=UTC)
    )
    assert await _statuses(db, did) == ["stale"]
