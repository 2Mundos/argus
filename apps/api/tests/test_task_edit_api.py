"""Task edit over REST and the real MCP endpoint: parity, permissions, errors."""

import pytest
import test_mcp
from fastapi.testclient import TestClient
from test_mcp import ADMIN_TOKEN, listed, register, rpc, tool

client = test_mcp.client  # the same app fixture as the MCP tests

FIELDS = {
    "title": "New title",
    "spec": "New spec",
    "acceptance": ["one", "two"],
    "priority": 7,
    "required_capabilities": ["a", "b"],
}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make(client: TestClient, **kw) -> dict:
    res = client.post(
        "/api/tasks",
        json={"project": "global", "title": "t", **kw},
        headers=bearer(ADMIN_TOKEN),
    )
    assert res.status_code == 201, res.text
    return res.json()


def rest_edit(client: TestClient, token: str, task: dict, **changes):
    return client.patch(
        f"/api/tasks/{task['id']}",
        json={"expected_version": task["version"], "changes": changes},
        headers=bearer(token),
    )


def mcp_edit(client: TestClient, token: str, task: dict, **changes) -> dict:
    return tool(
        client,
        token,
        "edit_task",
        task_id=task["id"],
        expected_version=task["version"],
        changes=changes,
    )


def read_rest(client: TestClient, task_id: str) -> dict:
    return client.get(f"/api/tasks/{task_id}", headers=bearer(ADMIN_TOKEN)).json()


def read_mcp(client: TestClient, task_id: str) -> dict:
    out = tool(client, ADMIN_TOKEN, "get_tasks", ids=[task_id], view="detail")
    return out["items"][0]


def test_rest_and_mcp_edits_give_the_same_task_and_readback(client: TestClient) -> None:
    via_rest, via_mcp = make(client), make(client)
    dep = make(client)
    patch = {**FIELDS, "depends_on": [dep["id"]]}
    rest = rest_edit(client, ADMIN_TOKEN, via_rest, **patch)
    assert rest.status_code == 200, rest.text
    mcp = mcp_edit(client, ADMIN_TOKEN, via_mcp, **patch)
    assert mcp["title"] == FIELDS["title"], mcp
    for edited, original in ((rest.json(), via_rest), (mcp, via_mcp)):
        assert edited["version"] > original["version"]
        assert {k: edited[k] for k in patch} == patch
        assert edited["state"] == "pending"
    # readback through both surfaces matches what the edit returned
    assert read_rest(client, via_rest["id"]) == rest.json()
    assert read_rest(client, via_mcp["id"]) == mcp
    for mine in (via_rest, via_mcp):
        got = read_mcp(client, mine["id"])
        assert got["version"] == read_rest(client, mine["id"])["version"]
        assert got["title"] == FIELDS["title"] and got["spec"] == FIELDS["spec"]


@pytest.mark.parametrize("surface", ["rest", "mcp"])
def test_omitted_is_kept_and_null_clears_on_both_surfaces(
    client: TestClient, surface: str
) -> None:
    task = make(client, spec="s", acceptance=["a"], required_capabilities=["c"])

    def edit(t: dict, **changes) -> dict:
        if surface == "rest":
            return rest_edit(client, ADMIN_TOKEN, t, **changes).json()
        return mcp_edit(client, ADMIN_TOKEN, t, **changes)

    titled = edit(task, title="only title")
    assert (titled["spec"], titled["acceptance"]) == ("s", ["a"])
    assert titled["required_capabilities"] == ["c"]
    cleared = edit(titled, spec=None, acceptance=None, required_capabilities=None)
    assert (cleared["spec"], cleared["acceptance"]) == ("", [])
    assert cleared["required_capabilities"] == []
    assert cleared["title"] == "only title"


def test_permissions_match_on_rest_and_mcp(client: TestClient) -> None:
    worker = register(client, "worker")
    orchestrator = register(client, "orchestrator")
    task = make(client)

    assert rest_edit(client, worker, task, title="w").status_code == 403
    denied = tool(
        client,
        worker,
        "edit_task",
        task_id=task["id"],
        expected_version=task["version"],
        changes={"title": "w"},
    )
    assert "error" in denied
    assert "edit_task" not in listed(client, worker)
    assert read_rest(client, task["id"])["title"] == "t"

    for token in (orchestrator, ADMIN_TOKEN):
        assert "edit_task" in listed(client, token)
    ok = rest_edit(client, orchestrator, task, title="by orchestrator")
    assert ok.status_code == 200
    again = mcp_edit(client, ADMIN_TOKEN, ok.json(), title="by admin")
    assert again["title"] == "by admin"
    no_token = client.patch(
        f"/api/tasks/{task['id']}",
        json={"expected_version": "x", "changes": {"title": "n"}},
        headers={"Authorization": ""},
    )
    assert no_token.status_code == 401


@pytest.mark.parametrize("field", ["state", "assignee_agent_id", "project", "kind"])
def test_immutable_fields_give_the_same_error_on_both_surfaces(
    client: TestClient, field: str
) -> None:
    task = make(client)
    rest = rest_edit(client, ADMIN_TOKEN, task, **{field: "x"})
    assert rest.status_code == 422
    detail = rest.json()["detail"]
    assert (detail["code"], detail["details"]) == ("immutable_field", [field])
    mcp = mcp_edit(client, ADMIN_TOKEN, task, **{field: "x"})
    assert "422 immutable_field" in mcp["error"] and field in mcp["error"]
    assert read_rest(client, task["id"]) == task


def test_stale_and_state_errors_are_structured_on_both_surfaces(
    client: TestClient,
) -> None:
    task = make(client)
    fresh = rest_edit(client, ADMIN_TOKEN, task, title="fresh").json()
    stale = rest_edit(client, ADMIN_TOKEN, task, title="late")
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_version"
    assert stale.json()["detail"]["details"] == [fresh["version"]]
    text = mcp_edit(client, ADMIN_TOKEN, task, title="late")["error"]
    assert "409 stale_version" in text and fresh["version"] in text

    cancelled = client.post(
        f"/api/tasks/{task['id']}/cancel", headers=bearer(ADMIN_TOKEN)
    )
    assert cancelled.status_code == 200, cancelled.text
    done = cancelled.json()
    assert done["editable_fields"] == []
    blocked = rest_edit(client, ADMIN_TOKEN, done, title="x")
    assert blocked.json()["detail"]["code"] == "task_not_editable"
    assert (
        "409 task_not_editable"
        in mcp_edit(client, ADMIN_TOKEN, done, title="x")["error"]
    )


def test_validation_errors_match_and_write_nothing(client: TestClient) -> None:
    a = make(client)
    b = make(client, depends_on=[a["id"]])
    cases = [
        ({"title": ""}, "422", "title"),
        ({"depends_on": [a["id"]]}, "422", "dependency_cycle"),
        ({"depends_on": [b["id"]]}, "422", "dependency_cycle"),
        ({"depends_on": ["ghost"]}, "404", "ghost"),
        ({"knowledge": ["no-page"]}, "404", "no-page"),
    ]
    for changes, status, needle in cases:
        rest = rest_edit(client, ADMIN_TOKEN, a, priority=9, **changes)
        assert str(rest.status_code) == status, (changes, rest.text)
        assert needle in rest.text
        mcp = mcp_edit(client, ADMIN_TOKEN, a, priority=9, **changes)
        assert status in mcp["error"] and needle in mcp["error"]
    assert read_rest(client, a["id"]) == a


def test_editable_fields_come_from_one_source_in_task_and_tool_text(
    client: TestClient,
) -> None:
    task = make(client)
    assert set(task["editable_fields"]) == {
        "title",
        "spec",
        "acceptance",
        "priority",
        "required_capabilities",
        "knowledge",
        "depends_on",
        "complexity",
        "account_providers",
        "harness_model",
        "executor_model",
    }
    listing = rpc(client, ADMIN_TOKEN, "tools/list")["tools"]
    schema = next(t for t in listing if t["name"] == "edit_task")
    assert "changes" in schema["inputSchema"]["properties"]
    assert "expected_version" in schema["inputSchema"]["required"]


def test_model_pins_over_rest_and_mcp(client: TestClient) -> None:
    opus = {"engine": "claude", "model": "claude-opus-5-5"}
    sol = {"executor": "codex", "model": "gpt-6.1-sol"}
    created = tool(
        client,
        ADMIN_TOKEN,
        "create_task",
        title="pinned",
        project="global",
        harness_model=opus,
    )
    assert created["harness_model"] == opus and created["executor_model"] is None
    edited = mcp_edit(client, ADMIN_TOKEN, created, executor_model=sol)
    assert edited["executor_model"] == sol
    cleared = rest_edit(client, ADMIN_TOKEN, edited, harness_model=None)
    assert cleared.status_code == 200 and cleared.json()["harness_model"] is None
    refused = rest_edit(
        client,
        ADMIN_TOKEN,
        cleared.json(),
        harness_model={"engine": "agent", "model": "x"},
    )
    assert refused.status_code == 422 and "harness_model" in refused.text


def test_attempt_snapshots_are_listed_over_rest(client: TestClient) -> None:
    worker = register(client, "worker")
    task = make(client, spec="v1")
    claimed = client.post("/api/tasks/claim", json={}, headers=bearer(worker)).json()
    assert claimed["id"] == task["id"]
    late = rest_edit(client, ADMIN_TOKEN, task, spec="v2")
    assert late.status_code == 409
    listing = client.get(f"/api/tasks/{task['id']}/attempts", headers=bearer(worker))
    [snap] = listing.json()
    assert (snap["attempt"], snap["definition"]["spec"]) == (1, "v1")
    assert (
        client.get("/api/tasks/ghost/attempts", headers=bearer(worker)).status_code
        == 404
    )


def page(client: TestClient, project: str, body: str) -> str:
    res = client.post(
        "/api/knowledge/pages",
        json={"project": project, "title": f"page {body}", "body": body},
        headers=bearer(ADMIN_TOKEN),
    )
    assert res.status_code == 201, res.text
    return res.json()["page"]["id"]


def test_pins_edit_alike_and_never_cross_projects_on_both_surfaces(
    client: TestClient,
) -> None:
    pin = page(client, "global", "shared notes")
    put = client.put(
        "/api/projects/other", json={"name": "other"}, headers=bearer(ADMIN_TOKEN)
    )
    assert put.status_code == 200, put.text
    foreign = page(client, "other", "other-project-secret")
    via_rest, via_mcp = make(client), make(client)

    rest = rest_edit(client, ADMIN_TOKEN, via_rest, knowledge=[pin]).json()
    mcp = mcp_edit(client, ADMIN_TOKEN, via_mcp, knowledge=[pin])
    assert rest["knowledge"] == mcp["knowledge"] == [pin]
    assert read_mcp(client, via_mcp["id"])["version"] == mcp["version"]

    refused = rest_edit(client, ADMIN_TOKEN, rest, knowledge=[pin, foreign])
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "validation_failed"
    error = mcp_edit(client, ADMIN_TOKEN, mcp, knowledge=[pin, foreign])["error"]
    assert "422 validation_failed" in error and foreign in error
    for text in (refused.text, error):
        assert "other-project-secret" not in text
    assert read_rest(client, via_rest["id"]) == rest

    unpinned = mcp_edit(client, ADMIN_TOKEN, mcp, knowledge=None)
    assert unpinned["knowledge"] == []
