"""A merge that happened elsewhere closes the merge-assistance question that blocks its
task (docs/specs/task-delivery.spec.md 3.7). The forge is a mock transport."""

import asyncio
from collections.abc import Generator
from pathlib import Path

import httpx2 as httpx
import pytest
from argus_api.db import Database
from argus_api.decisions import merge_supersede as ms
from argus_api.main import app
from argus_core.decisions import MERGE_ASSISTANCE_RESOURCE
from fastapi.testclient import TestClient
from test_mcp import ADMIN_TOKEN
from test_task_deliverable import (
    claim,
    code,
    create,
    events,
    state,
    worker,
)
from test_task_delivery import HEAD, URL, forge, full_forge, open_pr, setup_repository

MERGE_SHA = "c" * 40
CHECKLIST = ["the merged change is verified on the base branch"]
MERGED = open_pr(
    merged=True,
    state="closed",
    merged_at="2026-11-06T10:00:00Z",
    merge_commit_sha=MERGE_SHA,
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        setup_repository(c)
        yield c


def serve(client: TestClient, pull: dict | int) -> list[str]:
    """Serve PR 12 fresh, recording reads and failing on a read inside a write.
    An int is a forge that answers every request with that status."""
    client.app.state.pr_checker._cache.clear()  # ty: ignore[unresolved-attribute]
    db: Database = client.app.state.db  # ty: ignore[unresolved-attribute]
    reads: list[str] = []
    inner = full_forge(pull if isinstance(pull, dict) else {})

    def handler(request: httpx.Request) -> httpx.Response:
        assert not db.writing(), "forge read inside a write transaction"
        reads.append(request.url.path)
        if isinstance(pull, int):
            return httpx.Response(pull, json={"message": "no"})
        return inner(request)

    forge(client, handler)
    return reads


def blocked(
    client: TestClient,
    *,
    resource: str | None = MERGE_ASSISTANCE_RESOURCE,
    category: str = "assistance",
    progress: dict | None = None,
    note: str | None = None,
    head: str | None = None,
    **extra,
) -> tuple[str, str, dict[str, str]]:
    """A worker opens PR 12, asks for it to be merged and is blocked on the question."""
    w = worker(client)
    task_id = create(client, "pr_merged", acceptance=CHECKLIST, **extra).json()["id"]
    t = claim(client, w)
    assert t["id"] == task_id
    body = (
        {"attempt": t["attempt"], "message": note}
        if note
        else {
            "attempt": t["attempt"],
            "message": "PR up",
            "kind": "pr_opened",
            "data": progress or {"number": 12, "url": URL},
        }
    )
    res = client.post(f"/api/tasks/{task_id}/progress", json=body, headers=w)
    assert res.status_code in (200, 201), res.text
    if head:
        seen = client.post(
            f"/api/tasks/{task_id}/observations",
            json={
                "attempt": t["attempt"],
                "artifacts": [
                    {"kind": "pr", "ref": "12", "state": "open", "head_sha": head}
                ],
            },
            headers=w,
        )
        assert seen.status_code == 200, seen.text
    asked = client.post(
        f"/api/tasks/{task_id}/ask",
        json={
            "attempt": t["attempt"],
            "question": "Please merge the PR",
            "category": category,
            **({"resource": resource} if resource else {}),
        },
        headers=w,
    )
    assert asked.status_code == 201, asked.text
    assert state(client, task_id) == "blocked"
    return task_id, asked.json()["id"], w


def reconcile(client: TestClient, task_id: str) -> ms.Outcome:
    app_state = client.app.state  # ty: ignore[unresolved-attribute]
    assert client.portal is not None
    return client.portal.call(
        ms.reconcile_task, app_state.db, app_state.pr_checker, task_id
    )


def sweep(client: TestClient, backoff: ms.Backoff | None = None) -> list[ms.Outcome]:
    app_state = client.app.state  # ty: ignore[unresolved-attribute]
    assert client.portal is not None
    return client.portal.call(
        ms.sweep, app_state.db, app_state.pr_checker, backoff or ms.Backoff()
    )


def decision(client: TestClient, decision_id: str) -> dict:
    return client.get(f"/api/decisions/{decision_id}").json()


def contract(client: TestClient, task_id: str) -> dict:
    return client.get(f"/api/tasks/{task_id}/continuation").json()


def test_a_merge_elsewhere_resumes_a_task_blocked_on_the_merge_request(
    client: TestClient,
) -> None:
    task_id, decision_id, _ = blocked(client)
    serve(client, MERGED)
    before = client.get(f"/api/tasks/{task_id}/delivery")
    assert before.status_code == 200
    # Reading the delivery changes nothing, even though the forge says merged.
    assert state(client, task_id) == "blocked"
    assert decision(client, decision_id)["state"] == "open"

    assert reconcile(client, task_id) == ms.Outcome(task_id, True, "superseded")

    assert state(client, task_id) == "ready"
    answered = decision(client, decision_id)
    assert answered["state"] == "answered"
    assert answered["decided_by"] == ms.ACTOR
    [proof] = events(client, task_id, ms.SUPERSEDED)
    assert proof["actor"] == ms.ACTOR
    assert proof["data"] | {"checked_at": None} == {
        "decision_id": decision_id,
        "source": "forge",
        "repository": "https://git.example.test/team/argus",
        "number": 12,
        "url": URL,
        "head_sha": HEAD,
        "head_ref": "feat",
        "merge_sha": MERGE_SHA,
        "merged_at": "2026-11-06T10:00:00+00:00",
        "checked_at": None,
        "pending_checklist": proof["data"]["pending_checklist"],
        "not_asserted": ["checks", "review", "acceptance", "completion"],
        "revision": proof["data"]["revision"],
    }
    assert len(proof["data"]["pending_checklist"]) == 1

    # The former worker is gone: another one claims and receives the close-evidence
    # contract with the existing merged PR, the checklist and no completion.
    fresh = worker(client)
    claimed = claim(client, fresh)
    c = claimed["continuation"]
    assert claimed["id"] == task_id and claimed["attempt"] == 2
    assert c["phase"] == "close_evidence" and c["phase_source"] == "explicit"
    assert "Do not merge again" in c["objective"] and MERGE_SHA in c["objective"]
    [pr] = [a for a in c["artifacts"] if a["kind"] == "pr"]
    assert (pr["ref"], pr["state"], pr["head_sha"]) == ("12", "merged", HEAD)
    assert pr["provenance"] == "argus_event"
    assert len(c["pending"]) == 1 and c["checklist"][0]["status"] == "pending"
    assert c["decisions"][0]["decision_id"] == decision_id
    # Nothing was completed, accepted or ticked on the worker's behalf.
    assert not events(client, task_id, "task.completed")
    assert client.get(f"/api/tasks/{task_id}").json()["result"] is None


def test_replays_and_restarts_apply_the_effect_once(client: TestClient) -> None:
    task_id, decision_id, _ = blocked(client)
    serve(client, MERGED)
    assert [o.resumed for o in sweep(client)] == [True]
    revision = contract(client, task_id)["revision"]

    # Duplicate or out-of-order passes, with a fresh in-memory backoff (a restart).
    assert reconcile(client, task_id).reason == "not_blocked"
    assert sweep(client) == []
    assert len(events(client, task_id, ms.SUPERSEDED)) == 1
    assert len(events(client, decision_id, "decision.answered")) == 1
    assert contract(client, task_id)["revision"] == revision
    assert decision(client, decision_id)["state"] == "answered"


def test_an_answer_that_wins_the_race_leaves_nothing_behind(
    client: TestClient,
) -> None:
    task_id, decision_id, _ = blocked(client)
    serve(client, MERGED)
    app_state = client.app.state  # ty: ignore[unresolved-attribute]

    async def race() -> list[ms.Outcome]:
        # The orchestrator's answer lands between the forge read and the write.
        async def human() -> None:
            async with app_state.db.write() as tx:
                from argus_api.decisions import service
                from argus_core.decisions import DecisionAnswer

                await service.answer(
                    tx, decision_id, DecisionAnswer(text="merge it yourself"), ms.SYSTEM
                )

        original = app_state.pr_checker.snapshot

        async def snapshot(conn, ref):
            snap = await original(conn, ref)
            await human()
            return snap

        app_state.pr_checker.snapshot = snapshot
        try:
            return [
                await ms.reconcile_task(app_state.db, app_state.pr_checker, task_id)
            ]
        finally:
            del app_state.pr_checker.snapshot

    assert client.portal is not None
    [outcome] = client.portal.call(race)
    assert not outcome.resumed and outcome.reason in {"not_blocked", "no_open_question"}
    assert not events(client, task_id, ms.SUPERSEDED)
    assert state(client, task_id) == "ready"  # the single answer's effect
    assert contract(client, task_id)["phase"] != "close_evidence"


def test_a_claim_that_wins_leaves_the_running_task_alone(client: TestClient) -> None:
    task_id, _, _ = blocked(client)
    serve(client, MERGED)
    assert reconcile(client, task_id).resumed
    claimed = claim(client, worker(client))
    assert claimed["id"] == task_id
    # Now claimed: another pass finds nothing blocked and changes nothing.
    assert reconcile(client, task_id).reason == "not_blocked"
    assert state(client, task_id) == "claimed"
    assert len(events(client, task_id, ms.SUPERSEDED)) == 1


@pytest.mark.parametrize(
    ("pull", "reason"),
    [
        (open_pr(), "not_merged"),
        (open_pr(merged=False, state="closed"), "not_merged"),
        (500, "forge_unknown"),
        (404, "forge_unknown"),
        ({**MERGED, "merge_commit_sha": None}, "evidence_incomplete"),
        ({**MERGED, "merged_at": None}, "evidence_incomplete"),
    ],
    ids=["open", "closed", "forge_down", "not_found", "no_merge_sha", "no_merged_at"],
)
def test_unmerged_or_unknown_evidence_never_wakes_the_task(
    client: TestClient, pull: dict | int, reason: str
) -> None:
    task_id, decision_id, _ = blocked(client)
    serve(client, pull)
    assert reconcile(client, task_id) == ms.Outcome(task_id, False, reason)
    assert state(client, task_id) == "blocked"
    assert decision(client, decision_id)["state"] == "open"
    assert not events(client, task_id, ms.SUPERSEDED)
    assert contract(client, task_id)["phase"] != "close_evidence"


def test_a_forge_that_recovers_resumes_it_later(client: TestClient) -> None:
    blocked(client)
    serve(client, 500)
    backoff = ms.Backoff()
    assert [o.reason for o in sweep(client, backoff)] == ["forge_unknown"]
    # Still backing off: the forge is not read again.
    reads = serve(client, MERGED)
    assert sweep(client, backoff) == [] and reads == []
    backoff.next_at.clear()
    assert [o.resumed for o in sweep(client, backoff)] == [True]


def test_a_head_that_moved_since_the_worker_reported_it_is_left_for_review(
    client: TestClient,
) -> None:
    task_id, decision_id, _ = blocked(client, head=HEAD)
    serve(client, {**MERGED, "head": {"sha": "d" * 40, "ref": "feat"}})
    res = reconcile(client, task_id)
    assert (res.resumed, res.reason) == (False, "head_moved")
    assert state(client, task_id) == "blocked"
    [note] = events(client, task_id, ms.SKIPPED)
    assert note["data"]["decision_id"] == decision_id
    assert note["data"]["reported_head"] == HEAD
    assert note["data"]["forge_head"] == "d" * 40
    # The same reason is explained once, not on every pass.
    reconcile(client, task_id)
    assert len(events(client, task_id, ms.SKIPPED)) == 1


def test_a_stale_observation_is_not_evidence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _, _ = blocked(client)
    serve(client, MERGED)
    monkeypatch.setattr(ms, "FRESH_S", -1)
    assert reconcile(client, task_id).reason == "stale_observation"
    assert state(client, task_id) == "blocked"


@pytest.mark.parametrize(
    "progress",
    [
        {"number": 12, "url": "https://git.example.test/team/other/pull/12"},
        {"number": 12, "url": "https://elsewhere.test/team/argus/pulls/12"},
    ],
    ids=["wrong_repo", "wrong_host"],
)
def test_a_pr_that_is_not_the_projects_one_is_never_read(
    client: TestClient, progress: dict
) -> None:
    task_id, _, _ = blocked(client, progress=progress)
    reads = serve(client, MERGED)
    res = reconcile(client, task_id)
    assert (res.resumed, res.reason) == (False, "wrong_repository")
    assert reads == [] and state(client, task_id) == "blocked"


def test_prose_naming_two_prs_is_ambiguous_and_never_read(client: TestClient) -> None:
    task_id, _, _ = blocked(client, note="see PR #12 and PR #13")
    reads = serve(client, MERGED)
    assert reconcile(client, task_id).reason == "pr_unresolved"
    assert reads == [] and state(client, task_id) == "blocked"


@pytest.mark.parametrize(
    ("resource", "category"),
    [
        ("merge", "assistance"),  # a historical, free-text question
        ("postgres", "assistance"),
        (None, "assistance"),
        (None, "decision"),
    ],
    ids=["untyped_merge", "other_resource", "no_resource", "decision"],
)
def test_other_questions_are_preserved_and_explained(
    client: TestClient, resource: str | None, category: str
) -> None:
    task_id, decision_id, _ = blocked(client, resource=resource, category=category)
    reads = serve(client, MERGED)
    for outcome in sweep(client):
        assert not outcome.resumed
    sweep(client)
    assert reads == []  # not even read: Argus does not guess what was asked
    assert state(client, task_id) == "blocked"
    assert decision(client, decision_id)["state"] == "open"
    if category == "assistance":
        [note] = events(client, task_id, ms.SKIPPED)
        assert note["data"]["reason"] == "not_merge_assistance"
    assert not events(client, task_id, ms.SUPERSEDED)


def test_another_open_question_keeps_the_task_blocked(client: TestClient) -> None:
    task_id, decision_id, _ = blocked(client)
    serve(client, MERGED)

    async def second() -> None:
        app_state = client.app.state  # ty: ignore[unresolved-attribute]
        async with app_state.db.write() as tx:
            await tx.conn.execute(
                "INSERT INTO decisions (id, subject_type, subject_id, kind, question,"
                " audience, state, requested_by, created_at)"
                " SELECT 'other', subject_type, subject_id, kind, 'Which region?',"
                " 'any', 'open', requested_by, created_at FROM decisions WHERE id = ?",
                (decision_id,),
            )

    assert client.portal is not None
    client.portal.call(second)
    res = reconcile(client, task_id)
    assert (res.resumed, res.reason) == (False, "several_open_questions")
    assert decision(client, decision_id)["state"] == "open"
    assert state(client, task_id) == "blocked"


def test_a_human_audience_or_a_non_worker_question_is_not_ours(
    client: TestClient,
) -> None:
    task_id, decision_id, _ = blocked(client)
    serve(client, MERGED)

    async def retarget() -> None:
        app_state = client.app.state  # ty: ignore[unresolved-attribute]
        async with app_state.db.write() as tx:
            await tx.conn.execute(
                "UPDATE decisions SET requested_by = 'admin:someone' WHERE id = ?",
                (decision_id,),
            )

    assert client.portal is not None
    client.portal.call(retarget)
    assert reconcile(client, task_id).reason == "not_a_worker_request"
    assert state(client, task_id) == "blocked"


def test_terminal_and_budget_blocked_tasks_stay_as_they_are(
    client: TestClient,
) -> None:
    task_id, decision_id, _ = blocked(client)
    serve(client, MERGED)
    cancelled = client.post(f"/api/tasks/{task_id}/cancel", json={"reason": "no"})
    assert cancelled.status_code == 200, cancelled.text
    assert reconcile(client, task_id).resumed is False
    assert state(client, task_id) == "cancelled"
    assert decision(client, decision_id)["state"] == "cancelled"
    assert not events(client, task_id, ms.SUPERSEDED)
    # A resolved answer cannot be reused to reopen anything.
    again = client.post(f"/api/decisions/{decision_id}/answer", json={"text": "x"})
    assert code(again) == "decision_not_open"


def test_a_pass_reads_a_bounded_number_of_tasks(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ms, "MAX_PER_PASS", 2)
    serve(client, open_pr())
    ids = [blocked(client)[0] for _ in range(3)]
    backoff = ms.Backoff()
    assert len(sweep(client, backoff)) == 2
    serve(client, MERGED)
    backoff.next_at.clear()
    assert len(sweep(client, backoff)) == 2
    backoff.next_at.clear()
    assert len(sweep(client, backoff)) == 1  # the third; the others are resumed
    assert sorted(state(client, i) for i in ids) == ["ready", "ready", "ready"]


def test_the_loop_survives_a_failing_pass(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_state = client.app.state  # ty: ignore[unresolved-attribute]
    calls: list[int] = []

    async def broken(*_args) -> list:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        raise asyncio.CancelledError

    monkeypatch.setattr(ms, "sweep", broken)

    async def drive() -> None:
        with pytest.raises(asyncio.CancelledError):
            await ms.run(app_state.db, app_state.pr_checker, 0)

    assert client.portal is not None
    client.portal.call(drive)
    assert len(calls) == 2
