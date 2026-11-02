"""Task deliverable (docs/specs/task-deliverable.spec.md): the creation rules, the
completion gate, the forge-read accept gate and editing. The forge is a mock transport."""

import asyncio
from collections.abc import Callable, Generator
from pathlib import Path

import httpx2 as httpx
import pytest
from argus_api import model_policy
from argus_api.db import Database
from argus_api.main import app
from argus_api.tasks import independent_review as indep
from argus_api.tasks import pr_binding, service
from argus_core.delivery import DeliveryReconciliation
from argus_core.settings import IndependentReviewSetting
from argus_core.tasks import CODE_DELIVERABLES, Deliverable, TaskCreate, TaskResult
from fastapi.testclient import TestClient
from test_mcp import ADMIN_TOKEN
from test_task_delivery import (
    BASE,
    HEAD,
    URL,
    forge,
    full_forge,
    open_pr,
    setup_repository,
)

CAPS = ["repository:write"]
LEVELS = [d.value for d in Deliverable]


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        setup_repository(c)
        yield c


def worker(client: TestClient) -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
    res = client.post(
        "/api/agents/register",
        json={"bootstrap_token": boot["token"], "capabilities": CAPS},
    )
    assert res.status_code == 201, res.text
    return {"Authorization": f"Bearer {res.json()['token']}"}


def create(client: TestClient, level: str, **extra):
    body = {
        "project": "demo",
        "title": f"deliver {level}",
        "deliverable": level,
        "required_capabilities": CAPS if level in CODE_DELIVERABLES else [],
        "review": "orchestrator" if level in ("pr_green", "pr_merged") else "none",
        **extra,
    }
    return client.post("/api/tasks", json=body)


def claim(client: TestClient, w: dict[str, str]) -> dict:
    res = client.post("/api/tasks/claim", json={"capabilities": CAPS}, headers=w)
    assert res.status_code == 200, res.text
    return res.json()


def complete(client: TestClient, w: dict[str, str], task: dict, **result):
    return client.post(
        f"/api/tasks/{task['id']}/complete",
        json={"attempt": task["attempt"], "result": {"summary": "done", **result}},
        headers=w,
    )


def code(res: httpx.Response) -> str:
    return res.json()["detail"]["code"]


def state(client: TestClient, task_id: str) -> str:
    return client.get(f"/api/tasks/{task_id}").json()["state"]


# -- the evidence each level asks for ----------------------------------------------


def pr_opened(client: TestClient, w: dict[str, str], t: dict) -> dict:
    res = client.post(
        f"/api/tasks/{t['id']}/progress",
        json={
            "attempt": t["attempt"],
            "message": "PR up",
            "kind": "pr_opened",
            "data": {"number": 12, "url": URL},
        },
        headers=w,
    )
    assert res.status_code in (200, 201), res.text
    return {}


def branch(client: TestClient, w: dict[str, str], t: dict) -> dict:
    res = client.post(
        f"/api/tasks/{t['id']}/observations",
        json={
            "attempt": t["attempt"],
            "artifacts": [{"kind": "branch", "ref": "feat", "state": "open"}],
        },
        headers=w,
    )
    assert res.status_code == 200, res.text
    return {}


def knowledge(client: TestClient, w: dict[str, str], t: dict) -> dict:
    res = client.post(
        "/api/knowledge/pages",
        json={"project": "demo", "title": "Learned", "body": "b", "reason": "r"},
        headers=w,
    )
    assert res.status_code in (200, 201), res.text
    return {}


def subtask(client: TestClient, w: dict[str, str], t: dict) -> dict:
    res = client.post(
        "/api/tasks", json={"title": "part", "parent_id": t["id"]}, headers=w
    )
    assert res.status_code == 201, res.text
    return {}


def artifact(client: TestClient, w: dict[str, str], t: dict) -> dict:
    res = client.post(
        f"/api/tasks/{t['id']}/artifacts",
        params={"attempt": t["attempt"], "name": "report.md"},
        content=b"# hi",
        headers={**w, "Content-Type": "text/markdown"},
    )
    assert res.status_code == 201, res.text
    return {"artifacts": [res.json()["id"]]}


EVIDENCE: dict[str, Callable[[TestClient, dict[str, str], dict], dict]] = {
    "response": lambda c, w, t: {},
    "knowledge": knowledge,
    "artifact": artifact,
    "subtasks": subtask,
    "branch": branch,
    "pr_open": pr_opened,
    "pr_green": pr_opened,
    "pr_merged": pr_opened,
}


# -- creation -----------------------------------------------------------------------


@pytest.mark.parametrize("level", LEVELS)
def test_creation_asks_for_the_capabilities_the_level_implies(
    client: TestClient, level: str
) -> None:
    res = create(client, level, required_capabilities=[])
    if level not in CODE_DELIVERABLES:
        assert res.status_code == 201, res.text
        assert res.json()["deliverable"] == level
        return
    assert (res.status_code, code(res)) == (422, "deliverable_capability")
    client.put("/api/projects/bare", json={"name": "Bare", "description": "d"})
    res = create(client, level, project="bare")
    assert (res.status_code, code(res)) == (422, "deliverable_repository")
    res = create(client, level)
    assert res.status_code == 201, res.text
    assert res.json()["deliverable"] == level


async def test_routine_templates_and_drafts_share_the_creation_rules(
    db: Database,
) -> None:
    async with db.read() as conn:
        with pytest.raises(service.ValidationFailedError) as err:
            await service.check_create(
                conn,
                TaskCreate(
                    project="demo",
                    title="t",
                    deliverable=Deliverable.PR_OPEN,
                    required_capabilities=CAPS,
                ),
            )
    assert err.value.code == "deliverable_repository"  # no pool names a repository


# -- completion ---------------------------------------------------------------------


@pytest.mark.parametrize("level", LEVELS)
def test_completion_needs_the_evidence_of_the_level(
    client: TestClient, level: str
) -> None:
    w = worker(client)
    task_id = create(client, level).json()["id"]
    claimed = claim(client, w)
    if level != "response":
        refused = complete(client, w, claimed)
        assert (refused.status_code, code(refused)) == (422, "deliverable_missing")
        # The claim survives: the worker fixes the gap in the same attempt.
        assert state(client, task_id) == "claimed"
    result = EVIDENCE[level](client, w, claimed)
    res = complete(client, w, claimed, **result)
    assert res.status_code == 200, res.text
    # A PR or branch with no structured checklist already waits in review; green or
    # merged is the forge's word, so those say why they wait.
    assert res.json()["state"] == (
        "in_review" if level in CODE_DELIVERABLES else "succeeded"
    )
    [done] = [
        e
        for e in client.get("/api/events", params={"subject_id": task_id}).json()
        if e["kind"] == "task.complete"
    ]
    assert ("deliverable_pending" in done["data"]) == (
        level in ("pr_green", "pr_merged")
    )


def test_a_branch_task_is_not_met_by_a_closed_branch(client: TestClient) -> None:
    w = worker(client)
    create(client, "branch")
    claimed = claim(client, w)
    res = client.post(
        f"/api/tasks/{claimed['id']}/observations",
        json={
            "attempt": claimed["attempt"],
            "artifacts": [{"kind": "branch", "ref": "feat", "state": "closed"}],
        },
        headers=w,
    )
    assert res.status_code == 200, res.text
    assert code(complete(client, w, claimed)) == "deliverable_missing"


# -- accept -------------------------------------------------------------------------


def merged_pr() -> dict:
    return open_pr(merged=True, state="closed", merged_at="2026-10-24T09:00:00Z")


CI_OK = [{"context": "ci", "state": "success"}]
CI_RED = [{"context": "ci", "state": "failure"}]
CLOSED = {
    "state": "closed",
    "merged": False,
    "head": {"sha": HEAD, "ref": "feat"},
    "base": {"sha": BASE, "ref": "main"},
}


def in_review(client: TestClient, level: str) -> tuple[str, str]:
    w = worker(client)
    task_id = create(client, level).json()["id"]
    claimed = claim(client, w)
    pr_opened(client, w, claimed)
    assert complete(client, w, claimed).json()["state"] == "in_review"
    [decision] = client.get("/api/decisions", params={"subject_id": task_id}).json()[
        "items"
    ]
    return task_id, decision["id"]


# (level, forge pull body, commit statuses, accept result)
ACCEPTS = [
    ("pr_green", open_pr(), CI_OK, "ok"),
    ("pr_green", merged_pr(), [], "ok"),
    ("pr_green", open_pr(), CI_RED, "deliverable_unmet"),
    ("pr_green", open_pr(), [], "deliverable_unverified"),
    ("pr_green", CLOSED, CI_OK, "deliverable_unmet"),
    ("pr_merged", merged_pr(), [], "ok"),
    ("pr_merged", open_pr(), CI_OK, "deliverable_unmet"),
    ("pr_merged", CLOSED, [], "deliverable_unmet"),
]


@pytest.mark.parametrize(("level", "pull", "statuses", "outcome"), ACCEPTS)
@pytest.mark.parametrize("batch", [False, True])
def test_accepting_reads_the_forge_and_fails_closed(
    client: TestClient,
    level: str,
    pull: dict,
    statuses: list,
    outcome: str,
    batch: bool,
) -> None:
    task_id, decision_id = in_review(client, level)
    forge(client, full_forge(pull, statuses=statuses))
    answer = {"choice": "accept", "text": ""}
    if batch:
        res = client.post(
            "/api/decisions/answers",
            json={"answers": [{"decision_id": decision_id, **answer}]},
        )
        assert res.status_code == 200, res.text
        [item] = res.json()["results"]
        got = "ok" if item["ok"] else item["error"]["code"]
    else:
        res = client.post(f"/api/decisions/{decision_id}/answer", json=answer)
        got = "ok" if res.status_code == 200 else code(res)
    assert got == outcome
    assert state(client, task_id) == ("succeeded" if outcome == "ok" else "in_review")


def test_an_unreachable_forge_never_closes_the_task(client: TestClient) -> None:
    task_id, decision_id = in_review(client, "pr_merged")
    forge(client, lambda request: httpx.Response(500))
    res = client.post(
        f"/api/decisions/{decision_id}/answer", json={"choice": "accept", "text": ""}
    )
    assert (res.status_code, code(res)) == (409, "deliverable_unverified")
    assert state(client, task_id) == "in_review"


def test_rejecting_is_never_gated(client: TestClient) -> None:
    task_id, decision_id = in_review(client, "pr_merged")
    forge(client, lambda request: httpx.Response(500))
    res = client.post(
        f"/api/decisions/{decision_id}/answer", json={"choice": "reject", "text": "no"}
    )
    assert res.status_code == 200, res.text
    assert state(client, task_id) != "in_review"


async def test_an_independent_approval_does_not_close_a_forge_gated_task(
    db: Database,
) -> None:
    """The approval used to auto-accept a task with no review and no open items."""
    setting = IndependentReviewSetting(mode="different_model", max_rounds=3)
    policy = model_policy.validate(
        {"harness": {"claude": ["a"]}, "implementation": {"claude": ["a"]}}
    )
    states = {}
    for level in ("response", "pr_merged"):
        async with db.write() as tx:
            task = await service.create_task(
                tx, TaskCreate(project="demo", title=level), "human:test"
            )
            await tx.conn.execute(
                "UPDATE tasks SET state = 'in_review', deliverable = ? WHERE id = ?",
                (level, task.id),
            )
            child = {"id": "child", "parent_id": task.id, "input": "{}"}
            await indep.on_review_child_complete(
                tx,
                child,  # ty: ignore[invalid-argument-type]
                TaskResult(summary="ok", data={"verdict": "approve", "findings": []}),
                setting=setting,
                policy=policy,
                actor="agent:reviewer",
            )
        async with db.read() as conn:
            states[level] = (await service.get_task(conn, task.id)).state.value
    assert states == {"response": "succeeded", "pr_merged": "in_review"}


# -- editing ------------------------------------------------------------------------


def edit(client: TestClient, task_id: str, **changes) -> httpx.Response:
    task = client.get(f"/api/tasks/{task_id}").json()
    return client.patch(
        f"/api/tasks/{task_id}",
        json={"expected_version": task["version"], "changes": changes},
    )


def test_editing_keeps_the_creation_rules(client: TestClient) -> None:
    task_id = create(client, "pr_open").json()["id"]
    res = edit(client, task_id, required_capabilities=[])
    assert (res.status_code, code(res)) == (422, "deliverable_capability")
    res = edit(client, task_id, deliverable="response")
    assert res.status_code == 200, res.text
    assert res.json()["deliverable"] == "response"
    assert edit(client, task_id, required_capabilities=[]).status_code == 200
    res = edit(client, task_id, deliverable="pr_merged")
    assert (res.status_code, code(res)) == (422, "deliverable_capability")


def test_lowering_the_deliverable_in_review_unblocks_the_accept(
    client: TestClient,
) -> None:
    task_id, decision_id = in_review(client, "pr_green")
    forge(client, full_forge(open_pr(), statuses=[]))
    accept = {"choice": "accept", "text": ""}
    res = client.post(f"/api/decisions/{decision_id}/answer", json=accept)
    assert code(res) == "deliverable_unverified"
    assert edit(client, task_id, deliverable="pr_open").status_code == 200
    res = client.post(f"/api/decisions/{decision_id}/answer", json=accept)
    assert res.status_code == 200, res.text
    assert state(client, task_id) == "succeeded"


# -- binding an unreported pull request (3.7) ----------------------------------------

REPOSITORY = "https://git.example.test/team/argus"
OTHER = "c" * 40


def bind(client: TestClient, task_id: str, **body) -> httpx.Response:
    body = {
        "repository": REPOSITORY,
        "number": 12,
        "head_sha": HEAD,
        "reason": "worker opened #12 but never reported it",
        **body,
    }
    return client.post(f"/api/tasks/{task_id}/reconcile-pr", json=body)


def bindings(client: TestClient, task_id: str) -> list[dict]:
    events = client.get("/api/events", params={"subject_id": task_id}).json()
    return [e for e in events if e["kind"] == "task.pr_reconciled"]


def watched_forge(client: TestClient, pull: dict, statuses=None) -> list[str]:
    """Serve a fresh forge (no cached read) and record each read, failing if the
    write lock is held."""
    client.app.state.pr_checker._cache.clear()  # ty: ignore[unresolved-attribute]
    reads: list[str] = []
    serve = full_forge(pull, statuses=statuses)
    db: Database = client.app.state.db  # ty: ignore[unresolved-attribute]

    def handler(request: httpx.Request) -> httpx.Response:
        assert not db._write_lock.locked(), "forge read inside a write transaction"
        reads.append(request.url.path)
        return serve(request)

    forge(client, handler)
    return reads


def failed_for_missing_pr(client: TestClient) -> str:
    """The cdd08585 case: the PR exists but the attempt failed without reporting it."""
    w = worker(client)
    task_id = create(client, "pr_green", max_attempts=1).json()["id"]
    claimed = claim(client, w)
    assert code(complete(client, w, claimed)) == "deliverable_missing"
    res = client.post(
        f"/api/tasks/{task_id}/fail",
        json={"attempt": claimed["attempt"], "error": "deliverable_missing"},
        headers=w,
    )
    assert res.status_code == 200, res.text
    assert state(client, task_id) == "failed"
    return task_id


def test_binding_a_failed_task_records_evidence_and_keeps_it_failed(
    client: TestClient,
) -> None:
    task_id = failed_for_missing_pr(client)
    reads = watched_forge(client, open_pr(), statuses=CI_OK)
    res = bind(client, task_id)
    assert res.status_code == 200, res.text
    assert reads, "the binding reads the forge"
    task = res.json()
    assert (task["state"], task["failures"]) == ("failed", 1)
    [pr] = [a for a in task["continuation"]["artifacts"] if a["kind"] == "pr"]
    assert (pr["ref"], pr["url"], pr["head_sha"]) == ("12", URL, HEAD)
    assert (pr["state"], pr["provenance"]) == ("open", "orchestrator")
    [event] = bindings(client, task_id)
    assert event["actor"] == "admin:preset"
    assert event["data"] == {
        "reason": "worker opened #12 but never reported it",
        "repository": REPOSITORY,
        "number": 12,
        "url": URL,
        "head_sha": HEAD,
        "pr_state": "open",
        "task_state": "failed",
        "revision": task["continuation"]["revision"],
    }
    assert (
        client.get("/api/decisions", params={"subject_id": task_id}).json()["items"]
        == []
    )

    # The existing retry is the way on; the next attempt meets the gate without
    # reporting the PR again, and the accept still reads the forge.
    assert client.post(f"/api/tasks/{task_id}/retry").status_code == 200
    w = worker(client)
    claimed = claim(client, w)
    assert claimed["id"] == task_id
    assert complete(client, w, claimed).json()["state"] == "in_review"
    [decision] = client.get("/api/decisions", params={"subject_id": task_id}).json()[
        "items"
    ]
    accept = {"choice": "accept", "text": ""}
    watched_forge(client, open_pr(), statuses=[])
    res = client.post(f"/api/decisions/{decision['id']}/answer", json=accept)
    assert code(res) == "deliverable_unverified"
    watched_forge(client, open_pr(), statuses=CI_OK)
    res = client.post(f"/api/decisions/{decision['id']}/answer", json=accept)
    assert res.status_code == 200, res.text
    assert state(client, task_id) == "succeeded"


def test_binding_is_idempotent_and_refuses_another_head(client: TestClient) -> None:
    task_id = failed_for_missing_pr(client)
    watched_forge(client, open_pr())
    first = bind(client, task_id)
    assert first.status_code == 200, first.text
    reads = watched_forge(client, open_pr())
    again = bind(client, task_id, reason="replayed")
    assert again.status_code == 200, again.text
    assert again.json()["continuation"] == first.json()["continuation"]
    assert reads == [], "a replay does not read the forge"
    watched_forge(client, open_pr(head={"sha": OTHER, "ref": "feat"}))
    res = bind(client, task_id, head_sha=OTHER)
    assert (res.status_code, code(res)) == (409, "pr_already_bound")
    assert len(bindings(client, task_id)) == 1


@pytest.mark.parametrize(
    ("body", "pull", "outcome"),
    [
        ({"head_sha": OTHER}, open_pr(), (409, "pr_stale_head")),
        ({}, None, (409, "pr_unverified")),  # unreadable forge
        (
            {"repository": "https://git.example.test/team/other"},
            open_pr(),
            (422, "pr_wrong_repository"),
        ),
        (
            {"repository": "https://evil.example/team/argus"},
            open_pr(),
            (422, "pr_wrong_repository"),
        ),
    ],
)
def test_binding_fails_closed(
    client: TestClient, body: dict, pull: dict | None, outcome: tuple[int, str]
) -> None:
    task_id = failed_for_missing_pr(client)
    reads: list[str] = []
    if pull is None:
        forge(client, lambda request: httpx.Response(500))
    else:
        reads = watched_forge(client, pull)
    before = client.get(f"/api/tasks/{task_id}").json()
    res = bind(client, task_id, **body)
    assert (res.status_code, code(res)) == outcome
    if outcome[1] == "pr_wrong_repository":
        assert reads == [], "no credential goes to a repository from input"
    after = client.get(f"/api/tasks/{task_id}").json()
    assert after["continuation"] == before["continuation"]
    assert after["state"] == "failed"
    assert bindings(client, task_id) == []


def test_binding_a_pending_deliverable_keeps_the_closure_gate(
    client: TestClient,
) -> None:
    """A pr_green task with no review lands in_review as deliverable_pending; the
    binding neither decides nor waives the exact-head check."""
    w = worker(client)
    task_id = create(client, "pr_green", review="none").json()["id"]
    claimed = claim(client, w)
    pr_opened(client, w, claimed)
    assert complete(client, w, claimed).json()["state"] == "in_review"
    [decision] = client.get("/api/decisions", params={"subject_id": task_id}).json()[
        "items"
    ]
    watched_forge(client, open_pr(), statuses=[])
    res = bind(client, task_id)
    assert res.status_code == 200, res.text
    assert res.json()["state"] == "in_review"
    assert bindings(client, task_id)[0]["data"]["task_state"] == "in_review"
    accept = {"choice": "accept", "text": ""}
    res = client.post(f"/api/decisions/{decision['id']}/answer", json=accept)
    assert code(res) == "deliverable_unverified"
    assert state(client, task_id) == "in_review"


@pytest.mark.parametrize("terminal", ["succeeded", "cancelled"])
def test_binding_never_resurrects_a_terminal_task(
    client: TestClient, terminal: str
) -> None:
    task_id = create(client, "pr_green").json()["id"]
    db: Database = client.app.state.db  # ty: ignore[unresolved-attribute]

    async def force() -> None:
        async with db.write() as tx:
            await tx.conn.execute(
                "UPDATE tasks SET state = ? WHERE id = ?", (terminal, task_id)
            )

    assert client.portal is not None
    client.portal.call(force)
    watched_forge(client, merged_pr())
    res = bind(client, task_id)
    assert res.status_code == 200, res.text
    assert res.json()["state"] == terminal
    assert bindings(client, task_id)[0]["data"]["pr_state"] == "merged"


def test_binding_needs_a_manager(client: TestClient) -> None:
    task_id = create(client, "pr_green").json()["id"]
    res = client.post(
        f"/api/tasks/{task_id}/reconcile-pr",
        json={"repository": REPOSITORY, "number": 12, "head_sha": HEAD, "reason": "r"},
        headers=worker(client),
    )
    assert res.status_code == 403


# -- a failed task whose bound PR merged goes to review (3.8) -------------------------
#
# Modelled on task 1a7c140b: its COMPLETE was refused `deliverable_missing` (the
# channel then failed the attempt as `channel_error`), while its PR #154 was open and
# later merged. Every value below is a fixture on a temporary database and a fake forge.

INCIDENT_PR = 154
INCIDENT_URL = "https://git.example.test/team/argus/pulls/154"
INCIDENT_HEAD = "632b610e18d3b6db6b060af595f67982df3da7e3"
INCIDENT_MERGE = "91f45bb40feaabe06f43739d0e8ff3700dd1fdcc"
INCIDENT_BRANCH = "argus/task-1a7c140b"
CHECKLIST = ["the repository preflight names its cause"]
REFUSED = (
    "The worker's reply could not be delivered (422 deliverable_missing): "
    "the deliverable pr_merged needs a pull request"
)


def pr_154(**extra) -> dict:
    return {
        "state": "closed",
        "merged": True,
        "merged_at": "2026-11-01T18:00:00Z",
        "merge_commit_sha": INCIDENT_MERGE,
        "head": {"sha": INCIDENT_HEAD, "ref": INCIDENT_BRANCH},
        "base": {"sha": BASE, "ref": "main"},
        **extra,
    }


def incident_forge(client: TestClient, pull: dict | None) -> list[str]:
    """Serve PR #154 fresh (no cached read), recording each read and failing if the
    write lock is held. `None` is a forge that does not know the PR."""
    client.app.state.pr_checker._cache.clear()  # ty: ignore[unresolved-attribute]
    db: Database = client.app.state.db  # ty: ignore[unresolved-attribute]
    serve = full_forge(pull or {})
    reads: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert not db._write_lock.locked(), "forge read inside a write transaction"
        reads.append(request.url.path)
        if pull is None:
            return httpx.Response(404, json={"message": "not found"})
        if request.url.path.endswith(f"/pulls/{INCIDENT_PR}"):
            return httpx.Response(200, json=pull)
        return serve(request)

    forge(client, handler)
    return reads


def failed_like_1a7c140b(client: TestClient, *, reason: str = "channel_error") -> str:
    w = worker(client)
    task_id = create(client, "pr_merged", max_attempts=1, acceptance=CHECKLIST).json()[
        "id"
    ]
    claimed = claim(client, w)
    res = client.post(
        f"/api/tasks/{task_id}/progress",
        json={
            "attempt": claimed["attempt"],
            "message": "pushed",
            "kind": "commit_created",
            "data": {"branch": INCIDENT_BRANCH, "sha": INCIDENT_HEAD},
        },
        headers=w,
    )
    assert res.status_code in (200, 201), res.text
    # Prose is not evidence: a summary that says the PR merged meets no gate.
    refused = complete(client, w, claimed, summary=f"Merged in {INCIDENT_URL}.")
    assert (refused.status_code, code(refused)) == (422, "deliverable_missing")
    res = client.post(
        f"/api/tasks/{task_id}/fail",
        json={"attempt": claimed["attempt"], "error": REFUSED, "reason": reason},
        headers=w,
    )
    assert res.status_code == 200, res.text
    assert state(client, task_id) == "failed"
    return task_id


def events(client: TestClient, task_id: str, kind: str) -> list[dict]:
    found = client.get("/api/events", params={"subject_id": task_id}).json()
    return [e for e in found if e["kind"] == kind]


def failure_event(client: TestClient, task_id: str) -> dict:
    return [
        e
        for e in client.get("/api/events", params={"subject_id": task_id}).json()
        if e["data"].get("to") == "failed"
    ][-1]


def bind_154(client: TestClient, task_id: str) -> None:
    incident_forge(client, pr_154())
    res = bind(
        client,
        task_id,
        number=INCIDENT_PR,
        head_sha=INCIDENT_HEAD,
        reason="the worker opened #154 but never reported it",
    )
    assert res.status_code == 200, res.text


def reconcile(client: TestClient, task_id: str, **body) -> httpx.Response:
    task = client.get(f"/api/tasks/{task_id}").json()
    body = {
        "repository": REPOSITORY,
        "number": INCIDENT_PR,
        "head_sha": INCIDENT_HEAD,
        "attempt": task["attempt"],
        "failure_event_id": failure_event(client, task_id)["id"],
        "reason": "PR #154 merged at its head; review the delivery",
        **body,
    }
    return client.post(f"/api/tasks/{task_id}/reconcile-delivery", json=body)


def test_incident_1a7c140b_binding_alone_leaves_no_way_to_review(
    client: TestClient,
) -> None:
    """The gap: the merged PR is bound and the delivery shows it, but the task stays
    failed with nothing to accept; only a rerun (`retry`) moved it on."""
    task_id = failed_like_1a7c140b(client)
    bind_154(client, task_id)
    task = client.get(f"/api/tasks/{task_id}").json()
    assert (task["state"], task["failures"]) == ("failed", 1)
    incident_forge(client, pr_154())
    seen = client.get(f"/api/tasks/{task_id}/delivery").json()
    assert (seen["pr"], seen["pr_state"]) == (INCIDENT_URL, "merged")
    assert seen["accept_blocked"] == "not_in_review"
    assert seen["accept_decision_id"] is None
    assert seen["checklist"]["pending"] == 1
    assert (
        client.get("/api/decisions", params={"subject_id": task_id}).json()["items"]
        == []
    )


def test_prose_naming_a_merged_pr_is_not_evidence(client: TestClient) -> None:
    """The summary and progress name #154 and the forge shows it merged, but nothing
    bound it: the reconciliation refuses before reading the forge."""
    task_id = failed_like_1a7c140b(client)
    reads = incident_forge(client, pr_154())
    res = reconcile(client, task_id)
    assert (res.status_code, code(res)) == (409, "pr_not_bound")
    assert reads == []
    assert state(client, task_id) == "failed"


def test_reconciling_a_merged_delivery_opens_review_and_keeps_every_gate(
    client: TestClient,
) -> None:
    task_id = failed_like_1a7c140b(client)
    bind_154(client, task_id)
    before = client.get(f"/api/tasks/{task_id}").json()
    failed = failure_event(client, task_id)
    attempts = client.get(f"/api/tasks/{task_id}/attempts").json()
    reads = incident_forge(client, pr_154())
    res = reconcile(client, task_id)
    assert res.status_code == 200, res.text
    assert reads, "the reconciliation reads the forge"
    task = res.json()
    assert task["state"] == "in_review"
    # The failure history stays: attempt, failures, error, the fail event, the
    # attempt records; the checklist is exactly as it was.
    for key in ("attempt", "failures", "error", "continuation"):
        assert task[key] == before[key], key
    assert failure_event(client, task_id) == failed
    assert client.get(f"/api/tasks/{task_id}/attempts").json() == attempts
    [event] = events(client, task_id, "task.delivery_reconciled")
    [decision] = client.get("/api/decisions", params={"subject_id": task_id}).json()[
        "items"
    ]
    assert event["actor"] == "admin:preset"
    assert event["data"] == {
        "from": "failed",
        "to": "in_review",
        "attempt": before["attempt"],
        "failures": 1,
        "decision_id": decision["id"],
        "reason": "PR #154 merged at its head; review the delivery",
        "failure_event_id": failed["id"],
        "failure_reason": "channel_error",
        "failure_class": "deliverable_missing",
        "repository": REPOSITORY,
        "number": INCIDENT_PR,
        "url": INCIDENT_URL,
        "head_sha": INCIDENT_HEAD,
        "head_ref": INCIDENT_BRANCH,
        "merge_sha": INCIDENT_MERGE,
        "merged_at": "2026-11-01T18:00:00+00:00",
        "revision": before["continuation"]["revision"],
    }
    assert events(client, task_id, "task.retried") == []
    assert (decision["kind"], decision["audience"]) == ("review", "orchestrator")
    # Merged is not accepted: the delivery still says the checklist is pending and
    # the task waits for the decision.
    seen = client.get(f"/api/tasks/{task_id}/delivery").json()
    assert seen["accept_blocked"] == "checklist_pending"
    assert seen["checklist"]["pending"] == 1
    res = client.post(
        f"/api/decisions/{decision['id']}/answer",
        json={"choice": "accept", "text": ""},
        headers=worker(client),
    )
    assert res.status_code == 403
    assert state(client, task_id) == "in_review"
    # The accept still reads the forge, at the bound PR.
    incident_forge(client, None)
    res = client.post(
        f"/api/decisions/{decision['id']}/answer",
        json={"choice": "accept", "text": ""},
    )
    assert (res.status_code, code(res)) == (409, "deliverable_unverified")
    assert state(client, task_id) == "in_review"


def test_reconciling_twice_is_one_transition(client: TestClient) -> None:
    task_id = failed_like_1a7c140b(client)
    bind_154(client, task_id)
    incident_forge(client, pr_154())
    first = reconcile(client, task_id)
    assert first.status_code == 200, first.text
    reads = incident_forge(client, pr_154())
    body = {
        "repository": REPOSITORY,
        "number": INCIDENT_PR,
        "head_sha": INCIDENT_HEAD,
        "attempt": first.json()["attempt"],
        "failure_event_id": failure_event(client, task_id)["id"],
        "reason": "replayed",
    }
    again = client.post(f"/api/tasks/{task_id}/reconcile-delivery", json=body)
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert reads == [], "a replay does not read the forge"
    assert len(events(client, task_id, "task.delivery_reconciled")) == 1
    assert (
        len(
            client.get("/api/decisions", params={"subject_id": task_id}).json()["items"]
        )
        == 1
    )


def test_concurrent_reconciliations_make_one_transition(client: TestClient) -> None:
    task_id = failed_like_1a7c140b(client)
    bind_154(client, task_id)
    incident_forge(client, pr_154())
    task = client.get(f"/api/tasks/{task_id}").json()
    body = DeliveryReconciliation(
        repository=REPOSITORY,
        number=INCIDENT_PR,
        head_sha=INCIDENT_HEAD,
        attempt=task["attempt"],
        failure_event_id=failure_event(client, task_id)["id"],
        reason="race",
    )
    app_state = client.app.state  # ty: ignore[unresolved-attribute]

    async def race() -> list:
        return await asyncio.gather(
            *(
                pr_binding.reconcile_delivery(
                    app_state.db, app_state.pr_checker, task_id, body, actor
                )
                for actor in ("orchestrator:a", "orchestrator:b")
            )
        )

    assert client.portal is not None
    results = client.portal.call(race)
    assert [t.state.value for t in results] == ["in_review", "in_review"]
    assert len(events(client, task_id, "task.delivery_reconciled")) == 1
    assert (
        len(
            client.get("/api/decisions", params={"subject_id": task_id}).json()["items"]
        )
        == 1
    )


@pytest.mark.parametrize(
    ("body", "pull", "outcome"),
    [
        (
            {"repository": "https://git.example.test/team/other"},
            pr_154(),
            (422, "pr_wrong_repository"),
        ),
        ({"head_sha": "c" * 40}, pr_154(), (409, "pr_stale_head")),
        (
            {},
            pr_154(head={"sha": INCIDENT_HEAD, "ref": "someone-else"}),
            (409, "pr_branch_mismatch"),
        ),
        ({"attempt": 7}, pr_154(), (409, "stale_failure")),
        ({"failure_event_id": 1}, pr_154(), (409, "stale_failure")),
        (
            {},
            pr_154(state="open", merged=False, merged_at=None),
            (409, "pr_not_merged"),
        ),
        ({}, pr_154(merge_commit_sha=None), (409, "pr_unverified")),
        ({}, None, (409, "pr_unverified")),
    ],
)
def test_reconciling_fails_closed(
    client: TestClient, body: dict, pull: dict | None, outcome: tuple[int, str]
) -> None:
    task_id = failed_like_1a7c140b(client)
    bind_154(client, task_id)
    before = client.get(f"/api/tasks/{task_id}").json()
    incident_forge(client, pull)
    res = reconcile(client, task_id, **body)
    assert (res.status_code, code(res)) == outcome
    assert client.get(f"/api/tasks/{task_id}").json() == before
    assert events(client, task_id, "task.delivery_reconciled") == []


def test_reconciling_refuses_a_second_pull_request(client: TestClient) -> None:
    task_id = failed_like_1a7c140b(client)
    bind_154(client, task_id)
    watched_forge(client, merged_pr())
    assert bind(client, task_id).status_code == 200  # PR #12 bound as well
    incident_forge(client, pr_154())
    res = reconcile(client, task_id)
    assert (res.status_code, code(res)) == (409, "pr_ambiguous")
    assert state(client, task_id) == "failed"


def test_reconciling_needs_a_manager(client: TestClient) -> None:
    task_id = failed_like_1a7c140b(client)
    bind_154(client, task_id)
    res = client.post(
        f"/api/tasks/{task_id}/reconcile-delivery",
        json={
            "repository": REPOSITORY,
            "number": INCIDENT_PR,
            "head_sha": INCIDENT_HEAD,
            "attempt": 1,
            "failure_event_id": failure_event(client, task_id)["id"],
            "reason": "r",
        },
        headers=worker(client),
    )
    assert res.status_code == 403
    assert state(client, task_id) == "failed"


def test_reconciling_only_takes_a_deliverable_missing_failure(
    client: TestClient,
) -> None:
    """Another failure (here the worker's own FAIL text) is not the gap this closes,
    even when it quotes the refusal."""
    task_id = failed_like_1a7c140b(client, reason="worker_fail")
    bind_154(client, task_id)
    incident_forge(client, pr_154())
    res = reconcile(client, task_id)
    assert (res.status_code, code(res)) == (409, "failure_not_deliverable_missing")
    assert state(client, task_id) == "failed"


@pytest.mark.parametrize("other", ["succeeded", "cancelled", "ready"])
def test_reconciling_leaves_other_states_alone(client: TestClient, other: str) -> None:
    task_id = failed_like_1a7c140b(client)
    bind_154(client, task_id)
    failed_id = failure_event(client, task_id)["id"]
    db: Database = client.app.state.db  # ty: ignore[unresolved-attribute]

    async def force() -> None:
        async with db.write() as tx:
            await tx.conn.execute(
                "UPDATE tasks SET state = ? WHERE id = ?", (other, task_id)
            )

    assert client.portal is not None
    client.portal.call(force)
    incident_forge(client, pr_154())
    res = reconcile(client, task_id, failure_event_id=failed_id)
    assert (res.status_code, code(res)) == (409, "task_not_failed")
    assert state(client, task_id) == other
    assert events(client, task_id, "task.delivery_reconciled") == []
