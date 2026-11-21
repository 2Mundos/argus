"""Pre-merge independent review workflow (docs/specs/pre-merge-independent-review.spec.md).

Covers:
1. End-to-end deterministic workflow: PR open -> exact-head CI -> separate-session,
   different-model review -> request_changes -> correction on new head -> review of
   new head -> authorized merge -> forge confirmation.
2. Blocking on head change, replacement PR, stale verdict, failed review, and no
   eligible reviewer; idempotent spawns/retries and concurrent settlements.
3. Review chain precedence (parent difficulty first, cross-difficulty review/execution
   chains in deterministic order, no invented models or author fallback, actionable
   blocked decision).
4. Same pool/machine with separate sessions & different models; review-disabled and
   non-code tasks unchanged; existing auth/protections/redaction preserved.
5. Regression for PR #450/#451: approval for PR A / head X never permits merge or
   acceptance of replacement PR B / head Y.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from api_support import FakeClock
from argus_api import main as _main  # noqa: F401 - loads packages in order
from argus_api.accounts import service as accounts
from argus_api.db import Database
from argus_api.decisions import store as decisions
from argus_api.events import record
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from argus_api.settings import service as settings_svc
from argus_api.tasks import (
    deliverable,
    delivery,
    review_settle,
)
from argus_api.tasks import (
    execution_routes as routes,
)
from argus_api.tasks import (
    independent_review as indep,
)
from argus_api.tasks import (
    service as tasks,
)
from argus_core.accounts import AccountUpdate
from argus_core.delivery import CheckState, Delivery, FactOrigin, PrState, ShaFact
from argus_core.settings import IndependentReviewSetting
from argus_core.tasks import TaskResult, TaskState
from fastapi.testclient import TestClient
from test_execution_routes import (
    CAPS,
    CODEX_ALT,
    OPUS_ALT,
    SONNET_ALT,
    answer,
    direct,
    explicit,
    make_task,
    open_decisions,
    parent_events,
    record_execution,
    review_policy,
    reviewed_parent,
    selector,
    spawn,
    state,
)
from test_execution_routes import fleet as _fleet_fixture
from test_mcp import ADMIN_TOKEN

fleet = _fleet_fixture

REPO = "https://github.com/acme/widget"
PR_450_NUM = 450
PR_451_NUM = 451
PR_450_URL = f"{REPO}/pull/{PR_450_NUM}"
PR_451_URL = f"{REPO}/pull/{PR_451_NUM}"
HEAD_1 = "1" * 40
HEAD_2 = "2" * 40
HEAD_Y = "9" * 40
MERGE_SHA = "c" * 40
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class StatefulForge:
    """In-memory GitHub forge state driving PrChecker via MockTransport."""

    def __init__(self) -> None:
        self.prs: dict[int, dict[str, Any]] = {}
        self.statuses: dict[str, list[dict[str, Any]]] = {}

    def set_pr(
        self,
        number: int,
        *,
        head_sha: str,
        state: str = "open",
        merged: bool = False,
        head_ref: str = "argus/task-branch",
        merge_commit_sha: str | None = None,
    ) -> None:
        now_iso = datetime.now(UTC).isoformat()
        self.prs[number] = {
            "number": number,
            "html_url": f"{REPO}/pull/{number}",
            "state": "closed" if merged else state,
            "merged": merged,
            "merged_at": now_iso if merged else None,
            "created_at": now_iso,
            "merge_commit_sha": (merge_commit_sha or MERGE_SHA) if merged else None,
            "head": {"sha": head_sha, "ref": head_ref},
            "base": {"sha": "0" * 40, "ref": "main"},
            "mergeable": True,
            "mergeable_state": "clean",
        }

    def set_ci(self, sha: str, state: str) -> None:
        self.statuses[sha.lower()] = [
            {"context": "ci/test", "state": state, "description": state}
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for num, body in self.prs.items():
            if path.endswith(f"/pulls/{num}"):
                return httpx.Response(200, json=body)
            if path.endswith(f"/pulls/{num}/reviews"):
                return httpx.Response(200, json=[])
        for sha, items in self.statuses.items():
            if path.endswith(f"/commits/{sha}/status"):
                return httpx.Response(200, json={"statuses": items})
            if path.endswith(f"/commits/{sha}/check-runs"):
                return httpx.Response(200, json={"check_runs": []})
        if path.endswith("/required_status_checks"):
            return httpx.Response(404, json={"message": "not disclosed"})
        return httpx.Response(404, json={"message": f"unhandled {path}"})

    def install(self, client: TestClient) -> delivery.PrChecker:
        checker = delivery.PrChecker(
            client.app.state.secrets, httpx.MockTransport(self.handler)
        )
        client.app.state.pr_checker = checker
        return checker


@pytest.fixture
def api_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers=ADMIN_HEADERS) as client:
        store = MemorySecretStore()
        client.app.state.secrets = store  # type: ignore[attr-defined] - Starlette State dynamic attr
        client.app.state.pr_checker.secrets = store  # type: ignore[attr-defined] - Starlette State dynamic attr
        assert (
            client.put(
                "/api/projects/demo",
                json={
                    "name": "Demo",
                    "description": "demo project",
                    "repository_url": REPO,
                },
            ).status_code
            == 200
        )
        # Register github repo credential so PrChecker reads the mocked forge.
        res = client.post(
            "/api/accounts",
            json={
                "name": "gh-bot",
                "provider": "github",
                "priority": 10,
                "credential": {"token": "ghp_fixture"},
            },
        )
        assert res.status_code == 201, res.text
        # Register two distinct model providers (anthropic implementer + openai reviewer).
        res = client.post(
            "/api/accounts",
            json={
                "name": "cloud",
                "provider": "anthropic",
                "kind": "api_key",
                "priority": 10,
                "credential": {"api_key": "sk-ant-api03-fixture"},
            },
        )
        assert res.status_code == 201, res.text
        res = client.post(
            "/api/accounts",
            json={
                "name": "codex",
                "provider": "openai",
                "kind": "api_key",
                "priority": 10,
                "credential": {
                    "auth_mode": "apikey",
                    "OPENAI_API_KEY": "sk-openai-fixture",
                },
            },
        )
        assert res.status_code == 201, res.text
        yield client


def _register_worker(client: TestClient) -> tuple[str, dict[str, str]]:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
    caps = sorted({*CAPS, "repository:write"})
    res = client.post(
        "/api/agents/register",
        json={"bootstrap_token": boot["token"], "capabilities": caps},
    )
    assert res.status_code == 201, res.text
    reg = res.json()
    return reg["agent"]["id"], _bearer(reg["token"])


def _end_session(client: TestClient, headers: dict[str, str]) -> None:
    """Release the worker's previous session lease before starting a new session."""
    res = client.post(
        "/api/accounts/release", json={"reason": "session_ended"}, headers=headers
    )
    assert res.status_code == 204, res.text


def _claim(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    _end_session(client, headers)
    caps = sorted({*CAPS, "repository:write"})
    res = client.post(
        "/api/tasks/claim",
        json={
            "capabilities": caps,
            "runtime_providers": ["anthropic", "openai"],
            "wait_s": 0,
        },
        headers=headers,
    )
    assert res.status_code == 200, res.text
    return res.json()


def _record_model(
    client: TestClient,
    headers: dict[str, str],
    claimed: dict[str, Any],
    *,
    engine: str,
    provider: str,
    model: str,
) -> None:
    task_id = claimed["id"]
    attempt = claimed["attempt"]
    body: dict[str, Any] = {
        "attempt": attempt,
        "executions": [
            {
                "key": f"{task_id}:{attempt}:{engine}",
                "layer": "harness",
                "state": "succeeded",
                "engine": engine,
                "provider": provider,
                "configured_model": model,
                "observed_model": model,
                "evidence": "cli_output",
                "started_at": datetime.now(UTC).isoformat(),
            }
        ],
    }
    if claimed.get("execution"):
        body["execution_generation"] = claimed["execution"]["generation"]
    res = client.post(
        f"/api/tasks/{task_id}/models",
        json=body,
        headers=headers,
    )
    assert res.status_code == 200, res.text


def _configure_chains(client: TestClient) -> None:
    exec_chains = {
        "version": 1,
        "difficulties": {
            "standard": [
                explicit("opus", "anthropic", "claude", "claude-opus-5-5"),
            ]
        },
    }
    review_chains = {
        "version": 1,
        "difficulties": {
            "standard": [
                explicit("opus", "anthropic", "claude", "claude-opus-5-5"),
                explicit("codex", "openai", "codex", "gpt-5.5"),
            ]
        },
    }
    assert (
        client.put(
            "/api/settings/execution.chains", json={"value": exec_chains}
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/api/settings/review.chains", json={"value": review_chains}
        ).status_code
        == 200
    )


# ---------------------------------------------------------------------------
# Criterion 1 & 4: End-to-end deterministic pre-merge workflow (same pool/machine,
# separate sessions, different models/providers, non-circular pr_merged)
# ---------------------------------------------------------------------------


def test_pre_merge_e2e_open_ci_review_changes_re_review_authorized_merge_settle(
    api_client: TestClient,
) -> None:
    forge = StatefulForge()
    checker = forge.install(api_client)
    _configure_chains(api_client)
    assert (
        api_client.put(
            "/api/settings/review.independent",
            json={"value": {"mode": "different_provider", "max_rounds": 3}},
        ).status_code
        == 200
    )
    assert (
        api_client.put(
            "/api/settings/delivery.auto_merge", json={"value": True}
        ).status_code
        == 200
    )
    assert (
        api_client.put(
            "/api/settings/review.acceptance", json={"value": False}
        ).status_code
        == 200
    )

    worker_id, w_headers = _register_worker(api_client)

    created = api_client.post(
        "/api/tasks",
        json={
            "title": "Ship pre-merge feature",
            "project": "demo",
            "spec": "implement feature safely",
            "difficulty": "standard",
            "deliverable": "pr_merged",
            "review": "none",
            "required_capabilities": ["repository:write"],
        },
    )
    assert created.status_code == 201, created.text
    parent_id = created.json()["id"]

    # Attempt 1 (implementer session 1 on anthropic / claude-opus-5-5).
    impl_1 = _claim(api_client, w_headers)
    assert impl_1["id"] == parent_id
    assert impl_1["attempt"] == 1
    assert impl_1["assignee_agent_id"] == worker_id
    assert impl_1["independent_review_required"] is True
    assert impl_1["independent_review_approved_head"] is None
    assert impl_1["execution"]["harness"]["model"] == "claude-opus-5-5"

    # PR #450 opened at HEAD_1 with pending CI first: completion must fail closed
    # until exact-head CI is green, and merge is denied before independent review.
    forge.set_pr(PR_450_NUM, head_sha=HEAD_1, head_ref=f"argus/task-{parent_id[:8]}")
    forge.set_ci(HEAD_1, "pending")
    prog = api_client.post(
        f"/api/tasks/{parent_id}/progress",
        json={
            "attempt": 1,
            "message": "opened PR #450",
            "kind": "pr_opened",
            "data": {"number": PR_450_NUM, "url": PR_450_URL},
        },
        headers=w_headers,
    )
    assert prog.status_code == 200, prog.text

    detail = api_client.get(f"/api/tasks/{parent_id}").json()
    assert detail["merge"]["authorized"] is True
    assert detail["merge"]["may_merge"] is False

    pending_complete = api_client.post(
        f"/api/tasks/{parent_id}/complete",
        json={"attempt": 1, "result": {"summary": "PR opened"}},
        headers=w_headers,
    )
    assert pending_complete.status_code == 422
    assert pending_complete.json()["detail"]["code"] == "deliverable_missing"

    # CI passes on exact HEAD_1 while PR #450 is still OPEN (not merged!).
    # Non-circular completion succeeds into in_review and spawns code_review child R1.
    forge.set_ci(HEAD_1, "success")
    checker._cache.clear()
    _record_model(
        api_client,
        w_headers,
        impl_1,
        engine="claude",
        provider="anthropic",
        model="claude-opus-5-5",
    )
    done_1 = api_client.post(
        f"/api/tasks/{parent_id}/complete",
        json={"attempt": 1, "result": {"summary": "ready for pre-merge review"}},
        headers=w_headers,
    )
    assert done_1.status_code == 200, done_1.text
    assert done_1.json()["state"] == "in_review"

    # Same worker on the same pool/machine claims R1 in a separate session with
    # a different model & provider (openai / gpt-5.5) and read-only capabilities.
    rev_1 = _claim(api_client, w_headers)
    assert rev_1["kind"] == "code_review"
    assert rev_1["parent_id"] == parent_id
    assert rev_1["id"] != parent_id
    assert rev_1["assignee_agent_id"] == worker_id
    assert rev_1["required_capabilities"] == ["repository:write"]
    assert rev_1["input"]["fix"]["force_push"] is False
    rev_detail = api_client.get(f"/api/tasks/{rev_1['id']}").json()
    assert rev_detail["merge"]["may_merge"] is False
    assert rev_1["input"]["pr"] == PR_450_URL
    assert rev_1["input"]["head_sha"] == HEAD_1
    assert rev_1["execution"]["harness"]["model"] == "gpt-5.5"
    assert rev_1["engine_lease"]["lease_id"] != impl_1["engine_lease"]["lease_id"]

    # Merge remains blocked while R1 is running.
    assert (
        api_client.get(f"/api/tasks/{parent_id}").json()["merge"]["may_merge"] is False
    )

    # R1 requests changes on HEAD_1 -> parent returns to ready with findings.
    _record_model(
        api_client,
        w_headers,
        rev_1,
        engine="codex",
        provider="openai",
        model="gpt-5.5",
    )
    r1_done = api_client.post(
        f"/api/tasks/{rev_1['id']}/complete",
        json={
            "attempt": rev_1["attempt"],
            "result": {
                "summary": "Found P2 issue",
                "data": {
                    "verdict": "request_changes",
                    "findings": ["P2: fix boundary check"],
                    "head_sha": HEAD_1,
                },
            },
        },
        headers=w_headers,
    )
    assert r1_done.status_code == 200, r1_done.text
    parent_after_r1 = api_client.get(f"/api/tasks/{parent_id}").json()
    assert parent_after_r1["state"] == "ready"
    assert "P2: fix boundary check" in parent_after_r1["spec"]
    assert parent_after_r1["merge"]["may_merge"] is False

    # Attempt 2 (correction -> pushes HEAD_2 to PR #450, CI passes on HEAD_2).
    impl_2 = _claim(api_client, w_headers)
    assert impl_2["id"] == parent_id
    assert impl_2["attempt"] == 2
    assert impl_2["independent_review_required"] is True
    assert impl_2["independent_review_approved_head"] is None

    forge.set_pr(PR_450_NUM, head_sha=HEAD_2, head_ref=f"argus/task-{parent_id[:8]}")
    forge.set_ci(HEAD_2, "success")
    checker._cache.clear()
    _record_model(
        api_client,
        w_headers,
        impl_2,
        engine="claude",
        provider="anthropic",
        model="claude-opus-5-5",
    )
    done_2 = api_client.post(
        f"/api/tasks/{parent_id}/complete",
        json={"attempt": 2, "result": {"summary": "fixed P2 on new head"}},
        headers=w_headers,
    )
    assert done_2.status_code == 200, done_2.text
    assert done_2.json()["state"] == "in_review"

    # R2 inspects exact new head HEAD_2 (never stale HEAD_1!) in a separate session.
    rev_2 = _claim(api_client, w_headers)
    assert rev_2["kind"] == "code_review"
    assert rev_2["id"] != rev_1["id"]
    assert rev_2["input"]["pr"] == PR_450_URL
    assert rev_2["input"]["head_sha"] == HEAD_2
    assert rev_2["execution"]["harness"]["model"] == "gpt-5.5"

    _record_model(
        api_client,
        w_headers,
        rev_2,
        engine="codex",
        provider="openai",
        model="gpt-5.5",
    )
    r2_done = api_client.post(
        f"/api/tasks/{rev_2['id']}/complete",
        json={
            "attempt": rev_2["attempt"],
            "result": {
                "summary": "LGTM on HEAD_2",
                "data": {
                    "verdict": "approve",
                    "findings": [],
                    "head_sha": HEAD_2,
                },
            },
        },
        headers=w_headers,
    )
    assert r2_done.status_code == 200, r2_done.text

    # Approved for exact HEAD_2, still OPEN -> awaiting_forge, and merge is now allowed
    # ONLY for (PR #450, HEAD_2).
    awaiting = api_client.get(f"/api/tasks/{parent_id}").json()
    assert awaiting["state"] == "in_review"
    assert awaiting["block_reason"] == "awaiting_forge"
    assert awaiting["merge"]["authorized"] is True
    assert awaiting["merge"]["may_merge"] is True

    # Authorized merge lands PR #450 at exact HEAD_2 on the forge; review_settle confirms
    # merged state on exact (PR #450, HEAD_2) and transitions parent to succeeded.
    forge.set_pr(
        PR_450_NUM,
        head_sha=HEAD_2,
        merged=True,
        head_ref=f"argus/task-{parent_id[:8]}",
    )
    checker._cache.clear()
    settled = asyncio.run(
        review_settle.settle_task(api_client.app.state.db, checker, parent_id)
    )
    assert settled is True
    final = api_client.get(f"/api/tasks/{parent_id}").json()
    assert final["state"] == "succeeded"
    assert final["block_reason"] is None


# ---------------------------------------------------------------------------
# Criterion 2: Head change, replacement PR, stale verdict, failed review,
# no eligible reviewer block merge; idempotent retries & concurrent settlements
# ---------------------------------------------------------------------------


async def test_head_change_replacement_pr_stale_verdict_failed_review_and_no_reviewer_block_merge(
    db: Database, fleet
) -> None:
    await review_policy(db, [OPUS_ALT, CODEX_ALT])
    async with db.write() as tx:
        await settings_svc.put_setting(
            tx,
            Path(),
            "global",
            "review.independent",
            {"mode": "different_provider", "max_rounds": 3, "max_reviewer_failures": 0},
            "admin:test",
        )
    parent = await reviewed_parent(db, fleet, ("claude-opus-5-5", "anthropic", "cloud"))
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET result = ? WHERE id = ?",
            (
                TaskResult(
                    summary="pr", data={"pr": PR_450_URL, "head_sha": HEAD_1}
                ).model_dump_json(),
                parent.id,
            ),
        )

    # 1. Stale verdict for another SHA is voided and never approves HEAD_1.
    async with db.write() as tx:
        child = await indep.spawn_reviewer(
            tx,
            await tasks.task_row(tx.conn, parent.id),
            pr=PR_450_URL,
            head_sha=HEAD_1,
            setting=IndependentReviewSetting(mode="different_provider"),
            policy={},
            actor="system",
        )
        assert child is not None
        await record_execution(tx, child, 0, "gpt-5.5", "openai", "codex")
        await tx.conn.execute(
            "UPDATE tasks SET state = 'succeeded' WHERE id = ?", (child,)
        )
        await indep.on_review_child_complete(
            tx,
            await tasks.task_row(tx.conn, child),
            TaskResult(
                summary="approved wrong sha",
                data={"verdict": "approve", "findings": [], "head_sha": HEAD_2},
            ),
            setting=IndependentReviewSetting(mode="different_provider"),
            policy={},
            actor="agent:rev",
        )
    async with db.read() as conn:
        row = await tasks.task_row(conn, parent.id)
        allowed, reason = await indep.merge_gate_status(
            conn, row, pr=PR_450_URL, head_sha=HEAD_1
        )
        assert allowed is False
        assert reason == "independent_review_pending"
        assert await indep.reviewed_head_of(conn, parent.id) is None

    # 2. Failed reviewer blocks parent with review_failed and blocks merge.
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT id FROM tasks WHERE parent_id = ? AND kind = 'code_review'"
            " ORDER BY rowid DESC LIMIT 1",
            (parent.id,),
        ) as cur,
    ):
        second_child = (await cur.fetchone())[0]
    async with db.write() as tx:
        await tasks.cancel(tx, second_child, "admin:test")
    async with db.read() as conn:
        row = await tasks.task_row(conn, parent.id)
        assert row["block_reason"] == indep.REVIEW_FAILED
        allowed, reason = await indep.merge_gate_status(
            conn, row, pr=PR_450_URL, head_sha=HEAD_1
        )
        assert allowed is False
        assert reason == indep.REVIEW_FAILED

    # 3. No eligible different reviewer blocks merge with no_independent_reviewer.
    await review_policy(db, [OPUS_ALT])
    blocked_parent = await make_task(db)
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET state = 'in_review' WHERE id = ?", (blocked_parent.id,)
        )
        await record_execution(
            tx, blocked_parent.id, 0, "claude-opus-5-5", "anthropic", "cloud"
        )
        spawned = await indep.spawn_reviewer(
            tx,
            await tasks.task_row(tx.conn, blocked_parent.id),
            pr=PR_450_URL,
            head_sha=HEAD_1,
            setting=IndependentReviewSetting(mode="different_provider"),
            policy={},
            actor="system",
        )
        assert spawned is None
    async with db.read() as conn:
        brow = await tasks.task_row(conn, blocked_parent.id)
        allowed, reason = await indep.merge_gate_status(
            conn, brow, pr=PR_450_URL, head_sha=HEAD_1
        )
        assert allowed is False
        assert reason == indep.NO_INDEPENDENT_REVIEWER

    # 4. An approval on the exact head that lacks its PR or its reviewing child
    #    fails closed instead of skipping the PR or reviewer-model check.
    for missing, reason_expected in (
        ("pr", "independent_review_pr_mismatch"),
        ("child_id", "independent_review_same_model"),
    ):
        bare_parent = await make_task(db)
        approval = {
            "child_id": "rev-child",
            "verdict": "approve",
            "pr": PR_450_URL,
            "head_sha": HEAD_1,
        }
        del approval[missing]
        async with db.write() as tx:
            await tx.conn.execute(
                "UPDATE tasks SET state = 'in_review' WHERE id = ?", (bare_parent.id,)
            )
            await record(
                tx, "agent:rev", "task", bare_parent.id, indep.PASSED_EVENT, approval
            )
        async with db.read() as conn:
            bare_row = await tasks.task_row(conn, bare_parent.id)
            allowed, reason = await indep.merge_gate_status(
                conn, bare_row, pr=PR_450_URL, head_sha=HEAD_1
            )
            assert (allowed, reason) == (False, reason_expected), missing


async def test_idempotent_reviewer_spawn_retries_and_concurrent_settlements(
    db: Database, fleet, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await review_policy(db, [OPUS_ALT, CODEX_ALT])
    async with db.write() as tx:
        await settings_svc.put_setting(
            tx,
            Path(),
            "global",
            "review.independent",
            {"mode": "different_provider", "max_rounds": 3},
            "admin:test",
        )
        await settings_svc.put_setting(
            tx, Path(), "global", settings_svc.ACCEPTANCE_KEY, False, "admin:test"
        )
    parent = await reviewed_parent(db, fleet, ("claude-opus-5-5", "anthropic", "cloud"))
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET deliverable = 'pr_merged', review = 'none' WHERE id = ?",
            (parent.id,),
        )

    # Spawning twice for the exact same (pr, head_sha) is idempotent (one child).
    async with db.write() as tx:
        row = await tasks.task_row(tx.conn, parent.id)
        c1 = await indep.spawn_reviewer(
            tx,
            row,
            pr=PR_450_URL,
            head_sha=HEAD_1,
            setting=IndependentReviewSetting(mode="different_provider"),
            policy={},
            actor="system",
        )
        c2 = await indep.spawn_reviewer(
            tx,
            row,
            pr=PR_450_URL,
            head_sha=HEAD_1,
            setting=IndependentReviewSetting(mode="different_provider"),
            policy={},
            actor="system",
        )
        r1 = await indep.retry_reviewer(tx, parent.id, "human:test")
    assert c1 is not None and c1 == c2 == r1

    # Spawning for a new head creates a new child and cancels the stale active one.
    async with db.write() as tx:
        row = await tasks.task_row(tx.conn, parent.id)
        c_new = await indep.spawn_reviewer(
            tx,
            row,
            pr=PR_450_URL,
            head_sha=HEAD_2,
            setting=IndependentReviewSetting(mode="different_provider"),
            policy={},
            actor="system",
        )
    assert c_new is not None and c_new != c1
    c1_row, _ = await state(db, c1)
    assert c1_row["state"] == "cancelled"

    # Late verdict from superseded child c1 is ignored.
    async with db.write() as tx:
        await record_execution(tx, c1, 0, "gpt-5.5", "openai", "codex")
        await indep.on_review_child_complete(
            tx,
            await tasks.task_row(tx.conn, c1),
            TaskResult(
                summary="late",
                data={"verdict": "approve", "findings": [], "head_sha": HEAD_1},
            ),
            setting=IndependentReviewSetting(mode="different_provider"),
            policy={},
            actor="agent:rev",
        )
    async with db.read() as conn:
        assert await indep.reviewed_head_of(conn, parent.id) is None

    # Approve c_new (replayed twice -> idempotent) while PR is still open -> awaiting_forge.
    open_delivery = Delivery(
        pr=PR_450_URL,
        pr_state=PrState.OPEN,
        merged_at=None,
        checked_at=clock.now,
        flags=[],
        accept_decision_id=None,
        accept_blocked=None,
        head=ShaFact(sha=HEAD_2, source=FactOrigin.FORGE, checked_at=clock.now),
        checks_combined=CheckState.SUCCESS,
    )
    async with db.write() as tx:
        await record_execution(tx, c_new, 0, "gpt-5.5", "openai", "codex")
        await tx.conn.execute(
            "UPDATE tasks SET state = 'succeeded' WHERE id = ?", (c_new,)
        )
        c_row = await tasks.task_row(tx.conn, c_new)
        for _ in range(2):
            await indep.on_review_child_complete(
                tx,
                c_row,
                TaskResult(
                    summary="ok",
                    data={"verdict": "approve", "findings": [], "head_sha": HEAD_2},
                ),
                setting=IndependentReviewSetting(mode="different_provider"),
                policy={},
                actor="agent:rev",
                forge=deliverable.ForgeRead(open_delivery),
            )
    assert len(await parent_events(db, parent.id, indep.PASSED_EVENT)) == 1

    # Concurrent settlements on a moved head spawn at most ONE new reviewer.
    moved_delivery = Delivery(
        pr=PR_450_URL,
        pr_state=PrState.MERGED,
        merged_at=clock.now,
        checked_at=clock.now,
        flags=[],
        accept_decision_id=None,
        accept_blocked=None,
        head=ShaFact(sha=HEAD_Y, source=FactOrigin.FORGE, checked_at=clock.now),
        checks_combined=CheckState.SUCCESS,
    )

    async def fake_delivery_of(conn, checker, task):
        return moved_delivery

    monkeypatch.setattr(delivery, "delivery_of", fake_delivery_of)
    results = await asyncio.gather(
        review_settle.settle_task(db, None, parent.id),  # ty: ignore[invalid-argument-type]
        review_settle.settle_task(db, None, parent.id),  # ty: ignore[invalid-argument-type]
    )
    assert results == [False, False]
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT count(*) FROM tasks WHERE parent_id = ? AND kind = 'code_review'"
            " AND state NOT IN ('succeeded', 'failed', 'cancelled')",
            (parent.id,),
        ) as cur,
    ):
        assert (await cur.fetchone())[0] == 1

    # Now test concurrent settlements when forge confirms the exact merged head:
    # exactly one succeeds and the second is a no-op.
    parent_ok = await make_task(db)
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET state = 'in_review', deliverable = 'pr_merged',"
            " review = 'none', block_reason = ? WHERE id = ?",
            (indep.AWAITING_FORGE, parent_ok.id),
        )
        await record(
            tx,
            "agent:rev",
            "task",
            parent_ok.id,
            indep.PASSED_EVENT,
            {
                "child_id": "ok-child",
                "verdict": "approve",
                "pr": PR_450_URL,
                "head_sha": HEAD_2,
            },
        )
    merged_ok = moved_delivery.model_copy(
        update={
            "head": ShaFact(sha=HEAD_2, source=FactOrigin.FORGE, checked_at=clock.now)
        }
    )

    async def fake_merged_ok(conn, checker, task):
        return merged_ok

    monkeypatch.setattr(delivery, "delivery_of", fake_merged_ok)
    settled_pair = await asyncio.gather(
        review_settle.settle_task(db, None, parent_ok.id),  # ty: ignore[invalid-argument-type]
        review_settle.settle_task(db, None, parent_ok.id),  # ty: ignore[invalid-argument-type]
    )
    assert sorted(settled_pair) == [False, True]
    row_ok, _ = await state(db, parent_ok.id)
    assert row_ok["state"] == "succeeded"


# ---------------------------------------------------------------------------
# Criterion 3: Review chain precedence, cross-difficulty fallback, no author
# fallback / no invented models, actionable blocked decision + retry_reviewer
# ---------------------------------------------------------------------------


async def test_review_chain_precedence_and_cross_difficulty_fallback(
    db: Database, fleet
) -> None:
    # 1. Parent difficulty's own review.chains entry wins first, unchanged.
    async with db.write() as tx:
        await settings_svc.put_setting(
            tx,
            Path(),
            "global",
            routes.REVIEW_KEY,
            {
                "version": 1,
                "difficulties": {
                    "hard": [OPUS_ALT, SONNET_ALT],
                    "simple": [CODEX_ALT],
                },
            },
            "admin:test",
        )
    parent = await reviewed_parent(db, fleet, ("claude-opus-5-5", "anthropic", "cloud"))
    child_own = await spawn(db, parent.id, mode="different_model")
    _, own_route = await state(db, child_own)
    assert own_route.review.selected == "sonnet"
    assert own_route.review.difficulty == "hard"

    # 2. When parent's difficulty ("standard") has NO review.chains entry:
    #    searches other difficulties in review.chains in sorted order, then
    #    execution.chains in sorted order, requiring different model AND provider
    #    AND usable account.
    custom_alt = direct("custom-rev", selector("custom", "custom", "claude"))
    async with db.write() as tx:
        # Remove hard from review.chains; put standard-authored provider only in
        # review.chains["simple"] and eligible codex in execution.chains["hard"]
        # and custom_alt in execution.chains["trivial"].
        await settings_svc.put_setting(
            tx,
            Path(),
            "global",
            routes.REVIEW_KEY,
            {"version": 1, "difficulties": {"simple": [OPUS_ALT, SONNET_ALT]}},
            "admin:test",
        )
        await settings_svc.put_setting(
            tx,
            Path(),
            "global",
            routes.KEY,
            {
                "version": 1,
                "difficulties": {
                    "trivial": [custom_alt],
                    "hard": [CODEX_ALT],
                },
            },
            "admin:test",
        )
    std_parent = await make_task(db, difficulty="standard")
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET state = 'in_review', result = ? WHERE id = ?",
            (
                TaskResult(
                    summary="pr", data={"pr": PR_450_URL, "head_sha": HEAD_1}
                ).model_dump_json(),
                std_parent.id,
            ),
        )
        await record_execution(
            tx, std_parent.id, 0, "claude-opus-5-5", "anthropic", "cloud"
        )
    # Sorted difficulty order in execution.chains: "hard" before "trivial" -> picks codex!
    child_cross = await spawn(db, std_parent.id, mode="different_model")
    _, cross_route = await state(db, child_cross)
    assert cross_route.review.selected == "codex"
    assert cross_route.review.difficulty == "hard"

    # 3. Disable codex account -> deterministic search skips unavailable "hard"
    #    and picks "trivial" (custom-rev) instead of falling back to anthropic!
    async with db.write() as tx:
        await accounts.update_account(
            tx, "codex", AccountUpdate(enabled=False), "admin:test"
        )
    std_parent2 = await make_task(db, difficulty="standard")
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET state = 'in_review', result = ? WHERE id = ?",
            (
                TaskResult(
                    summary="pr", data={"pr": PR_450_URL, "head_sha": HEAD_1}
                ).model_dump_json(),
                std_parent2.id,
            ),
        )
        await record_execution(
            tx, std_parent2.id, 0, "claude-opus-5-5", "anthropic", "cloud"
        )
    child_next = await spawn(db, std_parent2.id, mode="different_model")
    _, next_route = await state(db, child_next)
    assert next_route.review.selected == "custom-rev"
    assert next_route.review.difficulty == "trivial"

    # 4. Disable custom too -> NO eligible different model/provider with usable
    #    account exists. Must NEVER invent a model ID or fall back to author's
    #    anthropic model/provider; records actionable blocked decision instead.
    async with db.write() as tx:
        await accounts.update_account(
            tx, "custom", AccountUpdate(enabled=False), "admin:test"
        )
    blocked_std = await make_task(db, difficulty="standard")
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET state = 'in_review', result = ? WHERE id = ?",
            (
                TaskResult(
                    summary="pr", data={"pr": PR_450_URL, "head_sha": HEAD_1}
                ).model_dump_json(),
                blocked_std.id,
            ),
        )
        await record_execution(
            tx, blocked_std.id, 0, "claude-opus-5-5", "anthropic", "cloud"
        )
    assert await spawn(db, blocked_std.id, mode="different_model") is None
    brow, _ = await state(db, blocked_std.id)
    assert brow["state"] == "in_review"
    assert brow["block_reason"] == indep.NO_INDEPENDENT_REVIEWER
    [decision] = await open_decisions(db, blocked_std.id)
    assert decision.options == ["retry_reviewer", "approve_without_review", "cancel"]
    assert decision.payload["reason"] == indep.NO_INDEPENDENT_REVIEWER

    # Re-enable codex and answer retry_reviewer -> resolves PR/head from parent and spawns!
    async with db.write() as tx:
        await accounts.update_account(
            tx, "codex", AccountUpdate(enabled=True), "admin:test"
        )
        await settings_svc.put_setting(
            tx,
            Path(),
            "global",
            "review.independent",
            {"mode": "different_provider", "max_rounds": 3},
            "admin:test",
        )
    await answer(db, decision.id, "retry_reviewer")
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT id, input FROM tasks WHERE parent_id = ? AND kind = 'code_review'",
            (blocked_std.id,),
        ) as cur,
    ):
        kids = await cur.fetchall()
    assert len(kids) == 1
    assert json.loads(kids[0]["input"])["head_sha"] == HEAD_1


# ---------------------------------------------------------------------------
# Criterion 4: Review-disabled (mode=off), non-code tasks, and redaction unchanged
# ---------------------------------------------------------------------------


def test_review_disabled_and_non_code_tasks_behave_unchanged(
    api_client: TestClient,
) -> None:
    forge = StatefulForge()
    checker = forge.install(api_client)
    _, w_headers = _register_worker(api_client)

    # 1. With review.independent mode="off" (default) and auto_merge=True,
    #    pr_merged still requires the PR to be merged at completion (unchanged).
    assert (
        api_client.put(
            "/api/settings/delivery.auto_merge", json={"value": True}
        ).status_code
        == 200
    )
    assert (
        api_client.put(
            "/api/settings/review.acceptance", json={"value": False}
        ).status_code
        == 200
    )
    t_off = api_client.post(
        "/api/tasks",
        json={
            "title": "Mode off PR",
            "project": "demo",
            "spec": "ship",
            "deliverable": "pr_merged",
            "review": "none",
            "required_capabilities": ["repository:write"],
        },
    ).json()["id"]
    claimed_off = _claim(api_client, w_headers)
    assert claimed_off["independent_review_required"] is False
    assert api_client.get(f"/api/tasks/{t_off}").json()["merge"]["may_merge"] is True

    forge.set_pr(PR_450_NUM, head_sha=HEAD_1, head_ref=f"argus/task-{t_off[:8]}")
    forge.set_ci(HEAD_1, "success")
    assert (
        api_client.post(
            f"/api/tasks/{t_off}/progress",
            json={
                "attempt": 1,
                "message": "opened",
                "kind": "pr_opened",
                "data": {"number": PR_450_NUM, "url": PR_450_URL},
            },
            headers=w_headers,
        ).status_code
        == 200
    )
    refused = api_client.post(
        f"/api/tasks/{t_off}/complete",
        json={"attempt": 1, "result": {"summary": "still open"}},
        headers=w_headers,
    )
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "deliverable_missing"

    forge.set_pr(
        PR_450_NUM, head_sha=HEAD_1, merged=True, head_ref=f"argus/task-{t_off[:8]}"
    )
    checker._cache.clear()
    ok_off = api_client.post(
        f"/api/tasks/{t_off}/complete",
        json={"attempt": 1, "result": {"summary": "merged"}},
        headers=w_headers,
    )
    assert ok_off.status_code == 200
    assert ok_off.json()["state"] == "succeeded"

    # 2. With review.independent ON, a non-code task skips review and succeeds.
    assert (
        api_client.put(
            "/api/settings/review.independent",
            json={"value": {"mode": "different_provider", "max_rounds": 3}},
        ).status_code
        == 200
    )
    t_resp = api_client.post(
        "/api/tasks",
        json={
            "title": "Answer question",
            "project": "demo",
            "spec": "explain",
            "deliverable": "response",
            "review": "none",
        },
    ).json()["id"]
    claimed_resp = _claim(api_client, w_headers)
    assert claimed_resp["id"] == t_resp
    done_resp = api_client.post(
        f"/api/tasks/{t_resp}/complete",
        json={"attempt": 1, "result": {"summary": "here is the answer"}},
        headers=w_headers,
    )
    assert done_resp.status_code == 200
    assert done_resp.json()["state"] == "succeeded"
    children = api_client.get(
        "/api/tasks", params={"project": "demo", "kind": "code_review"}
    ).json()["items"]
    assert not [c for c in children if c.get("parent_id") in (t_off, t_resp)]


async def test_failed_reviewer_error_redaction_is_preserved(
    db: Database, fleet
) -> None:
    await review_policy(db, [CODEX_ALT])
    async with db.write() as tx:
        await settings_svc.put_setting(
            tx,
            Path(),
            "global",
            "review.independent",
            {"mode": "different_provider", "max_rounds": 3, "max_reviewer_failures": 0},
            "admin:test",
        )
    parent = await reviewed_parent(db, fleet, ("claude-opus-5-5", "anthropic", "cloud"))
    child = await spawn(db, parent.id)
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET state = 'failed', error = ? WHERE id = ?",
            ("Authorization: Bearer sk-secret-token-value-123456 failed", child),
        )
        await indep.on_review_child_ended(
            tx,
            await tasks.task_row(tx.conn, child),
            TaskState.FAILED,
            "system",
        )
    [failed] = await parent_events(db, parent.id, indep.CHILD_FAILED_EVENT)
    assert "sk-secret-token-value-123456" not in (failed.get("error") or "")


# ---------------------------------------------------------------------------
# Criterion 5: Regression test for PR #450 / #451 scenario
# ---------------------------------------------------------------------------


def test_regression_450_451_replacement_pr_blocks_merge_and_accept(
    api_client: TestClient,
) -> None:
    """Parent delivers PR #450 at HEAD_1; independent review approves HEAD_1; PR is
    replaced by PR #451 at HEAD_Y. Approval for (#450, HEAD_1) must NEVER allow merge
    of (#451, HEAD_Y) and must NEVER be accepted as review of the merged head."""
    forge = StatefulForge()
    checker = forge.install(api_client)
    _configure_chains(api_client)
    assert (
        api_client.put(
            "/api/settings/review.independent",
            json={"value": {"mode": "different_provider", "max_rounds": 3}},
        ).status_code
        == 200
    )
    assert (
        api_client.put(
            "/api/settings/delivery.auto_merge", json={"value": True}
        ).status_code
        == 200
    )
    # Keep acceptance ON so an open review decision exists after approval of #450,
    # allowing us to verify that even answering `accept` rejects replacement PR #451.
    assert (
        api_client.put(
            "/api/settings/review.acceptance", json={"value": True}
        ).status_code
        == 200
    )

    _, w_headers = _register_worker(api_client)
    parent_id = api_client.post(
        "/api/tasks",
        json={
            "title": "PR 450/451 regression",
            "project": "demo",
            "spec": "deliver fix",
            "difficulty": "standard",
            "deliverable": "pr_merged",
            "review": "orchestrator",
            "required_capabilities": ["repository:write"],
        },
    ).json()["id"]

    # Implementer opens PR #450 at HEAD_1 with green CI and completes for review.
    impl = _claim(api_client, w_headers)
    assert impl["id"] == parent_id
    forge.set_pr(PR_450_NUM, head_sha=HEAD_1, head_ref=f"argus/task-{parent_id[:8]}")
    forge.set_ci(HEAD_1, "success")
    assert (
        api_client.post(
            f"/api/tasks/{parent_id}/progress",
            json={
                "attempt": 1,
                "message": "opened PR #450",
                "kind": "pr_opened",
                "data": {"number": PR_450_NUM, "url": PR_450_URL},
            },
            headers=w_headers,
        ).status_code
        == 200
    )
    _record_model(
        api_client,
        w_headers,
        impl,
        engine="claude",
        provider="anthropic",
        model="claude-opus-5-5",
    )
    assert (
        api_client.post(
            f"/api/tasks/{parent_id}/complete",
            json={"attempt": 1, "result": {"summary": "PR #450 ready"}},
            headers=w_headers,
        ).status_code
        == 200
    )

    # Independent reviewer approves PR #450 at HEAD_1.
    rev = _claim(api_client, w_headers)
    assert rev["kind"] == "code_review"
    assert rev["input"]["pr"] == PR_450_URL
    assert rev["input"]["head_sha"] == HEAD_1
    _record_model(
        api_client,
        w_headers,
        rev,
        engine="codex",
        provider="openai",
        model="gpt-5.5",
    )
    assert (
        api_client.post(
            f"/api/tasks/{rev['id']}/complete",
            json={
                "attempt": rev["attempt"],
                "result": {
                    "summary": "approved #450",
                    "data": {
                        "verdict": "approve",
                        "findings": [],
                        "head_sha": HEAD_1,
                    },
                },
            },
            headers=w_headers,
        ).status_code
        == 200
    )

    # Confirm merge gate allows (#450, HEAD_1), and rejects replacement PR #451
    # (whether at HEAD_Y OR even at the same SHA HEAD_1) and rejects moved head HEAD_Y.
    async def check_gates() -> tuple[tuple[bool, str | None], ...]:
        async with api_client.app.state.db.read() as conn:
            row = await tasks.task_row(conn, parent_id)
            return (
                await indep.merge_gate_status(
                    conn, row, pr=PR_450_URL, head_sha=HEAD_1
                ),
                await indep.merge_gate_status(
                    conn, row, pr=PR_451_URL, head_sha=HEAD_Y
                ),
                await indep.merge_gate_status(
                    conn, row, pr=PR_451_URL, head_sha=HEAD_1
                ),
                await indep.merge_gate_status(
                    conn, row, pr=PR_450_URL, head_sha=HEAD_Y
                ),
            )

    ok_450, bad_451_y, bad_451_x, bad_450_y = asyncio.run(check_gates())
    assert ok_450 == (True, None)
    assert bad_451_y == (False, "independent_review_pr_mismatch")
    assert bad_451_x == (False, "independent_review_pr_mismatch")
    assert bad_450_y == (False, "independent_review_head_mismatch")

    # Now replacement PR #451 at HEAD_Y is bound to the task and merged on the forge.
    forge.set_pr(
        PR_451_NUM,
        head_sha=HEAD_Y,
        merged=True,
        head_ref=f"argus/task-{parent_id[:8]}",
    )
    forge.set_ci(HEAD_Y, "success")
    checker._cache.clear()
    reconcile = api_client.post(
        f"/api/tasks/{parent_id}/reconcile-pr",
        json={
            "repository": REPO,
            "number": PR_451_NUM,
            "head_sha": HEAD_Y,
            "reason": "replacement PR #451",
        },
    )
    assert reconcile.status_code == 200, reconcile.text

    # Task read must now report merge.may_merge is False for PR #451 / HEAD_Y.
    detail = api_client.get(f"/api/tasks/{parent_id}").json()
    assert detail["merge"]["may_merge"] is False

    # Answering `accept` on the open review decision MUST be refused: approval for
    # PR #450 / HEAD_1 cannot accept merged PR #451 / HEAD_Y.
    open_decs = api_client.get(
        "/api/decisions", params={"subject_id": parent_id, "state": "open"}
    ).json()["items"]
    assert len(open_decs) == 1
    acc = api_client.post(
        f"/api/decisions/{open_decs[0]['id']}/answer",
        json={"choice": "accept", "text": "try accepting with #450 approval"},
    )
    assert acc.status_code == 409, acc.text
    assert acc.json()["detail"]["code"] == "independent_review_pr_mismatch"
    assert api_client.get(f"/api/tasks/{parent_id}").json()["state"] == "in_review"

    # Also verify the settles_alone / review_settle path voids #450 approval and
    # spawns a fresh reviewer for (#451, HEAD_Y) instead of accepting.
    async def settle_under_replacement() -> tuple[bool, str, str | None, str | None]:
        async with api_client.app.state.db.write() as tx:
            await settings_svc.put_setting(
                tx, Path(), "global", settings_svc.ACCEPTANCE_KEY, False, "admin:test"
            )
            await decisions.cancel_open(tx, "task", parent_id, None)
            await tx.conn.execute(
                "UPDATE tasks SET review = 'none', block_reason = ? WHERE id = ?",
                (indep.AWAITING_FORGE, parent_id),
            )
        checker._cache.clear()
        settled = await review_settle.settle_task(
            api_client.app.state.db, checker, parent_id
        )
        async with api_client.app.state.db.read() as conn:
            row = await tasks.task_row(conn, parent_id)
            async with conn.execute(
                "SELECT input FROM tasks WHERE parent_id = ? AND kind = 'code_review'"
                " ORDER BY rowid DESC LIMIT 1",
                (parent_id,),
            ) as cur:
                latest_inp = json.loads((await cur.fetchone())[0])
        return settled, row["state"], latest_inp.get("pr"), latest_inp.get("head_sha")

    settled, st, next_pr, next_sha = asyncio.run(settle_under_replacement())
    assert settled is False
    assert st == "in_review"
    assert next_pr == PR_451_URL
    assert next_sha == HEAD_Y
