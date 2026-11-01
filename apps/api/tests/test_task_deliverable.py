"""Task deliverable (docs/specs/task-deliverable.spec.md): the creation rules, the
completion gate, the forge-read accept gate and editing. The forge is a mock transport."""

from collections.abc import Callable, Generator
from pathlib import Path

import httpx2 as httpx
import pytest
from argus_api import model_policy
from argus_api.db import Database
from argus_api.main import app
from argus_api.tasks import independent_review as indep
from argus_api.tasks import service
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
