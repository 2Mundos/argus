"""Reviewer fixes its findings on the PR branch (pre-merge independent review spec 3.7).

A reviewer that pushes a correction never approves it: the old approval is void, a
fresh review of the exact new head runs in another session on another model, and
merge stays blocked until that approval exists.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from argus_api import main as _main  # noqa: F401 - loads packages in order
from argus_api.tasks import independent_review as indep
from argus_api.tasks import review_settle
from fastapi.testclient import TestClient
from test_execution_routes import (
    CODEX_ALT,
    DEEPSEEK_ALT,
    OPUS_ALT,
    record_execution,
)
from test_pre_merge_independent_review import (
    HEAD_1,
    HEAD_2,
    HEAD_Y,
    PR_450_NUM,
    PR_450_URL,
    StatefulForge,
    _claim,
    _record_model,
    _register_worker,
    api_client,
    fleet,
)

__all__ = ["api_client", "fleet"]

CHAIN = [OPUS_ALT, CODEX_ALT, DEEPSEEK_ALT]


def _put(client: TestClient, key: str, value: Any) -> None:
    res = client.put(f"/api/settings/{key}", json={"value": value})
    assert res.status_code == 200, res.text


def _setup(
    client: TestClient,
    *,
    mode: str = "different_model",
    max_rounds: int = 3,
    review_chain: list[dict[str, Any]] | None = None,
    review_chains: dict[str, Any] | None = None,
    exec_chains: dict[str, Any] | None = None,
    impl: tuple[str, str, str] = ("claude", "anthropic", "claude-opus-5-5"),
) -> tuple[StatefulForge, Any, str, dict[str, str], dict[str, Any]]:
    forge = StatefulForge()
    checker = forge.install(client)
    # The reviewer walks the same `execution.chains`: the reviewer alternatives follow
    # the author's in their difficulty's chain.
    difficulties = {
        d: list(chain) for d, chain in (exec_chains or {"standard": [OPUS_ALT]}).items()
    }
    for d, chain in (review_chains or {"standard": review_chain or CHAIN}).items():
        have = {a["id"] for a in difficulties.get(d, [])}
        difficulties[d] = difficulties.get(d, []) + [
            a for a in chain if a["id"] not in have
        ]
    _put(client, "execution.chains", {"version": 1, "difficulties": difficulties})
    _put(client, "review.independent", {"mode": mode, "max_rounds": max_rounds})
    _put(client, "delivery.auto_merge", True)
    _put(client, "review.acceptance", False)
    _, headers = _register_worker(client)
    parent_id = client.post(
        "/api/tasks",
        json={
            "title": "Ship feature",
            "project": "demo",
            "spec": "implement feature",
            "difficulty": "standard",
            "deliverable": "pr_merged",
            "review": "none",
            "required_capabilities": ["repository:write"],
        },
    ).json()["id"]
    claimed_impl = _claim(client, headers)
    branch = f"argus/task-{parent_id[:8]}"
    forge.set_pr(PR_450_NUM, head_sha=HEAD_1, head_ref=branch)
    forge.set_ci(HEAD_1, "success")
    assert (
        client.post(
            f"/api/tasks/{parent_id}/progress",
            json={
                "attempt": 1,
                "message": "opened PR",
                "kind": "pr_opened",
                "data": {"number": PR_450_NUM, "url": PR_450_URL},
            },
            headers=headers,
        ).status_code
        == 200
    )
    engine, provider, model = impl
    _record_model(
        client, headers, claimed_impl, engine=engine, provider=provider, model=model
    )
    checker._cache.clear()
    done = client.post(
        f"/api/tasks/{parent_id}/complete",
        json={"attempt": 1, "result": {"summary": "ready"}},
        headers=headers,
    )
    assert done.status_code == 200, done.text
    return forge, checker, parent_id, headers, {"branch": branch}


def _events(client: TestClient, parent_id: str, kind: str) -> list[dict[str, Any]]:
    async def read() -> list[dict[str, Any]]:
        async with (
            client.app.state.db.read() as conn,
            conn.execute(
                "SELECT data FROM events WHERE subject_id = ? AND kind = ?"
                " ORDER BY rowid",
                (parent_id, kind),
            ) as cur,
        ):
            return [json.loads(r[0]) for r in await cur.fetchall()]

    return asyncio.run(read())


def _children(client: TestClient, parent_id: str) -> list[str]:
    async def read() -> list[str]:
        async with (
            client.app.state.db.read() as conn,
            conn.execute(
                "SELECT id FROM tasks WHERE parent_id = ? AND kind = 'code_review'"
                " ORDER BY rowid",
                (parent_id,),
            ) as cur,
        ):
            return [r[0] for r in await cur.fetchall()]

    return asyncio.run(read())


def _approval(client: TestClient, parent_id: str) -> tuple[str | None, str | None]:
    async def read() -> tuple[str | None, str | None]:
        async with client.app.state.db.read() as conn:
            return await indep.reviewed_approval_of(conn, parent_id)

    return asyncio.run(read())


def _may_merge(client: TestClient, parent_id: str) -> bool:
    return client.get(f"/api/tasks/{parent_id}").json()["merge"]["may_merge"]


def _finish(
    client: TestClient,
    headers: dict[str, str],
    claimed: dict[str, Any],
    data: dict[str, Any],
    *,
    model: tuple[str, str, str] | None,
) -> None:
    if model is not None:
        engine, provider, name = model
        _record_model(
            client, headers, claimed, engine=engine, provider=provider, model=name
        )
    res = client.post(
        f"/api/tasks/{claimed['id']}/complete",
        json={
            "attempt": claimed["attempt"],
            "result": {"summary": "reviewed", "data": data},
        },
        headers=headers,
    )
    assert res.status_code == 200, res.text


CODEX = ("codex", "openai", "gpt-5.5")
DEEPSEEK = (
    "claude",
    "local",
    "served-model",
)  # harness of the routed deepseek alternative
OPUS = ("claude", "anthropic", "claude-opus-5-5")
CODEX_IMPL = ("codex", "openai", "gpt-5.5")


def _fixed(new: str = HEAD_2, old: str = HEAD_1) -> dict[str, Any]:
    return {
        "verdict": "fixed",
        "findings": ["P2: boundary check"],
        "head_sha": old,
        "fixed_head_sha": new,
        "tests": ["pytest -q: 12 passed"],
    }


def _insert_execution(client: TestClient, task_id: str, key: str, model: str) -> None:
    async def write() -> None:
        async with client.app.state.db.write() as tx:
            await record_execution(tx, task_id, key, model, "anthropic", None)

    asyncio.run(write())


def _push_fix(forge: StatefulForge, checker: Any, parent_id: str, head: str) -> None:
    forge.set_pr(PR_450_NUM, head_sha=head, head_ref=f"argus/task-{parent_id[:8]}")
    forge.set_ci(head, "success")
    checker._cache.clear()


def test_fix_then_fresh_different_model_approval_then_authorized_merge(
    api_client: TestClient,
) -> None:
    forge, checker, parent_id, headers, _ = _setup(api_client)
    r1 = _claim(api_client, headers)
    assert r1["kind"] == "code_review"
    assert r1["execution"]["harness"]["model"] == "gpt-5.5"
    assert r1["required_capabilities"] == ["repository:write"]
    assert r1["input"]["fix"] == {
        "pr": PR_450_URL,
        "base_head_sha": HEAD_1,
        "force_push": False,
    }
    # The reviewer holds no merge grant: it cannot merge its own or any PR.
    assert _may_merge(api_client, r1["id"]) is False

    _push_fix(forge, checker, parent_id, HEAD_2)
    _finish(api_client, headers, r1, _fixed(), model=CODEX)

    [fix] = _events(api_client, parent_id, indep.FIX_PUSHED_EVENT)
    assert fix["child_id"] == r1["id"]
    assert fix["pr"] == PR_450_URL
    assert (fix["old_head_sha"], fix["new_head_sha"]) == (HEAD_1, HEAD_2)
    assert fix["fix_models"] == ["gpt-5.5"] and fix["fix_providers"] == ["openai"]
    assert fix["findings"] == ["P2: boundary check"]
    # Nothing is approved and nothing merges until another model reviews HEAD_2.
    assert _approval(api_client, parent_id) == (None, None)
    assert _may_merge(api_client, parent_id) is False
    parent = api_client.get(f"/api/tasks/{parent_id}").json()
    assert parent["state"] == "in_review"

    r2_id = _children(api_client, parent_id)[-1]
    assert r2_id != r1["id"]
    r2 = _claim(api_client, headers)
    assert r2["id"] == r2_id
    assert r2["input"]["head_sha"] == HEAD_2
    assert r2["execution"]["executor"]["model"] == "deepseek/deepseek-v4.1-flash"
    assert r2["engine_lease"]["lease_id"] != r1["engine_lease"]["lease_id"]
    assert _may_merge(api_client, parent_id) is False

    _finish(
        api_client,
        headers,
        r2,
        {"verdict": "approve", "findings": [], "head_sha": HEAD_2},
        model=DEEPSEEK,
    )
    assert _approval(api_client, parent_id) == (PR_450_URL, HEAD_2)
    awaiting = api_client.get(f"/api/tasks/{parent_id}").json()
    assert awaiting["block_reason"] == "awaiting_forge"
    assert awaiting["merge"]["may_merge"] is True

    forge.set_pr(
        PR_450_NUM,
        head_sha=HEAD_2,
        merged=True,
        head_ref=f"argus/task-{parent_id[:8]}",
    )
    checker._cache.clear()
    assert asyncio.run(
        review_settle.settle_task(api_client.app.state.db, checker, parent_id)
    )
    assert api_client.get(f"/api/tasks/{parent_id}").json()["state"] == "succeeded"


def test_fixer_model_and_session_never_approve_and_old_approval_is_void(
    api_client: TestClient,
) -> None:
    forge, checker, parent_id, headers, _ = _setup(api_client)
    r1 = _claim(api_client, headers)
    _push_fix(forge, checker, parent_id, HEAD_2)
    _finish(api_client, headers, r1, _fixed(), model=CODEX)
    r2 = _claim(api_client, headers)
    # The next reviewer's session turns out to have run the fixer's model: its
    # approval is void and never recorded; another reviewer is scheduled.
    _insert_execution(api_client, r2["id"], "x", "gpt-5.5")
    _finish(
        api_client,
        headers,
        r2,
        {"verdict": "approve", "findings": [], "head_sha": HEAD_2},
        model=None,
    )
    assert _approval(api_client, parent_id) == (None, None)
    assert _may_merge(api_client, parent_id) is False
    voids = _events(api_client, parent_id, "review.independent_void")
    assert [v["reason"] for v in voids] == ["same_model"]

    async def clean(child: str) -> bool:
        async with api_client.app.state.db.read() as conn:
            row = await conn.execute_fetchall(
                "SELECT 1"  # parent id is the child's parent
            )
            assert row
            return await indep.child_models_clean(
                conn,
                child,
                mode="different_model",
                author_models={"claude-opus-5-5"},
                author_providers={"anthropic"},
                parent_id=parent_id,
            )

    # The fixing session itself is never clean, whatever model it reports.
    assert asyncio.run(clean(r1["id"])) is False


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("two_models", "fix_provenance_unclear"),
        ("same_head", "fix_head_invalid"),
        ("stale_head", "fix_head_mismatch"),
        ("moved_head", "fix_head_mismatch"),
    ],
)
def test_unverifiable_fix_is_refused_and_nothing_is_approved(
    api_client: TestClient, case: str, reason: str
) -> None:
    forge, checker, parent_id, headers, _ = _setup(api_client)
    r1 = _claim(api_client, headers)
    data = _fixed()
    model: tuple[str, str, str] | None = CODEX
    if case == "two_models":
        _insert_execution(api_client, r1["id"], "y", "claude-sonnet-5-5")
        _push_fix(forge, checker, parent_id, HEAD_2)
    elif case == "same_head":
        data = _fixed(new=HEAD_1)
    elif case == "stale_head":
        _push_fix(forge, checker, parent_id, HEAD_2)
        data = _fixed(new=HEAD_Y)
    else:  # moved_head: the branch moved again after the fix
        _push_fix(forge, checker, parent_id, HEAD_Y)
    _finish(api_client, headers, r1, data, model=model)
    assert _events(api_client, parent_id, indep.FIX_PUSHED_EVENT) == []
    voids = _events(api_client, parent_id, "review.independent_void")
    assert [v["reason"] for v in voids] == [reason]
    assert _approval(api_client, parent_id) == (None, None)
    assert _may_merge(api_client, parent_id) is False
    assert _children(api_client, parent_id) == [r1["id"]]  # no loop, no respawn


def test_fix_without_recorded_model_is_void_and_never_trusted(
    api_client: TestClient,
) -> None:
    forge, checker, parent_id, headers, _ = _setup(api_client)
    r1 = _claim(api_client, headers)
    _push_fix(forge, checker, parent_id, HEAD_2)
    _finish(api_client, headers, r1, _fixed(), model=None)
    assert _events(api_client, parent_id, indep.FIX_PUSHED_EVENT) == []
    assert [
        v["reason"] for v in _events(api_client, parent_id, "review.independent_void")
    ] == ["same_model"]
    assert _approval(api_client, parent_id) == (None, None)
    assert _may_merge(api_client, parent_id) is False


def test_short_fixed_head_is_rejected_by_the_result_schema(
    api_client: TestClient,
) -> None:
    _forge, _checker, _parent_id, headers, _ = _setup(api_client)
    r1 = _claim(api_client, headers)
    res = api_client.post(
        f"/api/tasks/{r1['id']}/complete",
        json={
            "attempt": r1["attempt"],
            "result": {"summary": "x", "data": _fixed(new=HEAD_2[:10])},
        },
        headers=headers,
    )
    assert res.status_code == 422


def test_head_moving_after_the_fix_voids_the_fresh_approval(
    api_client: TestClient,
) -> None:
    forge, checker, parent_id, headers, _ = _setup(api_client)
    r1 = _claim(api_client, headers)
    _push_fix(forge, checker, parent_id, HEAD_2)
    _finish(api_client, headers, r1, _fixed(), model=CODEX)
    r2 = _claim(api_client, headers)
    _push_fix(forge, checker, parent_id, HEAD_Y)
    _finish(
        api_client,
        headers,
        r2,
        {"verdict": "approve", "findings": [], "head_sha": HEAD_2},
        model=DEEPSEEK,
    )
    assert _approval(api_client, parent_id) == (None, None)
    assert _may_merge(api_client, parent_id) is False
    assert [
        v["reason"] for v in _events(api_client, parent_id, "review.independent_void")
    ] == ["head_mismatch"]


def test_no_independent_model_left_after_a_fix_blocks_without_bypass(
    api_client: TestClient,
) -> None:
    forge, checker, parent_id, headers, _ = _setup(
        api_client, review_chain=[OPUS_ALT, CODEX_ALT]
    )
    r1 = _claim(api_client, headers)
    _push_fix(forge, checker, parent_id, HEAD_2)
    _finish(api_client, headers, r1, _fixed(), model=CODEX)
    parent = api_client.get(f"/api/tasks/{parent_id}").json()
    assert parent["state"] == "in_review"
    assert parent["block_reason"] == "no_independent_reviewer"
    assert _approval(api_client, parent_id) == (None, None)
    assert _may_merge(api_client, parent_id) is False
    assert _children(api_client, parent_id) == [r1["id"]]


def test_post_fix_review_falls_back_across_difficulty_chains(
    api_client: TestClient,
) -> None:
    # No review chain for the parent's difficulty: other difficulties are searched,
    # and the fixer's model is skipped like an author's.
    forge, checker, parent_id, headers, _ = _setup(
        api_client,
        review_chains={"hard": [OPUS_ALT, DEEPSEEK_ALT]},
        exec_chains={"standard": [CODEX_ALT]},
        impl=CODEX_IMPL,
    )
    r1 = _claim(api_client, headers)
    assert r1["execution"]["harness"]["model"] == "claude-opus-5-5"
    _push_fix(forge, checker, parent_id, HEAD_2)
    _finish(api_client, headers, r1, _fixed(), model=OPUS)
    r2 = _claim(api_client, headers)
    assert r2["input"]["head_sha"] == HEAD_2
    assert r2["execution"]["executor"]["model"] == "deepseek/deepseek-v4.1-flash"


def test_fix_rounds_are_bounded(api_client: TestClient) -> None:
    forge, checker, parent_id, headers, _ = _setup(api_client, max_rounds=1)
    r1 = _claim(api_client, headers)
    _push_fix(forge, checker, parent_id, HEAD_2)
    _finish(api_client, headers, r1, _fixed(), model=CODEX)
    assert len(_events(api_client, parent_id, indep.FIX_PUSHED_EVENT)) == 1
    assert len(_events(api_client, parent_id, "review.independent_escalated")) == 1
    assert _children(api_client, parent_id) == [r1["id"]]
    assert _approval(api_client, parent_id) == (None, None)
    assert _may_merge(api_client, parent_id) is False


def test_replayed_fix_completion_is_idempotent(api_client: TestClient) -> None:
    from argus_api.tasks import service as tasks_svc
    from argus_core.settings import IndependentReviewSetting
    from argus_core.tasks import TaskResult

    forge, checker, parent_id, headers, _ = _setup(api_client)
    r1 = _claim(api_client, headers)
    _push_fix(forge, checker, parent_id, HEAD_2)
    _finish(api_client, headers, r1, _fixed(), model=CODEX)
    children = _children(api_client, parent_id)

    async def replay() -> None:
        db = api_client.app.state.db
        async with db.write() as tx:
            child = await tasks_svc._row(tx.conn, r1["id"])
            await indep.on_review_child_complete(
                tx,
                child,
                TaskResult(summary="again", data=_fixed()),
                setting=IndependentReviewSetting(mode="different_model"),
                policy=indep.model_policy.Policy(),
                actor="test",
            )

    asyncio.run(replay())
    assert len(_events(api_client, parent_id, indep.FIX_PUSHED_EVENT)) == 1
    assert _children(api_client, parent_id) == children


def test_no_finding_approve_and_cannot_fix_request_changes_are_unchanged(
    api_client: TestClient,
) -> None:
    _forge, _checker, parent_id, headers, _ = _setup(api_client)
    r1 = _claim(api_client, headers)
    _finish(
        api_client,
        headers,
        r1,
        {"verdict": "approve", "findings": [], "head_sha": HEAD_1},
        model=CODEX,
    )
    assert _approval(api_client, parent_id) == (PR_450_URL, HEAD_1)
    assert _events(api_client, parent_id, indep.FIX_PUSHED_EVENT) == []
    assert _may_merge(api_client, parent_id) is True


def test_cannot_fix_request_changes_hands_back_to_the_implementer(
    api_client: TestClient,
) -> None:
    _forge, _checker, parent_id, headers, _ = _setup(api_client)
    r1 = _claim(api_client, headers)
    _finish(
        api_client,
        headers,
        r1,
        {
            "verdict": "request_changes",
            "findings": ["P1: redesign"],
            "head_sha": HEAD_1,
        },
        model=CODEX,
    )
    parent = api_client.get(f"/api/tasks/{parent_id}").json()
    assert parent["state"] == "ready"
    assert "P1: redesign" in parent["spec"]
    assert _events(api_client, parent_id, indep.FIX_PUSHED_EVENT) == []


def test_reviewer_input_cannot_grant_force_push(api_client: TestClient) -> None:
    res = api_client.post(
        "/api/tasks",
        json={
            "title": "bad review",
            "project": "demo",
            "spec": "x",
            "kind": "code_review",
            "input": {
                "task_id": "t",
                "head_sha": HEAD_1,
                "fix": {"force_push": True},
            },
        },
    )
    assert res.status_code == 422, res.text
