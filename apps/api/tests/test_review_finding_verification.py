"""Structured findings, disputes and the read-only verifier (review finding
verification spec): the schema, `observe` and `enforce`, the all-refuted decision,
and the verifier's model."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi.testclient import TestClient
from test_pre_merge_independent_review import (
    HEAD_1,
    HEAD_2,
    PR_450_NUM,
    _claim,
    _record_model,
    api_client,
    fleet,
)
from test_review_fix import (
    CODEX,
    DEEPSEEK,
    OPUS,
    _approval,
    _children,
    _events,
    _finding,
    _finish,
    _push_fix,
    _put,
    _setup,
)

__all__ = ["api_client", "fleet"]


def _verification_children(client: TestClient, parent_id: str) -> list[dict[str, Any]]:
    async def read() -> list[dict[str, Any]]:
        async with (
            client.app.state.db.read() as conn,
            conn.execute(
                "SELECT id, state, input, required_capabilities FROM tasks"
                " WHERE parent_id = ? AND kind = 'review_verification' ORDER BY rowid",
                (parent_id,),
            ) as cur,
        ):
            return [dict(r) for r in await cur.fetchall()]

    return asyncio.run(read())


def _review_request(client: TestClient, headers: dict[str, str], findings: list) -> str:
    """The first reviewer (gpt-5.5) asks for changes; returns the parent's state."""
    r1 = _claim(client, headers)
    assert r1["kind"] == "code_review"
    _finish(
        client,
        headers,
        r1,
        {"verdict": "request_changes", "findings": findings, "head_sha": HEAD_1},
        model=CODEX,
    )
    return r1["id"]


def _verify(
    client: TestClient, headers: dict[str, str], verdicts: dict[str, str]
) -> dict[str, Any]:
    v = _claim(client, headers)
    assert v["kind"] == "review_verification"
    assert v["required_capabilities"] == []
    _finish(
        client,
        headers,
        v,
        {
            "verifications": [
                {"id": i, "status": s, "evidence": f"ran it: {s}"}
                for i, s in verdicts.items()
            ]
        },
        model=DEEPSEEK,
    )
    return v


def _state(client: TestClient, task_id: str) -> dict[str, Any]:
    return client.get(f"/api/tasks/{task_id}").json()


def _enable(client: TestClient, mode: str) -> None:
    _put(
        client,
        "review.independent",
        {"mode": "different_model", "max_rounds": 3, "verify_findings": mode},
    )


def test_findings_schema_is_enforced_with_a_path(api_client: TestClient) -> None:
    _forge, _checker, parent_id, headers, _ = _setup(api_client)
    r1 = _claim(api_client, headers)
    bad = [
        ["P2: boundary check"],
        [{**_finding(), "title": ""}],
        [{**_finding(), "title": "one\nline"}],
        [{**_finding(), "evidence": {"kind": "guess", "detail": "x"}}],
        [{**_finding(), "repeat_of": "no-colon"}],
        [{k: v for k, v in _finding().items() if k != "evidence"}],
        [_finding(), _finding()],  # duplicate id
    ]
    for findings in bad:
        res = api_client.post(
            f"/api/tasks/{r1['id']}/complete",
            json={
                "attempt": r1["attempt"],
                "result": {
                    "summary": "s",
                    "data": {
                        "verdict": "request_changes",
                        "findings": findings,
                        "head_sha": HEAD_1,
                    },
                },
            },
            headers=headers,
        )
        assert res.status_code == 422, (findings, res.text)
    assert _state(api_client, parent_id)["state"] == "in_review"


def test_observe_sends_findings_at_once_and_records_the_verifier(
    api_client: TestClient,
) -> None:
    _, _, parent_id, headers, _ = _setup(api_client)
    _enable(api_client, "observe")
    review_id = _review_request(
        api_client,
        headers,
        [_finding(id="f1"), _finding(id="f2", title="second")],
    )
    parent = _state(api_client, parent_id)
    assert parent["state"] == "ready"  # the verdict was not held
    assert "boundary check" in parent["spec"]
    [req] = _events(api_client, parent_id, "review.finding_verification_requested")
    assert (req["review_child_id"], req["mode"]) == (review_id, "observe")
    [verification] = _verification_children(api_client, parent_id)
    assert json.loads(verification["required_capabilities"]) == []


def test_enforce_holds_the_verdict_and_sends_only_unrefuted_findings(
    api_client: TestClient,
) -> None:
    _, _, parent_id, headers, _ = _setup(api_client)
    _enable(api_client, "enforce")
    _review_request(
        api_client,
        headers,
        [
            _finding(id="f1", title="real bug"),
            _finding(id="f2", title="bogus claim"),
            _finding(id="f3", title="maybe"),
        ],
    )
    assert _state(api_client, parent_id)["state"] == "in_review"  # held
    v = _verify(
        api_client,
        headers,
        {"f1": "confirmed", "f2": "refuted", "f3": "unclear"},
    )
    # The verifier used a model that is neither the author's nor the reviewer's.
    executions = api_client.get(f"/api/tasks/{v['id']}").json()
    assert "gpt-5.5" not in json.dumps(executions.get("execution", {}))
    parent = _state(api_client, parent_id)
    assert parent["state"] == "ready"
    spec = parent["spec"]
    assert "real bug" in spec and "maybe" in spec
    assert "refuted" in spec.lower()
    statuses = {
        e["id"]: e["status"]
        for e in _events(api_client, parent_id, "review.finding_verified")
    }
    assert statuses == {"f1": "confirmed", "f2": "refuted", "f3": "unclear"}


def test_all_refuted_opens_one_decision_and_never_approves(
    api_client: TestClient,
) -> None:
    _, _, parent_id, headers, _ = _setup(api_client)
    _enable(api_client, "enforce")
    review_id = _review_request(api_client, headers, [_finding(id="f1")])
    _verify(api_client, headers, {"f1": "refuted"})
    [event] = _events(api_client, parent_id, "review.independent_findings_refuted")
    assert event["child_id"] == review_id
    decisions = api_client.get(
        "/api/decisions", params={"subject_id": parent_id, "state": "open"}
    ).json()["items"]
    assert len(decisions) == 1
    assert decisions[0]["kind"] == "review"
    assert decisions[0]["options"] == ["accept", "accept_with_deviation", "reject"]
    parent = _state(api_client, parent_id)
    assert parent["state"] == "in_review"
    assert parent["merge"]["may_merge"] is False
    # Plain accept needs the approval a verifier cannot give.
    res = api_client.post(
        f"/api/decisions/{decisions[0]['id']}/answer",
        json={"choice": "reject", "text": "the finding stands"},
    )
    assert res.status_code == 200, res.text
    assert _state(api_client, parent_id)["state"] == "ready"
    assert "boundary check" in _state(api_client, parent_id)["spec"]


def test_ended_verifier_leaves_the_verdict_unchanged(api_client: TestClient) -> None:
    _, _, parent_id, headers, _ = _setup(api_client)
    _enable(api_client, "enforce")
    _review_request(api_client, headers, [_finding(id="f1")])
    [v] = _verification_children(api_client, parent_id)
    res = api_client.post(f"/api/tasks/{v['id']}/cancel")
    assert res.status_code == 200, res.text
    assert _state(api_client, parent_id)["state"] == "ready"
    [skipped] = _events(api_client, parent_id, "review.finding_verification_skipped")
    assert skipped["reason"] == "verifier_failed"


def test_off_creates_no_verifier(api_client: TestClient) -> None:
    _, _, parent_id, headers, _ = _setup(api_client)
    _review_request(api_client, headers, [_finding()])
    assert _verification_children(api_client, parent_id) == []
    assert _state(api_client, parent_id)["state"] == "ready"
    assert len(_children(api_client, parent_id)) == 1


def test_responses_and_repeats_reach_the_next_reviewer_and_the_report(
    api_client: TestClient,
) -> None:
    forge, checker, parent_id, headers, _ = _setup(api_client)
    first = _review_request(
        api_client, headers, [_finding(id="f1"), _finding(id="f2", title="other")]
    )
    impl = _claim(api_client, headers)
    assert impl["id"] == parent_id
    _record_model(
        api_client, headers, impl, engine="claude", provider="anthropic", model=OPUS[2]
    )
    _push_fix(forge, checker, parent_id, HEAD_2)
    done = api_client.post(
        f"/api/tasks/{parent_id}/complete",
        json={
            "attempt": impl["attempt"],
            "result": {
                "summary": "fixed",
                "data": {
                    "review_responses": [
                        {"id": "f1", "status": "disputed", "evidence": "pytest -k b"}
                    ]
                },
            },
        },
        headers=headers,
    )
    assert done.status_code == 200, done.text
    [responded] = _events(api_client, parent_id, "review.findings_responded")
    assert responded["responses"]["f1"]["status"] == "disputed"
    assert responded["responses"]["f2"]["status"] == "unanswered"
    second = _claim(api_client, headers)
    assert second["kind"] == "code_review"
    assert f"{first}:f1" in second["spec"] and "pytest -k b" in second["spec"]
    # The reviewer raises the disputed finding again.
    _finish(
        api_client,
        headers,
        second,
        {
            "verdict": "request_changes",
            "head_sha": HEAD_2,
            "findings": [_finding(id="g1", repeat_of=f"{first}:f1")],
        },
        model=CODEX,
    )
    rounds = api_client.get(f"/api/tasks/{parent_id}/reviews").json()
    assert rounds["repeats"] == 1
    assert rounds["rounds"][0]["findings"][0]["response"] == "disputed"
    report = api_client.get("/api/projects/demo/review-report").json()["findings"]
    assert report["responses"] == {"disputed": 1, "unanswered": 1, "pending": 1}
    assert report["disputes"] == {
        "disputed": 1,
        "dropped": 0,
        "raised_again": 1,
        "no_next_round": 0,
    }
    assert report["repeats"] == 1
    assert api_client.get("/api/projects/nope/review-report").status_code == 404
    assert api_client.get("/api/projects/demo/review-report?days=91").status_code == 422


def test_all_refuted_accept_with_deviation_waives_without_approval(
    api_client: TestClient,
) -> None:
    forge, checker, parent_id, headers, extra = _setup(api_client)
    _enable(api_client, "enforce")
    _review_request(api_client, headers, [_finding(id="f1")])
    _verify(api_client, headers, {"f1": "refuted"})
    forge.set_pr(PR_450_NUM, head_sha=HEAD_1, head_ref=extra["branch"], merged=True)
    checker._cache.clear()
    [decision] = api_client.get(
        "/api/decisions", params={"subject_id": parent_id, "state": "open"}
    ).json()["items"]
    res = api_client.post(
        f"/api/decisions/{decision['id']}/answer",
        json={"choice": "accept_with_deviation", "text": "verifier is right"},
    )
    assert res.status_code == 200, res.text
    [waived] = _events(api_client, parent_id, "review.independent_waived")
    assert waived["reason"] == "findings_refuted"
    assert _approval(api_client, parent_id) == (None, None)


def test_verification_children_are_hidden_from_the_board(
    api_client: TestClient,
) -> None:
    _, _, parent_id, headers, _ = _setup(api_client)
    _enable(api_client, "observe")
    _review_request(api_client, headers, [_finding()])
    [v] = _verification_children(api_client, parent_id)
    everything = {t["id"] for t in api_client.get("/api/tasks").json()["items"]}
    board = {
        t["id"]
        for t in api_client.get(
            "/api/tasks", params={"hide_review_children": "true"}
        ).json()["items"]
    }
    assert v["id"] in everything and v["id"] not in board
    assert parent_id in board
