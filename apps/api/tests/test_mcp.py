import itertools
import json
from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api.main import app
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
    headers = {"Accept": "application/json, text/event-stream"}
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
        return {"error": text.split(": ", 1)[1] if text.startswith("Error executing") else text}
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
    tools = {t["name"]: t for t in rpc(client, ADMIN_TOKEN, "tools/list")["tools"]}
    for name in (
        "claim_task",
        "heartbeat",
        "report_progress",
        "complete_task",
        "fail_task",
        "ask_question",
        "search_knowledge",
        "write_knowledge",
        "lease_account",
        "report_account_exhausted",
        "scale_pool",
        "answer_decision",
    ):
        assert tools[name]["description"], name
    assert "task_id" in tools["complete_task"]["inputSchema"]["properties"]


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
    [open_decision] = tool(client, orchestrator, "list_decisions")
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
