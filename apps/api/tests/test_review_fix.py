"""Independent reviewers are verdict-only (pre-merge independent review spec 3.7).

A reviewer holds no write grant and no fix fence; a `fixed` verdict, or any verdict
for a head that moved during the review, is void and approves nothing.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from argus_api import main as _main  # noqa: F401 - loads packages in order
from argus_api.tasks import independent_review as indep
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
        executor = (claimed.get("execution") or {}).get("executor")
        if executor:
            # A paired reviewer's verdict counts only when its executor ran.
            res = client.post(
                f"/api/tasks/{claimed['id']}/models",
                json={
                    "attempt": claimed["attempt"],
                    "execution_generation": claimed["execution"]["generation"],
                    "executions": [
                        {
                            "key": f"{claimed['id']}:{claimed['attempt']}:delegate",
                            "layer": "delegate",
                            "state": "succeeded",
                            "executor": executor["selector"]["adapter"],
                            "provider": executor["selector"]["provider"],
                            "configured_model": executor["model"],
                            "observed_model": executor["model"],
                            "evidence": "cli_output",
                            "started_at": datetime.now(UTC).isoformat(),
                        }
                    ],
                },
                headers=headers,
            )
            assert res.status_code == 200, res.text
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


def _finding(priority: str = "P2", **extra: Any) -> dict[str, Any]:
    return {
        "id": "f1",
        "priority": priority,
        "file": "a.py",
        "title": "boundary check",
        "description": "the boundary is off by one",
        "evidence": {"kind": "reading", "detail": "a.py:10"},
        **extra,
    }


def _fixed(new: str = HEAD_2, old: str = HEAD_1) -> dict[str, Any]:
    return {
        "verdict": "fixed",
        "findings": [_finding()],
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


def test_reviewer_is_verdict_only_and_its_prompt_bounds_tests(
    api_client: TestClient,
) -> None:
    _forge, _checker, _parent_id, headers, _ = _setup(api_client)
    r1 = _claim(api_client, headers)
    assert r1["kind"] == "code_review"
    assert r1["required_capabilities"] == []
    assert "fix" not in r1["input"]
    # The reviewer holds no merge grant either.
    assert _may_merge(api_client, r1["id"]) is False
    spec = api_client.get(f"/api/tasks/{r1['id']}").json()["spec"]
    assert "verdict-only" in spec
    assert "no commit, no push" in spec
    assert "no PR create" in spec
    assert "timeout 300" in spec
    assert "never run a whole test suite" in spec
    assert "do not use `--raw`" in spec


def test_fixed_verdict_is_void_and_nothing_is_approved(
    api_client: TestClient,
) -> None:
    forge, checker, parent_id, headers, _ = _setup(api_client)
    r1 = _claim(api_client, headers)
    _push_fix(forge, checker, parent_id, HEAD_2)
    _finish(api_client, headers, r1, _fixed(), model=CODEX)
    assert _events(api_client, parent_id, indep.FIX_PUSHED_EVENT) == []
    assert [
        v["reason"] for v in _events(api_client, parent_id, "review.independent_void")
    ] == ["fix_not_permitted"]
    assert _approval(api_client, parent_id) == (None, None)
    assert _may_merge(api_client, parent_id) is False
    assert _children(api_client, parent_id) == [r1["id"]]  # no automatic respawn


def test_head_moved_by_the_reviewer_voids_its_approval(
    api_client: TestClient,
) -> None:
    forge, checker, parent_id, headers, _ = _setup(api_client)
    r1 = _claim(api_client, headers)
    # The reviewer pushed to the PR branch anyway and still reports the head it was given.
    _push_fix(forge, checker, parent_id, HEAD_2)
    _finish(
        api_client,
        headers,
        r1,
        {"verdict": "approve", "findings": [], "head_sha": HEAD_1},
        model=CODEX,
    )
    assert _approval(api_client, parent_id) == (None, None)
    assert _may_merge(api_client, parent_id) is False
    assert [
        v["reason"] for v in _events(api_client, parent_id, "review.independent_void")
    ] == ["head_mismatch"]


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
            "findings": [_finding("P1", title="redesign")],
            "head_sha": HEAD_1,
        },
        model=CODEX,
    )
    parent = api_client.get(f"/api/tasks/{parent_id}").json()
    assert parent["state"] == "ready"
    assert "redesign" in parent["spec"]
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
