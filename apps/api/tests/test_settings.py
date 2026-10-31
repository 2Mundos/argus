"""Typed settings with per-project overrides (docs/specs/settings.spec.md)."""

import os
from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api import model_policy
from argus_api.errors import ValidationFailedError
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import independent_review as indep
from argus_core.continuation import item_id
from argus_core.tasks import HarnessModel
from fastapi.testclient import TestClient
from test_launch import ADMIN, agent_token, bearer

ADMIN_TOKEN = ADMIN["Authorization"].split()[1]


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    for name in list(os.environ):
        if name.startswith(("ARGUS_", "DOCKER_HOST")) and not name.startswith(
            "ARGUS_TEST_"
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        app.state.secrets = MemorySecretStore()
        yield c


def test_registry_lists_phase1_keys(client: TestClient) -> None:
    body = client.get("/api/settings/registry", headers=ADMIN).json()
    keys = {d["key"] for d in body}
    assert "models" in keys
    assert "review.independent" in keys
    assert "onboarding.dismissed" in keys
    assert "orchestrator.enabled" in keys
    assert "orchestrator.daily_budget_usd" in keys
    review = next(d for d in body if d["key"] == "review.independent")
    assert review["default"] == {"mode": "off", "max_rounds": 3}
    assert "schema" in review
    assert set(review["scopes"]) == {"global", "project"}


def test_global_defaults_and_admin_write(client: TestClient) -> None:
    worker = bearer(agent_token(client, "worker"))
    got = client.get("/api/settings", headers=worker).json()
    by_key = {s["key"]: s for s in got["settings"]}
    assert by_key["review.independent"]["source"] == "default"
    assert by_key["review.independent"]["value"]["mode"] == "off"
    assert by_key["models"]["source"] in ("default", "env", "global")
    assert by_key["models"]["revision"]

    assert (
        client.put(
            "/api/settings/review.independent",
            json={"value": {"mode": "different_model", "max_rounds": 2}},
            headers=worker,
        ).status_code
        == 403
    )
    res = client.put(
        "/api/settings/review.independent",
        json={"value": {"mode": "different_model", "max_rounds": 2}},
        headers=ADMIN,
    )
    assert res.status_code == 200, res.text
    assert res.json()["source"] == "global"
    assert res.json()["value"]["mode"] == "different_model"

    events = client.get(
        "/api/events", params={"kind": "settings.changed"}, headers=ADMIN
    ).json()
    rows = events["items"] if isinstance(events, dict) else events
    assert any(e["kind"] == "settings.changed" for e in rows)


def test_project_override_inherits_and_resets(client: TestClient) -> None:
    client.put(
        "/api/projects/demo",
        json={"name": "Demo", "description": "d"},
        headers=ADMIN,
    )
    client.put(
        "/api/settings/review.independent",
        json={"value": {"mode": "different_provider", "max_rounds": 4}},
        headers=ADMIN,
    )
    got = client.get("/api/projects/demo/settings", headers=ADMIN).json()
    review = next(s for s in got["settings"] if s["key"] == "review.independent")
    assert review["source"] == "global"
    assert review["value"]["mode"] == "different_provider"

    res = client.put(
        "/api/projects/demo/settings/review.independent",
        json={"value": {"mode": "off", "max_rounds": 1}},
        headers=ADMIN,
    )
    assert res.status_code == 200, res.text
    assert res.json()["source"] == "project"
    assert res.json()["value"]["mode"] == "off"

    reset = client.delete(
        "/api/projects/demo/settings/review.independent", headers=ADMIN
    )
    assert reset.status_code == 200
    assert reset.json()["source"] == "global"
    assert reset.json()["value"]["mode"] == "different_provider"


def test_unknown_key_and_bad_value_are_422(client: TestClient) -> None:
    assert (
        client.put("/api/settings/nope", json={"value": 1}, headers=ADMIN).status_code
        == 422
    )
    res = client.put(
        "/api/settings/review.independent",
        json={"value": {"mode": "maybe", "max_rounds": 3}},
        headers=ADMIN,
    )
    assert res.status_code == 422


def test_models_project_override_merges(client: TestClient) -> None:
    client.put(
        "/api/projects/demo", json={"name": "Demo", "description": "d"}, headers=ADMIN
    )
    base = client.get("/api/models", headers=ADMIN).json()
    override = {"harness": {"claude": ["claude-opus-5-5"]}}
    res = client.put(
        "/api/projects/demo/settings/models",
        json={"value": override},
        headers=ADMIN,
    )
    assert res.status_code == 200, res.text
    assert res.json()["source"] == "project"
    assert res.json()["value"]["harness"]["claude"] == ["claude-opus-5-5"]
    for eng, chain in base["implementation"].items():
        assert res.json()["value"]["implementation"][eng] == chain
    assert res.json()["revision"] != base["revision"]

    partial = client.get(
        "/api/projects/demo/settings/models/override", headers=ADMIN
    ).json()
    assert partial["value"] == model_policy.validate_partial(override)


def test_global_models_write_records_previous_policy(client: TestClient) -> None:
    got = client.get("/api/settings", headers=ADMIN).json()
    before = next(s for s in got["settings"] if s["key"] == "models")["value"]
    new = {**before, "harness": {**before["harness"], "claude": ["claude-opus-5-5"]}}
    assert new != before

    res = client.put("/api/settings/models", json={"value": new}, headers=ADMIN)
    assert res.status_code == 200, res.text

    events = client.get(
        "/api/events", params={"kind": "settings.changed"}, headers=ADMIN
    ).json()
    rows = events["items"] if isinstance(events, dict) else events
    changes = [
        e
        for e in rows
        if e["data"]["key"] == "models" and e["data"]["scope"] == "global"
    ]
    newest = max(changes, key=lambda e: e["id"])["data"]
    assert newest["from"] == before
    assert newest["to"] == res.json()["value"]
    assert newest["to"] == new


def test_claim_carries_effective_model_policy(client: TestClient) -> None:
    client.put(
        "/api/projects/demo", json={"name": "Demo", "description": "d"}, headers=ADMIN
    )
    client.put(
        "/api/projects/demo/settings/models",
        json={"value": {"implementation": {"codex": ["gpt-custom"]}}},
        headers=ADMIN,
    )
    task = client.post(
        "/api/tasks",
        json={"title": "t", "project": "demo", "spec": "do it"},
        headers=ADMIN,
    ).json()
    boot = client.post(
        "/api/agents/bootstrap-tokens", json={"role": "worker"}, headers=ADMIN
    ).json()
    agent = client.post(
        "/api/agents/register",
        json={"bootstrap_token": boot["token"], "capabilities": []},
    ).json()
    claimed = client.post(
        "/api/tasks/claim",
        json={"capabilities": [], "wait_s": 0},
        headers=bearer(agent["token"]),
    )
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["id"] == task["id"]
    assert body["model_policy"]["implementation"]["codex"] == ["gpt-custom"]
    assert body["model_policy_revision"]
    assert len(body["model_policy_revision"]) == 64


def test_merge_and_partial_policy_unit() -> None:
    base = model_policy.validate(
        {
            "harness": {"claude": ["a", "b"], "codex": ["c"]},
            "implementation": {"claude": ["d"]},
        }
    )
    override = model_policy.validate_partial({"harness": {"claude": ["z"]}})
    merged = model_policy.merge_policy(base, override)
    assert merged["harness"]["claude"] == ["z"]
    assert merged["harness"]["codex"] == ["c"]
    assert merged["implementation"]["claude"] == ["d"]
    with pytest.raises(ValidationFailedError):
        model_policy.validate_partial({"unknown_map": {"claude": ["x"]}})


def test_pick_reviewer_excludes_author_models() -> None:
    policy = model_policy.validate(
        {
            "harness": {"claude": ["claude-sonnet-5-5", "claude-opus-5-5"]},
            "implementation": {"claude": ["claude-opus-5-5"]},
        }
    )
    harness, executor = indep.pick_reviewer(
        policy,
        mode="different_model",
        author_models={"claude-sonnet-5-5"},
        author_providers={"anthropic"},
        usable={"anthropic"},
    )
    assert harness == HarnessModel(engine="claude", model="claude-opus-5-5")
    assert executor is not None and executor.model == "claude-opus-5-5"

    none, _ = indep.pick_reviewer(
        policy,
        mode="different_provider",
        author_models=set(),
        author_providers={"anthropic"},
        usable={"anthropic"},
    )
    assert none is None


def test_code_review_kind_is_seeded(client: TestClient) -> None:
    kinds = {k["name"]: k for k in client.get("/api/task-kinds", headers=ADMIN).json()}
    assert "code_review" in kinds
    assert kinds["code_review"]["result_schema"]["required"] == [
        "verdict",
        "findings",
        "head_sha",
    ]


def test_independent_review_spawns_child_on_delivery(client: TestClient) -> None:
    client.put(
        "/api/projects/demo", json={"name": "Demo", "description": "d"}, headers=ADMIN
    )
    acc = client.post(
        "/api/accounts",
        json={
            "name": "claude-1",
            "provider": "anthropic",
            "kind": "api_key",
            "priority": 10,
            "credential": {"api_key": "sk-ant-api03-fake"},
        },
        headers=ADMIN,
    )
    assert acc.status_code == 201, acc.text
    client.put(
        "/api/settings/review.independent",
        json={"value": {"mode": "different_model", "max_rounds": 3}},
        headers=ADMIN,
    )
    client.put(
        "/api/models",
        json={
            "harness": {"claude": ["claude-opus-5-5", "claude-sonnet-5-5"]},
            "implementation": {"claude": ["claude-opus-5-5"]},
        },
        headers=ADMIN,
    )
    client.post(
        "/api/tasks",
        json={
            "title": "Ship feature",
            "project": "demo",
            "spec": "implement",
            "acceptance": ["done"],
        },
        headers=ADMIN,
    )
    boot = client.post(
        "/api/agents/bootstrap-tokens", json={"role": "worker"}, headers=ADMIN
    ).json()
    agent = client.post(
        "/api/agents/register",
        json={"bootstrap_token": boot["token"], "capabilities": []},
    ).json()
    headers = bearer(agent["token"])
    claimed = client.post(
        "/api/tasks/claim", json={"capabilities": [], "wait_s": 0}, headers=headers
    ).json()
    client.post(
        f"/api/tasks/{claimed['id']}/models",
        json={
            "attempt": claimed["attempt"],
            "executions": [
                {
                    "key": "h1",
                    "layer": "harness",
                    "state": "succeeded",
                    "engine": "claude",
                    "provider": "anthropic",
                    "configured_model": "claude-sonnet-5-5",
                    "observed_model": "claude-sonnet-5-5",
                    "evidence": "cli_output",
                }
            ],
        },
        headers=headers,
    )
    res = client.post(
        f"/api/tasks/{claimed['id']}/complete",
        json={
            "attempt": claimed["attempt"],
            "result": {
                "summary": "shipped",
                "data": {
                    "pr": "https://git.example/o/r/pulls/1",
                    "head_sha": "abcdef1",
                },
                "checklist": [
                    {
                        "id": item_id("done"),
                        "status": "done",
                        "evidence": "see PR",
                    }
                ],
            },
        },
        headers=headers,
    )
    assert res.status_code == 200, res.text
    parent = res.json()
    assert parent["state"] == "in_review", parent
    children = client.get(
        "/api/tasks",
        params={"project": "demo", "kind": "code_review"},
        headers=ADMIN,
    ).json()
    kids = [t for t in children["items"] if t.get("parent_id") == claimed["id"]]
    assert len(kids) == 1, children
    assert kids[0]["kind"] == "code_review"
    detail = client.get(f"/api/tasks/{kids[0]['id']}", headers=ADMIN).json()
    assert detail["harness_model"]["model"] == "claude-opus-5-5"


def test_independent_review_skips_without_delivery(client: TestClient) -> None:
    client.put(
        "/api/settings/review.independent",
        json={"value": {"mode": "different_model", "max_rounds": 3}},
        headers=ADMIN,
    )
    client.post(
        "/api/tasks",
        json={"title": "Answer a question", "project": "global", "spec": "why?"},
        headers=ADMIN,
    )
    boot = client.post(
        "/api/agents/bootstrap-tokens", json={"role": "worker"}, headers=ADMIN
    ).json()
    agent = client.post(
        "/api/agents/register",
        json={"bootstrap_token": boot["token"], "capabilities": []},
    ).json()
    headers = bearer(agent["token"])
    claimed = client.post(
        "/api/tasks/claim", json={"capabilities": [], "wait_s": 0}, headers=headers
    ).json()
    res = client.post(
        f"/api/tasks/{claimed['id']}/complete",
        json={
            "attempt": claimed["attempt"],
            "result": {"summary": "because"},
        },
        headers=headers,
    )
    assert res.status_code == 200, res.text
    assert res.json()["state"] == "succeeded"
    events = client.get(
        "/api/events",
        params={"kind": "review.independent_skipped"},
        headers=ADMIN,
    ).json()
    rows = events["items"] if isinstance(events, dict) else events
    assert any(e["subject_id"] == claimed["id"] for e in rows), events


def test_independent_review_blocks_when_delivery_has_no_head_sha(
    client: TestClient,
) -> None:
    # A PR without a commit SHA cannot pin a reviewer's input (head_sha is required),
    # so the gate blocks visibly instead of failing completion with a 422.
    client.put(
        "/api/projects/demo", json={"name": "Demo", "description": "d"}, headers=ADMIN
    )
    acc = client.post(
        "/api/accounts",
        json={
            "name": "claude-1",
            "provider": "anthropic",
            "kind": "api_key",
            "priority": 10,
            "credential": {"api_key": "sk-ant-api03-fake"},
        },
        headers=ADMIN,
    )
    assert acc.status_code == 201, acc.text
    client.put(
        "/api/settings/review.independent",
        json={"value": {"mode": "different_model", "max_rounds": 3}},
        headers=ADMIN,
    )
    client.put(
        "/api/models",
        json={
            "harness": {"claude": ["claude-opus-5-5", "claude-sonnet-5-5"]},
            "implementation": {"claude": ["claude-opus-5-5"]},
        },
        headers=ADMIN,
    )
    client.post(
        "/api/tasks",
        json={
            "title": "Ship feature",
            "project": "demo",
            "spec": "implement",
            "acceptance": ["done"],
        },
        headers=ADMIN,
    )
    boot = client.post(
        "/api/agents/bootstrap-tokens", json={"role": "worker"}, headers=ADMIN
    ).json()
    agent = client.post(
        "/api/agents/register",
        json={"bootstrap_token": boot["token"], "capabilities": []},
    ).json()
    headers = bearer(agent["token"])
    claimed = client.post(
        "/api/tasks/claim", json={"capabilities": [], "wait_s": 0}, headers=headers
    ).json()
    client.post(
        f"/api/tasks/{claimed['id']}/models",
        json={
            "attempt": claimed["attempt"],
            "executions": [
                {
                    "key": "h1",
                    "layer": "harness",
                    "state": "succeeded",
                    "engine": "claude",
                    "provider": "anthropic",
                    "configured_model": "claude-sonnet-5-5",
                    "observed_model": "claude-sonnet-5-5",
                    "evidence": "cli_output",
                }
            ],
        },
        headers=headers,
    )
    res = client.post(
        f"/api/tasks/{claimed['id']}/complete",
        json={
            "attempt": claimed["attempt"],
            "result": {
                "summary": "shipped",
                "data": {"pr": "https://git.example/o/r/pulls/1"},
                "checklist": [
                    {
                        "id": item_id("done"),
                        "status": "done",
                        "evidence": "see PR",
                    }
                ],
            },
        },
        headers=headers,
    )
    assert res.status_code == 200, res.text
    parent = res.json()
    assert parent["state"] == "in_review", parent
    assert parent["block_reason"] == "no_review_head", parent
    children = client.get(
        "/api/tasks",
        params={"project": "demo", "kind": "code_review"},
        headers=ADMIN,
    ).json()
    assert not [t for t in children["items"] if t.get("parent_id") == claimed["id"]]
    events = client.get(
        "/api/events",
        params={"kind": "review.independent_blocked"},
        headers=ADMIN,
    ).json()
    rows = events["items"] if isinstance(events, dict) else events
    blocked = [e for e in rows if e["subject_id"] == claimed["id"]]
    assert blocked and blocked[0]["data"]["reason"] == "no_review_head", events
