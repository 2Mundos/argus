"""Authorized worker merge resumed after exact-head independent approval
(docs/specs/pre-merge-independent-review.spec.md 3.6).

The forge is a stateful mock; Argus never merges, the test plays the worker that holds
the scoped merge authorization and flips the PR to merged on the mock forge.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from argus_api import main as _main  # noqa: F401 - loads packages in order
from argus_api.tasks import merge_resume, review_settle
from fastapi.testclient import TestClient
from test_execution_routes import fleet as _fleet_fixture
from test_execution_routes import open_decisions, parent_events
from test_pre_merge_independent_review import (
    HEAD_1,
    HEAD_2,
    PR_450_NUM,
    PR_450_URL,
    StatefulForge,
    _claim,
    _configure_chains,
    _record_model,
    _register_worker,
    api_client,  # noqa: F401 - fixture
)

fleet = _fleet_fixture


class Flow:
    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.forge = StatefulForge()
        self.checker = self.forge.install(client)
        self.db = client.app.state.db  # type: ignore[attr-defined] - Starlette State dynamic attr
        _configure_chains(client)
        for key, value in (
            ("review.independent", {"mode": "different_provider", "max_rounds": 3}),
            ("delivery.auto_merge", True),
            ("review.acceptance", False),
        ):
            res = client.put(f"/api/settings/{key}", json={"value": value})
            assert res.status_code == 200, res.text
        _, self.headers = _register_worker(client)

    def task(self, detail: bool = False) -> dict[str, Any]:
        return self.client.get(f"/api/tasks/{self.parent_id}").json()

    def settle(self) -> bool:
        self.checker._cache.clear()
        return asyncio.run(
            review_settle.settle_task(self.db, self.checker, self.parent_id)
        )

    def events(self, kind: str) -> list[dict[str, Any]]:
        return asyncio.run(parent_events(self.db, self.parent_id, kind))

    def reviewers(self) -> int:
        async def count() -> int:
            async with (
                self.db.read() as conn,
                conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE parent_id = ?"
                    " AND kind = 'code_review'",
                    (self.parent_id,),
                ) as cur,
            ):
                return (await cur.fetchone())[0]

        return asyncio.run(count())

    def open_and_review(self) -> None:
        """Attempt 1 opens PR 450 (green on HEAD_1); the independent reviewer approves."""
        created = self.client.post(
            "/api/tasks",
            json={
                "title": "Ship feature",
                "project": "demo",
                "spec": "implement",
                "difficulty": "standard",
                "deliverable": "pr_merged",
                "review": "none",
                "required_capabilities": ["repository:write"],
            },
        )
        assert created.status_code == 201, created.text
        self.parent_id = created.json()["id"]
        impl = _claim(self.client, self.headers)
        self.forge.set_pr(
            PR_450_NUM, head_sha=HEAD_1, head_ref=f"argus/task-{self.parent_id[:8]}"
        )
        self.forge.set_ci(HEAD_1, "success")
        res = self.client.post(
            f"/api/tasks/{self.parent_id}/progress",
            json={
                "attempt": 1,
                "message": "opened PR",
                "kind": "pr_opened",
                "data": {"number": PR_450_NUM, "url": PR_450_URL},
            },
            headers=self.headers,
        )
        assert res.status_code == 200, res.text
        _record_model(
            self.client, self.headers, impl,
            engine="claude", provider="anthropic", model="claude-opus-5-5",
        )  # fmt: skip
        done = self.client.post(
            f"/api/tasks/{self.parent_id}/complete",
            json={"attempt": 1, "result": {"summary": "ready"}},
            headers=self.headers,
        )
        assert done.status_code == 200, done.text
        assert self.task()["state"] == "in_review"
        self.rev = _claim(self.client, self.headers)
        assert self.rev["kind"] == "code_review"
        # Nothing may resume the merge while the reviewer is still running.
        assert self.settle() is False
        assert self.task()["state"] == "in_review"
        assert self.events(merge_resume.RESUMED_EVENT) == []

    def approve(self) -> None:
        _record_model(
            self.client, self.headers, self.rev,
            engine="codex", provider="openai", model="gpt-5.5",
        )  # fmt: skip
        res = self.client.post(
            f"/api/tasks/{self.rev['id']}/complete",
            json={
                "attempt": self.rev["attempt"],
                "result": {
                    "summary": "LGTM",
                    "data": {"verdict": "approve", "findings": [], "head_sha": HEAD_1},
                },
            },
            headers=self.headers,
        )
        assert res.status_code == 200, res.text
        assert self.task()["block_reason"] == "awaiting_forge"


def test_approved_open_pr_resumes_worker_merge_and_settles_on_forge_merge(
    api_client: TestClient,  # noqa: F811 - fixture
) -> None:
    flow = Flow(api_client)
    flow.open_and_review()
    flow.approve()

    # Approval alone merges nothing; the sweep requeues the parent once.
    assert flow.settle() is False
    resumed = flow.task()
    assert resumed["state"] == "ready"
    assert "argus-task merge" in resumed["spec"]
    # Duplicate or concurrent wakeups find a ready task and do nothing.
    assert flow.settle() is False
    assert len(flow.events(merge_resume.RESUMED_EVENT)) == 1

    merge_attempt = _claim(api_client, flow.headers)
    assert merge_attempt["id"] == flow.parent_id and merge_attempt["attempt"] == 2
    assert merge_attempt["independent_review_approved_head"] == HEAD_1
    assert merge_attempt["independent_review_approved_pr"] == PR_450_URL
    assert flow.forge.prs[PR_450_NUM]["merged"] is False  # not merged before the worker

    # The worker's `argus-task merge` lands the PR at the approved head.
    flow.forge.set_pr(
        PR_450_NUM, head_sha=HEAD_1, merged=True, head_ref=f"argus/task-{flow.parent_id[:8]}"
    )  # fmt: skip
    flow.checker._cache.clear()
    done = api_client.post(
        f"/api/tasks/{flow.parent_id}/complete",
        json={"attempt": 2, "result": {"summary": "merged"}},
        headers=flow.headers,
    )
    assert done.status_code == 200, done.text
    final = flow.task()
    assert final["state"] == "succeeded" and final["block_reason"] is None
    assert flow.reviewers() == 1  # the merged head is not reviewed again
    assert flow.settle() is False  # retries are inert once settled


def test_merge_attempt_that_leaves_pr_open_asks_instead_of_looping(
    api_client: TestClient,  # noqa: F811 - fixture
) -> None:
    flow = Flow(api_client)
    flow.open_and_review()
    flow.approve()
    assert flow.settle() is False
    assert flow.task()["state"] == "ready"
    _claim(api_client, flow.headers)
    done = api_client.post(
        f"/api/tasks/{flow.parent_id}/complete",
        json={"attempt": 2, "result": {"summary": "could not merge"}},
        headers=flow.headers,
    )
    assert done.status_code == 200, done.text
    assert flow.task()["state"] == "in_review"
    assert flow.reviewers() == 1
    assert len(asyncio.run(open_decisions(flow.db, flow.parent_id))) == 1
    assert flow.settle() is False  # no second automatic attempt for this approval
    assert len(flow.events(merge_resume.RESUMED_EVENT)) == 1


@pytest.mark.parametrize(
    "change",
    ["head_moved", "pr_closed", "ci_failed", "ci_pending", "grant_revoked"],
)
def test_changed_forge_or_grants_never_resume_the_merge(
    api_client: TestClient,  # noqa: F811 - fixture
    change: str,
) -> None:
    flow = Flow(api_client)
    flow.open_and_review()
    flow.approve()
    ref = f"argus/task-{flow.parent_id[:8]}"
    if change == "head_moved":
        flow.forge.set_pr(PR_450_NUM, head_sha=HEAD_2, head_ref=ref)
        flow.forge.set_ci(HEAD_2, "success")
    elif change == "pr_closed":
        flow.forge.set_pr(PR_450_NUM, head_sha=HEAD_1, state="closed", head_ref=ref)
    elif change == "ci_failed":
        flow.forge.set_ci(HEAD_1, "failure")
    elif change == "ci_pending":
        flow.forge.set_ci(HEAD_1, "pending")
    else:
        res = api_client.put("/api/settings/delivery.auto_merge", json={"value": False})
        assert res.status_code == 200, res.text
    assert flow.settle() is False
    assert flow.task()["state"] == "in_review"
    assert flow.events(merge_resume.RESUMED_EVENT) == []
    if change == "grant_revoked":
        (blocked,) = flow.events(merge_resume.BLOCKED_EVENT)
        assert blocked["reason"] == "merge_not_authorized"
        assert flow.settle() is False
        assert len(flow.events(merge_resume.BLOCKED_EVENT)) == 1  # not repeated
