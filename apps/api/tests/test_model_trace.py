"""Model observability: REST, MCP and the account-switch hook (model observability spec)."""

import json
import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from api_support import insert_agent
from argus_api import mcp_server
from argus_api.accounts import usage
from argus_api.db import Database
from argus_api.main import app
from argus_api.modeltrace import service as modeltrace
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import service as tasks
from argus_core.model_trace import ExecutionReport, SelectionReport
from argus_core.tasks import TaskCreate
from fastapi.testclient import TestClient
from test_account_failover import QUOTA, add, fail
from test_mcp import tool as mcp_tool

ADMIN_TOKEN = "test-admin-token-" + "t" * 32
REV = "ab" * 32


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        yield c


def register(client: TestClient) -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
    res = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    return {"Authorization": f"Bearer {res.json()['token']}"}


@pytest.fixture
def agent(client: TestClient) -> dict[str, str]:
    return register(client)


def claimed(client: TestClient, agent: dict[str, str], **kw) -> tuple[str, int]:
    client.post("/api/tasks", json={"project": "demo", "title": "t", **kw})
    task = client.post("/api/tasks/claim", json={}, headers=agent).json()
    return task["id"], task["attempt"]


def harness(key: str = "h1", **kw) -> dict:
    return {
        "key": key,
        "layer": "harness",
        "state": "running",
        "runtime": "nanoclaw",
        "engine": "codex",
        "provider": "openai",
        "requested_model": "gpt-6-luna",
        "configured_model": "gpt-6-luna",
        "policy_revision": REV,
        **kw,
    }


def delegate(key: str = "d1", **kw) -> dict:
    return {
        "key": key,
        "layer": "delegate",
        "state": "running",
        "runtime": "argus-delegate",
        "executor": "claude",
        "provider": "anthropic",
        "requested_model": "claude-sonnet-5-5",
        "configured_model": "claude-sonnet-5-5",
        "policy_revision": REV,
        "delegation_id": "run-1",
        **kw,
    }


def post(client, agent, task_id, attempt, executions=(), selections=()):
    return client.post(
        f"/api/tasks/{task_id}/models",
        json={
            "attempt": attempt,
            "executions": list(executions),
            "selections": list(selections),
        },
        headers=agent,
    )


def test_both_layers_flow_and_card_summary(client, agent) -> None:
    task_id, attempt = claimed(client, agent)
    res = post(
        client,
        agent,
        task_id,
        attempt,
        [
            harness(observed_model="gpt-6-luna", evidence="reported_by_runtime"),
            delegate(),
        ],
        [
            {
                "key": "s1",
                "layer": "harness",
                "kind": "initial",
                "reason_code": "policy",
                "explanation": "first model of the codex chain",
                "to_model": "gpt-6-luna",
                "alternatives": [{"model": "gpt-6-sol", "reason_code": "priority"}],
                "execution_key": "h1",
                "policy_revision": REV,
            }
        ],
    )
    assert res.status_code == 200, res.text
    trace = client.get(f"/api/tasks/{task_id}/models").json()
    h, d = trace["executions"]
    assert (h["layer"], h["observed_model"], h["evidence"]) == (
        "harness",
        "gpt-6-luna",
        "reported_by_runtime",
    )
    # Configuration is not proof: the delegate asked for a model but none was observed.
    assert (d["layer"], d["requested_model"]) == ("delegate", "claude-sonnet-5-5")
    assert (d["observed_model"], d["evidence"]) == (None, "not_reported")
    assert d["policy_revision"] == REV and d["started_at"] is not None
    assert d["usage"] is None  # no usage row: unknown, not zero
    (alt,) = trace["timeline"][0]["alternatives"]
    assert (alt["model"], alt["reason_code"]) == ("gpt-6-sol", "priority")
    models = client.get(f"/api/tasks/{task_id}").json()["models"]
    assert models["harness"]["observed_model"] == "gpt-6-luna"
    assert models["delegate"]["observed_model"] is None
    assert (models["executions"], models["switches"]) == (2, 0)
    assert models["used"] == [
        {
            "layer": "harness",
            "tool": "codex",
            "model": "gpt-6-luna",
            "observed": True,
        },
        {
            "layer": "delegate",
            "tool": "claude",
            "model": "claude-sonnet-5-5",
            "observed": False,
        },
    ]
    assert "account_name" in h and "account_name" in d
    card = client.get("/api/tasks").json()["items"][0]["models"]
    assert card["delegate"]["executor"] == "claude" and "timeline" not in card
    assert card["used"][0]["model"] == "gpt-6-luna"


def test_old_task_has_unknown_models_not_invented(client, agent) -> None:
    task_id, _ = claimed(client, agent)
    models = client.get(f"/api/tasks/{task_id}").json()["models"]
    assert models == {
        "harness": None,
        "delegate": None,
        "executions": 0,
        "switches": 0,
        "used": [],
    }
    trace = client.get(f"/api/tasks/{task_id}/models").json()
    assert trace["executions"] == [] and trace["timeline"] == []
    assert client.get("/api/tasks/nope/models").status_code == 404


def test_cli_fallback_keeps_every_delegation(client, agent) -> None:
    task_id, attempt = claimed(client, agent)
    post(
        client,
        agent,
        task_id,
        attempt,
        [delegate("d1", state="failed", error_code="gate_failed")],
    )
    res = post(
        client,
        agent,
        task_id,
        attempt,
        [
            delegate(
                "d2",
                requested_model="claude-opus-5-5",
                observed_model="claude-opus-5-5",
                evidence="cli_output",
                state="succeeded",
                started_at=datetime.now(UTC).isoformat(),
                delegation_id="run-2",
                resumed_from="d1",
            )
        ],
        [
            {
                "key": "s2",
                "layer": "delegate",
                "kind": "model_switch",
                "reason_code": "error",
                "explanation": "gate failed on sonnet; next model in chain",
                "from_model": "claude-sonnet-5-5",
                "to_model": "claude-opus-5-5",
                "execution_key": "d2",
                "outcome": "succeeded",
            }
        ],
    )
    assert res.status_code == 200, res.text
    trace = res.json()
    assert sorted(e["key"] for e in trace["executions"]) == ["d1", "d2"]
    by_key = {e["key"]: e for e in trace["executions"]}
    assert by_key["d1"]["state"] == "failed"
    assert by_key["d1"]["error_code"] == "gate_failed"
    assert by_key["d2"]["resumed_from"] == "d1"
    summary = trace["summary"]
    assert summary["delegate"]["requested_model"] == "claude-opus-5-5"
    assert (summary["delegate"]["executions"], summary["switches"]) == (2, 1)


def test_harness_fallback_across_attempts(client, agent) -> None:
    task_id, attempt = claimed(client, agent)
    post(client, agent, task_id, attempt, [harness("h1", state="running")])
    client.post(
        f"/api/tasks/{task_id}/fail",
        json={"attempt": attempt, "error": "boom", "reason": "session_lost"},
        headers=agent,
    )
    again = client.post("/api/tasks/claim", json={}, headers=agent).json()
    assert again["attempt"] == attempt + 1
    res = post(
        client,
        agent,
        task_id,
        again["attempt"],
        [harness("h2", requested_model="gpt-6-sol", configured_model="gpt-6-sol")],
        [
            {
                "key": "s1",
                "layer": "harness",
                "kind": "model_switch",
                "reason_code": "error",
                "from_model": "gpt-6-luna",
                "to_model": "gpt-6-sol",
                "policy_revision": REV,
            }
        ],
    )
    trace = res.json()
    assert [(e["key"], e["attempt"]) for e in trace["executions"]] == [
        ("h1", attempt),
        ("h2", attempt + 1),
    ]
    assert trace["summary"]["harness"]["configured_model"] == "gpt-6-sol"
    # The fenced attempt can no longer write.
    assert post(client, agent, task_id, attempt, [harness("h3")]).status_code == 409


def test_account_only_switch_is_not_a_model_switch(client, agent) -> None:
    task_id, attempt = claimed(client, agent)
    base = {"layer": "harness", "reason_code": "quota", "from_model": "gpt-6-luna"}
    same = {
        **base,
        "to_model": "gpt-6-luna",
        "from_account_id": "a",
        "to_account_id": "b",
    }
    ok = post(
        client,
        agent,
        task_id,
        attempt,
        [],
        [{"key": "s1", "kind": "account_switch", **same}],
    )
    assert ok.status_code == 200
    for kind in ("model_switch", "provider_switch", "executor_switch"):
        res = post(
            client,
            agent,
            task_id,
            attempt,
            [],
            [{"key": f"x-{kind}", "kind": kind, **same}],
        )
        assert res.status_code == 422, kind
    changed = {**same, "to_model": "gpt-6-sol"}
    res = post(
        client,
        agent,
        task_id,
        attempt,
        [],
        [{"key": "s2", "kind": "account_switch", **changed}],
    )
    assert res.status_code == 422
    retry = {"key": "s3", "kind": "retry_no_fallback", "layer": "delegate"}
    retry |= {"reason_code": "error", "from_model": "m", "to_model": "m"}
    assert post(client, agent, task_id, attempt, [], [retry]).status_code == 200


def test_failure_before_model_started(client, agent) -> None:
    task_id, attempt = claimed(client, agent)
    failed = delegate("d1", state="failed", error_code="auth_missing")
    res = post(client, agent, task_id, attempt, [failed])
    run = res.json()["executions"][0]
    assert run["started_at"] is None and run["ended_at"] is not None
    assert run["observed_model"] is None and run["evidence"] == "not_reported"
    # Nothing ran, so nothing can have been observed.
    bad = delegate("d2", state="failed", observed_model="m", evidence="cli_output")
    assert post(client, agent, task_id, attempt, [bad]).status_code == 422
    # Evidence and an observed model go together.
    for kw in ({"observed_model": "m"}, {"evidence": "cli_output"}):
        assert (
            post(client, agent, task_id, attempt, [delegate("d3", **kw)]).status_code
            == 422
        )


def test_resend_completes_but_never_overwrites(client, agent) -> None:
    task_id, attempt = claimed(client, agent)
    post(client, agent, task_id, attempt, [delegate("d1")])
    events = len(client.get(f"/api/events?subject_id={task_id}").json())
    post(client, agent, task_id, attempt, [delegate("d1")])  # identical
    assert len(client.get(f"/api/events?subject_id={task_id}").json()) == events
    done = delegate(
        "d1",
        state="succeeded",
        observed_model="claude-sonnet-5-5",
        evidence="cli_output",
    )
    run = post(client, agent, task_id, attempt, [done]).json()["executions"][0]
    assert run["state"] == "succeeded" and run["observed_model"] == "claude-sonnet-5-5"
    # A later report cannot rewrite what was observed, move it back, or change it.
    for kw in (
        {
            "observed_model": "other",
            "evidence": "provider_response",
            "state": "succeeded",
        },
        {"requested_model": "claude-opus-5-5"},
    ):
        assert (
            post(client, agent, task_id, attempt, [{**done, **kw}]).status_code == 409
        )
    back = post(client, agent, task_id, attempt, [delegate("d1", state="running")])
    assert back.json()["executions"][0]["state"] == "succeeded"
    other = {**done, "state": "failed"}
    assert post(client, agent, task_id, attempt, [other]).status_code == 409


def test_authorization(client, agent) -> None:
    task_id, attempt = claimed(client, agent)
    url = f"/api/tasks/{task_id}/models"
    body = {"attempt": attempt, "executions": [delegate()]}
    stranger = register(client)
    assert client.post(url, json=body, headers=stranger).status_code == 409
    assert client.post(url, json=body).status_code == 403  # admin is not an agent
    assert client.post(url, json=body, headers={"Authorization": ""}).status_code == 401
    assert client.get(url, headers={"Authorization": ""}).status_code == 401
    # reads follow the worker read scope: a worker that never leased it is refused
    refused = client.get(url, headers=stranger)
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "not_leased"
    assert client.get(url, headers=agent).status_code == 200  # the leaseholder
    assert client.get(url).status_code == 200  # managers stay global
    assert client.post(url, json={"attempt": attempt}, headers=agent).status_code == 422
    done = client.post(
        f"/api/tasks/{task_id}/complete",
        json={"attempt": attempt, "result": {"summary": "x"}},
        headers=agent,
    )
    assert done.status_code == 200
    assert client.post(url, json=body, headers=agent).status_code == 409  # not claimed


def test_sanitization(client, agent, tmp_path: Path) -> None:
    task_id, attempt = claimed(client, agent)
    secret = "sk-ant-api03-" + "A1b2C3d4" * 6
    text = f"quota hit; Authorization: Bearer {secret} password=hunter2 " + "x " * 400
    sel = {"key": "s1", "layer": "harness", "kind": "initial", "reason_code": "quota"}
    res = post(client, agent, task_id, attempt, [], [{**sel, "explanation": text}])
    assert res.status_code == 200
    explanation = res.json()["timeline"][0]["explanation"]
    assert len(explanation) <= 300 and explanation.endswith("…")
    assert secret not in explanation and "hunter2" not in explanation
    assert "[redacted]" in explanation
    # Identifiers that look like credentials are refused, and the 422 never echoes them.
    bad = post(
        client, agent, task_id, attempt, [delegate("d1", requested_model=secret)]
    )
    assert bad.status_code == 422 and secret not in bad.text
    # Free text is not a prompt channel: events carry codes and ids, never explanations.
    raw = (
        sqlite3.connect(tmp_path / "argus.db")
        .execute("SELECT data FROM events")
        .fetchall()
    )
    assert secret not in json.dumps(raw)
    kinds = [e["kind"] for e in client.get(f"/api/events?subject_id={task_id}").json()]
    assert "task.model_selection" in kinds
    event = next(
        e
        for e in client.get(f"/api/events?subject_id={task_id}").json()
        if e["kind"] == "task.model_selection"
    )
    assert (
        "explanation" not in event["data"] and event["data"]["reason_code"] == "quota"
    )


def test_per_task_cap(client, agent) -> None:
    task_id, attempt = claimed(client, agent)
    for batch in range(10):
        runs = [delegate(f"d{batch}-{i}") for i in range(20)]
        assert post(client, agent, task_id, attempt, runs).status_code == 200
    assert (
        post(client, agent, task_id, attempt, [delegate("one-more")]).status_code == 422
    )
    assert post(client, agent, task_id, attempt, [delegate("d0-0")]).status_code == 200


def test_agent_models_history(client, agent) -> None:
    me = client.get("/api/agents", headers=agent).json()[0]["id"]
    task_id, attempt = claimed(client, agent)
    post(
        client, agent, task_id, attempt, [harness("h1"), delegate("d1"), delegate("d2")]
    )
    got = client.get(f"/api/agents/{me}/models").json()
    assert got["current"]["delegate"]["executions"] == 2
    assert got["current"]["harness"]["evidence"] == "not_reported"
    assert [r["key"] for r in got["history"]] == ["d2", "d1", "h1"][
        : len(got["history"])
    ]
    assert client.get("/api/agents/ghost/models").status_code == 404


def test_mcp_tools(client, agent) -> None:
    token = agent["Authorization"].removeprefix("Bearer ")
    task_id, attempt = claimed(client, agent)
    out = mcp_tool(
        client,
        token,
        "report_task_models",
        task_id=task_id,
        attempt=attempt,
        executions=[delegate("d1")],
    )
    assert out["summary"]["delegate"]["requested_model"] == "claude-sonnet-5-5"
    got = mcp_tool(client, token, "get_task_models", task_id=task_id)
    assert got["executions"][0]["observed_model"] is None
    assert "error" in mcp_tool(
        client,
        ADMIN_TOKEN,
        "report_task_models",
        task_id=task_id,
        attempt=1,
        executions=[delegate("d9")],
    )
    assert mcp_server.audiences["get_task_models"] == mcp_server.audiences["get_task"]


async def test_usage_correlation_is_unknown_until_reported(db: Database) -> None:
    store = MemorySecretStore()
    await insert_agent(db, "agent-a")
    acct = await add(db, store, "acct")
    async with db.write() as tx:
        await tasks.create_task(tx, TaskCreate(project="demo", title="t"), "admin:test")
        task = await tasks.claim(tx, "agent-a", [])
        assert task is not None
        for key, run_key in (
            ("d1", "r1:t:a:claude:claude:1"),
            ("d2", "r1:t:a:claude:claude:2"),
        ):
            await modeltrace.record_execution(
                tx,
                "agent-a",
                task.id,
                task.attempt,
                ExecutionReport(
                    key=key,
                    layer="delegate",
                    state="succeeded",
                    account_id=acct,
                    run_key=run_key,
                ),
            )
        await usage.insert_usage(
            tx,
            acct,
            "agent-a",
            task.id,
            run_key="r1:t:a:claude:claude:1",
            measurement="not_reported",
        )
    async with db.read() as conn:
        trace = await modeltrace.get_trace(conn, task.id)
    first, second = trace.executions
    assert first.usage is not None and first.usage.measurement == "not_reported"
    assert first.usage.input_tokens is None and first.usage.cost_usd is None  # not 0
    assert second.usage is None
    assert first.account_name == "acct" and second.account_name == "acct"


async def test_quota_failover_records_account_switch(db: Database) -> None:
    store = MemorySecretStore()
    for agent in ("agent-a",):
        await insert_agent(db, agent)
    a = await add(db, store, "first", priority=10)
    b = await add(db, store, "second", priority=1)
    c = await add(db, store, "third", priority=0, provider="openai")
    async with db.write() as tx:
        from argus_api.accounts import service as accounts

        await tasks.create_task(tx, TaskCreate(project="demo", title="t"), "admin:test")
        task = await tasks.claim(tx, "agent-a", [])
        assert task is not None
        # Leases are bound to a live claim since #46.
        await accounts.lease(
            tx,
            "agent-a",
            ["anthropic", "openai"],
            store,
            task_id=task.id,
            attempt=task.attempt,
        )
    await fail(db, store, task, agent="agent-a")
    async with db.read() as conn:
        trace = await modeltrace.get_trace(conn, task.id)
    (switch,) = trace.timeline
    assert (switch.kind, switch.layer, switch.reason_code) == (
        "account_switch",
        "harness",
        "quota",
    )
    assert (switch.from_account_id, switch.to_account_id) == (a, b)
    assert (switch.from_account_name, switch.to_account_name) == ("first", "second")
    assert (
        switch.from_model is None and switch.to_model is None
    )  # model unknown, not guessed
    assert QUOTA and c  # the third account (other provider) is the next fallback
    async with db.write() as tx:
        task = await tasks.claim(tx, "agent-a", [])
        assert task is not None
        from argus_api.accounts import service as accounts

        # The switch happens at the next claim: it leases the next account.
        await accounts.lease(
            tx,
            "agent-a",
            ["anthropic", "openai"],
            store,
            task_id=task.id,
            attempt=task.attempt,
        )
    await fail(db, store, task, agent="agent-a")
    async with db.read() as conn:
        trace = await modeltrace.get_trace(conn, task.id)
    assert [s.kind for s in trace.timeline] == ["account_switch", "provider_switch"]
    assert trace.timeline[1].to_provider == "openai"
    assert trace.summary.switches == 2 and trace.summary.harness is None


def test_selection_report_rejects_mislabeled_kinds() -> None:
    with pytest.raises(ValueError):
        SelectionReport(
            key="k",
            layer="harness",
            kind="initial",
            reason_code="policy",
            from_model="m",
        )
    with pytest.raises(ValueError):
        SelectionReport(
            key="k",
            layer="harness",
            kind="model_switch",
            reason_code="error",
            to_model="m",
        )
