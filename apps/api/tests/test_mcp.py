import base64
import itertools
import json
import re
from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api import mcp_server
from argus_api.main import app
from argus_core.auth import Scope
from fastapi.testclient import TestClient

ADMIN_TOKEN = "test-admin-token-" + "m" * 32
_ids = itertools.count(1)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        yield c


def rpc(
    client: TestClient, token: str | None, method: str, params: dict | None = None
) -> dict:
    # Host must be on the MCP DNS-rebinding allowlist (security-hardening spec).
    headers = {"Accept": "application/json, text/event-stream", "Host": "127.0.0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    res = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": next(_ids),
            "method": method,
            "params": params or {},
        },
    )
    assert res.status_code == 200, res.text
    return res.json()["result"]


def tool(client: TestClient, token: str | None, name: str, /, **arguments) -> dict:
    """Call a tool; return {"error": text} or the structured result."""
    result = rpc(client, token, "tools/call", {"name": name, "arguments": arguments})
    text = result["content"][0]["text"] if result["content"] else ""
    if result["isError"]:
        return {
            "error": text.split(": ", 1)[1]
            if text.startswith("Error executing")
            else text
        }
    if "structuredContent" in result:
        return result["structuredContent"].get("result", result["structuredContent"])
    return json.loads(text)


def register(client: TestClient, role: str = "worker") -> str:
    h = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    boot = client.post(
        "/api/agents/bootstrap-tokens", json={"role": role}, headers=h
    ).json()
    return client.post(
        "/api/agents/register", json={"bootstrap_token": boot["token"]}
    ).json()["token"]


def test_tools_are_listed_with_descriptions(client: TestClient) -> None:
    orchestrator = register(client, "orchestrator")
    tools = {t["name"]: t for t in rpc(client, orchestrator, "tools/list")["tools"]}
    admin = {t["name"] for t in rpc(client, ADMIN_TOKEN, "tools/list")["tools"]}
    assert set(tools) | admin == set(mcp_server.audiences)  # each has an audience
    for name, t in tools.items():
        assert t["description"], name
    assert "task_id" in tools["complete_task"]["inputSchema"]["properties"]


def listed(client: TestClient, token: str) -> set[str]:
    return {t["name"] for t in rpc(client, token, "tools/list")["tools"]}


def test_tools_are_listed_by_scope(client: TestClient) -> None:
    by_scope = {
        Scope.WORKER: listed(client, register(client)),
        Scope.ORCHESTRATOR: listed(client, register(client, "orchestrator")),
        Scope.ADMIN: listed(client, ADMIN_TOKEN),
    }
    for scope, names in by_scope.items():
        assert names == {n for n, a in mcp_server.audiences.items() if scope in a}
    assert "claim_task" in by_scope[Scope.WORKER]
    assert "claim_task" not in by_scope[Scope.ADMIN]
    assert "answer_decision" not in by_scope[Scope.WORKER]
    admin_only = {n for n, a in mcp_server.audiences.items() if a == {Scope.ADMIN}}
    assert admin_only == {
        "update_pool",
        "set_model_policy",
        "reset_model_policy",
        "set_project_runtime",
        "rebuild_project_runtime",
        "archive_agents",
    }
    assert by_scope[Scope.ORCHESTRATOR] == set(mcp_server.audiences) - admin_only


@pytest.mark.parametrize("token", [None, "not-a-token"])
def test_tools_list_needs_a_valid_token(client: TestClient, token: str | None) -> None:
    headers = {"Accept": "application/json, text/event-stream", "Host": "127.0.0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    res = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert "401 unauthorized" in res.json()["error"]["message"]


def test_mcp_rejects_unlisted_host(client: TestClient) -> None:
    token = register(client)
    res = client.post(
        "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "Host": "evil.example",
            "Authorization": f"Bearer {token}",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert res.status_code == 421
    assert "Invalid Host" in res.text


def _placeholder(schema: dict) -> object:
    kind = schema.get("type") or next(
        (s["type"] for s in schema.get("anyOf", []) if s.get("type") != "null"), None
    )
    return {"integer": 1, "number": 1, "boolean": False, "array": [], "object": {}}.get(
        kind, "x"
    )


@pytest.mark.parametrize("role", ["worker", "orchestrator", "admin"])
def test_no_listed_tool_is_refused_for_scope(client: TestClient, role: str) -> None:
    """Listing must not advertise a tool whose REST endpoint rejects the caller's
    scope. Placeholder arguments may fail later (404, 409, 422), never on scope."""
    token = ADMIN_TOKEN if role == "admin" else register(client, role)
    tools = rpc(client, token, "tools/list")["tools"]
    for t in sorted(tools, key=lambda t: t["name"] == "leave"):  # leave ends the token
        schema = t["inputSchema"]
        args = {
            name: _placeholder(schema["properties"][name])
            for name in schema.get("required", [])
        }
        if t["name"] in ("claim_task", "wait_for_events"):
            args["wait_s"] = 0
        result = tool(client, token, t["name"], **args)
        error = result.get("error", "") if isinstance(result, dict) else ""
        assert "requires scope" not in error, (t["name"], error)
        assert "requires an agent token" not in error, (t["name"], error)


def test_unlisted_tools_still_follow_rest_rules(client: TestClient) -> None:
    worker = register(client)
    assert tool(client, worker, "list_tasks")["total"] == 0  # any caller may read
    assert "requires scope" in tool(client, worker, "cancel_task", task_id="t")["error"]


def test_worker_flow_over_mcp(client: TestClient) -> None:
    worker = register(client)
    created = tool(
        client,
        ADMIN_TOKEN,
        "create_task",
        title="Write release notes",
        project="global",
    )
    assert created["state"] == "ready"
    assert (
        tool(
            client,
            ADMIN_TOKEN,
            "write_knowledge",
            title="Release notes style",
            body="Use bullet points.",
            reason="convention",
        )["status"]
        == "published"
    )

    claimed = tool(client, worker, "claim_task", wait_s=0)["task"]
    assert claimed["id"] == created["id"]
    assert [r["title"] for r in claimed["knowledge_refs"]] == ["Release notes style"]
    task_id, attempt = claimed["id"], claimed["attempt"]

    beat = tool(
        client, worker, "heartbeat", leases=[{"task_id": task_id, "attempt": attempt}]
    )
    assert [r["task_id"] for r in beat["renewed"]] == [task_id]
    tool(
        client,
        worker,
        "report_progress",
        task_id=task_id,
        attempt=attempt,
        message="drafting",
    )
    artifact = tool(
        client,
        worker,
        "upload_text_artifact",
        task_id=task_id,
        attempt=attempt,
        name="notes.md",
        content="# v1\n- things",
        media_type="text/markdown",
    )
    done = tool(
        client,
        worker,
        "complete_task",
        task_id=task_id,
        attempt=attempt,
        summary="Wrote them",
        artifacts=[artifact["id"]],
        input_tokens=10,
    )
    assert done["state"] == "succeeded"
    assert done["result"]["artifacts"] == [artifact["id"]]
    assert tool(client, worker, "claim_task", wait_s=0) == {"task": None}


def test_errors_come_back_as_tool_errors_with_codes(client: TestClient) -> None:
    worker = register(client)
    tool(client, ADMIN_TOKEN, "create_task", title="t", project="global")
    claimed = tool(client, worker, "claim_task", wait_s=0)["task"]
    stale = tool(
        client, worker, "complete_task", task_id=claimed["id"], attempt=99, summary="x"
    )
    assert stale["error"].startswith("409 stale_attempt:")
    forbidden = tool(client, worker, "scale_pool", name="web", desired_replicas=3)
    assert forbidden["error"].startswith("403 forbidden:")
    invalid = tool(client, ADMIN_TOKEN, "create_task", title="no project")
    assert "project_required" in invalid["error"]


def test_tools_need_a_token(client: TestClient) -> None:
    assert "missing token" in tool(client, None, "list_tasks")["error"]
    assert tool(client, "bogus", "list_tasks")["error"].startswith("401 invalid_token")


def test_ask_and_answer_over_mcp(client: TestClient) -> None:
    worker, orchestrator = register(client), register(client, "orchestrator")
    tool(client, orchestrator, "create_task", title="Pick a DB", project="global")
    claimed = tool(client, worker, "claim_task", wait_s=0)["task"]
    asked = tool(
        client,
        worker,
        "ask_question",
        task_id=claimed["id"],
        attempt=claimed["attempt"],
        question="Postgres or SQLite?",
        options=["postgres", "sqlite"],
    )
    [open_decision] = tool(client, orchestrator, "list_decisions")["items"]
    assert open_decision["id"] == asked["id"]
    tool(
        client,
        orchestrator,
        "answer_decision",
        decision_id=asked["id"],
        choice="sqlite",
    )
    again = tool(client, worker, "claim_task", wait_s=0)["task"]
    assert "sqlite" in again["spec"]


def test_dict_results_also_come_as_text_content(client: TestClient) -> None:
    result = rpc(
        client,
        ADMIN_TOKEN,
        "tools/call",
        {"name": "create_task", "arguments": {"title": "t", "project": "global"}},
    )
    text = result["content"][0]["text"]
    assert json.loads(text)["title"] == "t"  # for clients that only read content


def test_inbox_wait_wakes_only_for_new_inbox_rows(client: TestClient) -> None:
    worker, orchestrator = register(client), register(client, "orchestrator")
    start = tool(client, orchestrator, "wait_for_events", wait_s=0)["next_cursor"]
    tool(client, orchestrator, "create_task", title="t", project="global")
    claimed = tool(client, worker, "claim_task", wait_s=0)["task"]
    task_id, attempt = claimed["id"], claimed["attempt"]
    tool(
        client, worker, "report_progress", task_id=task_id, attempt=attempt, message="m"
    )

    # Created, claimed and progress are not inbox rows; the cursor still moves
    # past them so the next wait does not rescan them.
    idle = tool(
        client, orchestrator, "wait_for_events", after=start, wait_s=0, inbox=True
    )
    assert idle["timed_out"] and idle["events"] == []
    assert idle["next_cursor"] > start

    asked = tool(
        client,
        worker,
        "ask_question",
        task_id=task_id,
        attempt=attempt,
        question="Which?",
    )
    woke = tool(
        client,
        orchestrator,
        "wait_for_events",
        after=idle["next_cursor"],
        wait_s=0,
        inbox=True,
    )
    kinds = {(e["kind"], e["subject_id"]) for e in woke["events"]}
    assert ("decision.opened", asked["id"]) in kinds
    assert all(e["kind"] != "task.progress" for e in woke["events"])
    assert any(e["data"].get("to") == "blocked" for e in woke["events"])


def test_inbox_journey_tools_match_rest(client: TestClient) -> None:
    worker, orchestrator = register(client), register(client, "orchestrator")
    rest = {"Authorization": f"Bearer {orchestrator}"}
    tool(client, orchestrator, "create_task", title="Review me", project="global")
    claimed = tool(client, worker, "claim_task", wait_s=0)["task"]
    task_id, attempt = claimed["id"], claimed["attempt"]
    artifact = tool(
        client,
        worker,
        "upload_text_artifact",
        task_id=task_id,
        attempt=attempt,
        name="report.md",
        content="verdict: pass",
        role="report",
    )
    asked = tool(
        client,
        worker,
        "ask_question",
        task_id=task_id,
        attempt=attempt,
        question="Ship it?",
        options=["yes", "no"],
    )

    def without_ages(page: dict) -> dict:
        return {
            **page,
            "items": [{**r, "age_s": 0} for r in page["items"]],
            "groups": [{**g, "oldest_age_s": 0} for g in page["groups"]],
        }

    inbox = tool(client, orchestrator, "get_inbox", project="global")
    direct = client.get("/api/inbox?project=global", headers=rest).json()
    assert without_ages(inbox) == without_ages(direct)
    [row] = inbox["items"]
    assert (row["decision_id"], row["artifact_ids"]) == (asked["id"], [artifact["id"]])
    assert [(g["key"], g["open_decisions"]) for g in inbox["groups"]] == [
        (f"task:{task_id}", 1)
    ]
    mcp = tool(
        client, orchestrator, "get_inbox", project="global", blocking_only=True,
        order="impact",
    )  # fmt: skip
    query = "project=global&blocking_only=true&order=impact"
    rest_page = client.get(f"/api/inbox?{query}", headers=rest).json()
    assert without_ages(mcp) == without_ages(rest_page)
    assert [r["impact"] for r in mcp["items"]] == [1]
    page = tool(
        client, orchestrator, "list_decisions", kinds=["question"], view="detail"
    )
    assert page["items"][0]["question"] == "Ship it?"
    assert tool(client, orchestrator, "get_decision", decision_id=asked["id"])[
        "options"
    ] == ["yes", "no"]

    [listed] = tool(client, orchestrator, "list_artifacts", task_id=task_id)
    assert (listed["role"], listed["in_result"]) == ("report", False)
    assert tool(client, orchestrator, "verify_artifact", artifact_id=artifact["id"])[
        "ok"
    ]
    read = tool(client, orchestrator, "read_artifact", artifact_id=artifact["id"])
    assert (read["text"], read["next_offset"]) == ("verdict: pass", None)

    unblocked = tool(client, orchestrator, "unblock_task", task_id=task_id)
    assert unblocked["state"] == "ready"
    batch = tool(client, orchestrator, "get_tasks", ids=[task_id, "gone"])
    assert ([t["state"] for t in batch["items"]], batch["missing"]) == (
        ["ready"],
        ["gone"],
    )
    retry = tool(client, orchestrator, "retry_task", task_id=task_id)
    assert retry["error"].startswith("409 task_not_failed")
    tasks = tool(client, orchestrator, "list_tasks", states=["ready"], limit=1)
    assert (tasks["total"], tasks["has_more"]) == (1, False)


def test_release_task_over_mcp(client: TestClient) -> None:
    orchestrator, worker = register(client, "orchestrator"), register(client)
    task = tool(client, ADMIN_TOKEN, "create_task", title="t", project="global")
    claimed = tool(client, worker, "claim_task", wait_s=0)["task"]
    assert (
        "403"
        in tool(client, worker, "release_task", task_id=task["id"], reason="x")["error"]
    )
    stale = tool(
        client,
        orchestrator,
        "release_task",
        task_id=task["id"],
        reason="recycle",
        attempt=claimed["attempt"] + 1,
    )
    assert stale["error"].startswith("409 stale_attempt")
    released = tool(
        client, orchestrator, "release_task", task_id=task["id"], reason="recycle"
    )
    assert (released["state"], released["failures"]) == ("ready", 0)
    again = tool(client, orchestrator, "release_task", task_id=task["id"], reason="x")
    assert again["error"].startswith("409 task_not_claimed")


def test_answers_over_mcp_are_safe_to_retry(client: TestClient) -> None:
    orchestrator = register(client, "orchestrator")
    ids = []
    for _ in range(2):
        task = tool(client, ADMIN_TOKEN, "create_task", title="t", project="global")
        ids.append(task["id"])
    worker = register(client)
    asked = []
    for _ in ids:
        claimed = tool(client, worker, "claim_task", wait_s=0)["task"]
        asked.append(
            tool(
                client,
                worker,
                "ask_question",
                task_id=claimed["id"],
                attempt=claimed["attempt"],
                question="?",
                options=["a", "b"],
            )["id"]
        )
    one = tool(
        client,
        orchestrator,
        "answer_decision",
        decision_id=asked[0],
        choice="a",
        request_id="r0",
    )
    retry = tool(
        client,
        orchestrator,
        "answer_decision",
        decision_id=asked[0],
        choice="a",
        request_id="r0",
    )
    assert (one["replayed"], retry["replayed"]) == (False, True)
    answers = [
        {"decision_id": asked[0], "choice": "a", "request_id": "r0"},
        {"decision_id": asked[1], "choice": "c", "request_id": "r1"},
    ]
    batch = tool(client, orchestrator, "answer_decisions", answers=answers)
    assert [r["ok"] for r in batch["results"]] == [True, False]
    assert batch["results"][0]["replayed"] is True
    assert batch["results"][1]["error"]["code"] == "invalid_choice"


def test_fleet_tools_forward_and_redact(client: TestClient) -> None:
    from api_support import FakeTarget
    from argus_api.db import ts

    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    client.put(
        "/api/pools/web",
        json={"image": "w:1", "max_replicas": 2, "env": {"KEY": "s3cret"}},
        headers=admin,
    )
    client.app.state.targets.override("docker", None, FakeTarget())

    async def add_instance() -> None:
        async with client.app.state.db.write() as tx:
            await tx.conn.execute(
                "INSERT INTO instances (id, pool, provider_ref, state, created_at)"
                " VALUES ('i-1', 'web', 'argus-i-1', 'running', ?)",
                (ts(tx.now),),
            )

    client.portal.call(add_instance)
    orchestrator = register(client, "orchestrator")

    inspected = tool(client, orchestrator, "inspect_pool", name="web")
    assert inspected["pool"]["env"] == {"KEY": "***"}
    assert [i["id"] for i in inspected["instances"]] == ["i-1"]
    assert tool(client, orchestrator, "get_instance", instance_id="i-1")["state"] == (
        "running"
    )
    logs = tool(client, orchestrator, "get_instance_logs", instance_id="i-1", tail=5)
    assert logs == {"logs": "logs of argus-i-1"}  # plain text passes through
    too_long = tool(
        client, orchestrator, "get_instance_logs", instance_id="i-1", tail=501
    )
    assert "error" in too_long

    recycled = tool(client, orchestrator, "recycle_instance", instance_id="i-1")
    assert recycled["draining"] is True
    requeued = tool(
        client, orchestrator, "recycle_instance", instance_id="i-1", requeue=True
    )
    assert requeued["draining"] is True  # nothing claimed: nothing to release
    version = inspected["pool"]["version"]
    assert "error" in tool(
        client, orchestrator, "update_pool", name="web", expected_version=version
    )
    updated = tool(
        client,
        ADMIN_TOKEN,
        "update_pool",
        name="web",
        expected_version=version,
        max_replicas=3,
        env={"OTHER": "v"},
    )
    assert (updated["max_replicas"], updated["env"]) == (
        3,
        {"KEY": "***", "OTHER": "***"},
    )
    stale = tool(
        client, ADMIN_TOKEN, "update_pool", name="web", expected_version=version
    )
    assert stale["error"].startswith("409 stale_version")

    worker = register(client)
    [agent] = [
        a
        for a in client.get("/api/agents", headers=admin).json()
        if a["role"] == "worker"
    ]
    tool(client, worker, "heartbeat", runtime={"worker_version": "2.0"})
    assert tool(client, orchestrator, "drain_agent", agent_id=agent["id"])["state"] == (
        "draining"
    )
    assert (
        client.get(f"/api/agents/{agent['id']}", headers=admin).json()["runtime"][
            "worker_version"
        ]
        == "2.0"
    )


def test_model_policy_tools_match_rest(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARGUS_MODEL_POLICY", raising=False)
    orchestrator = register(client, "orchestrator")
    worker = register(client)
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    for token in (worker, orchestrator, ADMIN_TOKEN):
        assert "get_model_policy" in listed(client, token)
    assert "set_model_policy" not in listed(client, orchestrator)

    before = tool(client, worker, "get_model_policy")
    assert before == client.get("/api/models", headers=admin).json()
    assert before["source"] == "defaults"

    policy = {
        "harness": {"codex": ["luna"]},
        "implementation": {"agent": ["vendor/model:v1"], "claude": ["a", "b"]},
    }
    for token in (worker, orchestrator):
        assert "error" in tool(client, token, "set_model_policy", **policy)
        assert "error" in tool(client, token, "reset_model_policy")
    bad = tool(
        client, ADMIN_TOKEN, "set_model_policy", harness={}, implementation={"x": ["m"]}
    )
    assert "invalid model chain: implementation.x" in bad["error"]

    saved = tool(client, ADMIN_TOKEN, "set_model_policy", **policy)
    assert saved["source"] == "file"
    assert saved["implementation"] == policy["implementation"]
    assert tool(client, worker, "get_model_policy") == saved
    assert client.get("/api/models", headers=admin).json() == saved

    reset = tool(client, ADMIN_TOKEN, "reset_model_policy")
    assert (reset["source"], reset["revision"]) == ("defaults", before["revision"])


def test_orchestrator_skill_is_served(client: TestClient) -> None:
    token = register(client, "orchestrator")
    uri = "skill://argus-orchestrator/SKILL.md"
    listed = rpc(client, token, "resources/list")["resources"]
    assert [r["uri"] for r in listed] == [uri]
    read = rpc(client, token, "resources/read", {"uri": uri})["contents"][0]
    assert read["mimeType"] == "text/markdown"
    assert read["text"].startswith("---\nname: argus-orchestrator\n")
    prompt = rpc(client, token, "prompts/get", {"name": "argus-orchestrator"})
    assert prompt["messages"][0]["content"]["text"] == read["text"]


def test_orchestrator_skill_names_only_real_tools() -> None:
    # A backticked name that starts like a tool (get_, list_, answer_, ...) must be one.
    verbs = {name.split("_")[0] for name in mcp_server.audiences}
    named = set(re.findall(r"`([a-z]+_[a-z_]+)", mcp_server.ORCHESTRATOR_SKILL))
    tool_like = {n for n in named if n.split("_")[0] in verbs}
    assert tool_like - set(mcp_server.audiences) == set()
    assert {"get_inbox", "answer_decisions", "wait_for_events"} <= tool_like


def test_attach_artifact_is_manager_only_and_round_trips(client: TestClient) -> None:
    worker, orchestrator = register(client), register(client, "orchestrator")
    assert "attach_artifact" not in listed(client, worker)
    assert "attach_artifact" in listed(client, orchestrator)
    assert "attach_artifact" in listed(client, ADMIN_TOKEN)
    created = tool(
        client, orchestrator, "create_task", title="Evidence", project="global"
    )
    task_id = created["id"]
    raw = b"\x00\xff evidence"
    art = tool(
        client,
        orchestrator,
        "attach_artifact",
        task_id=task_id,
        name="shot.bin",
        content_base64=base64.b64encode(raw).decode(),
        media_type="application/octet-stream",
    )
    assert (art["source"], art["size"]) == ("attachment", len(raw))
    rest = client.get(
        f"/api/artifacts/{art['id']}/content",
        headers={"Authorization": f"Bearer {orchestrator}"},
    )
    assert rest.content == raw
    assert "error" in tool(
        client, orchestrator, "attach_artifact", task_id=task_id, name="x"
    )
    denied = tool(
        client, worker, "attach_artifact", task_id=task_id, name="y", content="z"
    )
    assert "error" in denied
