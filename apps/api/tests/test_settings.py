"""Typed settings with per-project overrides (docs/specs/settings.spec.md)."""

import os
from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api import model_policy
from argus_api.config import Settings, refuse_preview_data, route_bind
from argus_api.errors import ValidationFailedError
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from argus_api.settings.service import effective_git_identity, resolve_git_identity
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
    # Clearing ARGUS_* would otherwise re-enable the disk guard that conftest turns off.
    monkeypatch.setenv("ARGUS_DISK_GUARD_ENABLED", "false")
    with TestClient(app) as c:
        app.state.secrets = MemorySecretStore()
        yield c


def test_registry_lists_phase1_keys(client: TestClient) -> None:
    body = client.get("/api/settings/registry", headers=ADMIN).json()
    keys = {d["key"] for d in body}
    assert "models" in keys
    assert "difficulty.executors" in keys
    assert "review.independent" in keys
    assert "onboarding.dismissed" in keys
    assert "orchestrator.enabled" in keys
    assert "orchestrator.daily_budget_usd" in keys
    review = next(d for d in body if d["key"] == "review.independent")
    assert review["default"] == {"mode": "off", "max_rounds": 3}
    assert "schema" in review
    assert set(review["scopes"]) == {"global", "project"}
    executors = next(d for d in body if d["key"] == "difficulty.executors")
    assert executors["default"] == {}
    assert set(executors["scopes"]) == {"global", "project"}


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


def test_global_only_key_is_scope_not_allowed_at_project_scope(
    client: TestClient,
) -> None:
    client.put(
        "/api/projects/demo",
        json={"name": "Demo", "description": "d"},
        headers=ADMIN,
    )
    put = client.put(
        "/api/projects/demo/settings/orchestrator.enabled",
        json={"value": True},
        headers=ADMIN,
    )
    assert put.status_code == 422, put.text
    assert put.json()["detail"]["code"] == "scope_not_allowed"

    reset = client.delete(
        "/api/projects/demo/settings/orchestrator.enabled", headers=ADMIN
    )
    assert reset.status_code == 422, reset.text
    assert reset.json()["detail"]["code"] == "scope_not_allowed"

    ok = client.delete("/api/projects/demo/settings/review.independent", headers=ADMIN)
    assert ok.status_code == 200, ok.text


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


def test_command_guard_enabled_setting(client: TestClient) -> None:
    client.put(
        "/api/projects/demo", json={"name": "Demo", "description": "d"}, headers=ADMIN
    )
    body = client.get("/api/settings/registry", headers=ADMIN).json()
    row = next(d for d in body if d["key"] == "command_guard.enabled")
    assert row["default"] is True
    assert row["schema"] == {"type": "boolean"}
    assert set(row["scopes"]) == {"global", "project"}

    for path in (
        "/api/settings/command_guard.enabled",
        "/api/projects/demo/settings/command_guard.enabled",
    ):
        for value in (False, True):
            res = client.put(path, json={"value": value}, headers=ADMIN)
            assert res.status_code == 200, res.text
            assert res.json()["value"] is value
        for bad in ("off", 0, None):
            res = client.put(path, json={"value": bad}, headers=ADMIN)
            assert res.status_code == 422, (bad, res.text)


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
    # Built-in standard chain when difficulty.executors is unset.
    assert body["difficulty_executors"] == ["claude", "codex"]


def test_difficulty_executors_setting_and_claim(client: TestClient) -> None:
    client.put(
        "/api/projects/demo", json={"name": "Demo", "description": "d"}, headers=ADMIN
    )
    res = client.put(
        "/api/settings/difficulty.executors",
        json={"value": {"trivial": [], "standard": ["codex", "claude"]}},
        headers=ADMIN,
    )
    assert res.status_code == 200, res.text
    assert res.json()["value"] == {"trivial": [], "standard": ["codex", "claude"]}
    assert res.json()["source"] == "global"

    got = client.get("/api/settings", headers=ADMIN).json()
    by_key = {s["key"]: s for s in got["settings"]}
    assert by_key["difficulty.executors"]["value"]["trivial"] == []

    task = client.post(
        "/api/tasks",
        json={
            "title": "copy tweak",
            "project": "demo",
            "spec": "Fix a typo in the README wording only.",
            "difficulty": "trivial",
        },
        headers=ADMIN,
    ).json()
    assert task["difficulty"] == "trivial"
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
    assert claimed.json()["difficulty_executors"] == []


@pytest.mark.parametrize(
    ("project", "global_", "expected"),
    [
        ({"name": "P", "email": "p@x.io"}, {"name": "G", "email": "g@x.io"}, "P"),
        ({}, {"name": "G", "email": "g@x.io"}, "G"),
        (None, {"name": "G", "email": "g@x.io"}, "G"),
        ({"name": "P", "email": "p@x.io"}, {}, "P"),
    ],
)
def test_git_identity_precedence(
    project: dict[str, str] | None, global_: dict[str, str], expected: str
) -> None:
    got = resolve_git_identity(project, global_)
    assert got.name == expected


def test_git_identity_unset_fails_closed_naming_the_key() -> None:
    with pytest.raises(ValidationFailedError, match=r"git\.identity"):
        resolve_git_identity({}, {})


def test_git_identity_setting_resolves_project_over_global(
    client: TestClient,
) -> None:
    client.put(
        "/api/projects/demo",
        json={"name": "Demo", "description": "d"},
        headers=ADMIN,
    )
    db = client.app.state.db  # ty: ignore[unresolved-attribute]
    path = Path(os.environ["ARGUS_DATA_DIR"])

    async def resolve() -> str:
        async with db.read() as conn:
            return (await effective_git_identity(conn, path, "demo")).name

    def put(url: str, value: dict[str, str]) -> None:
        res = client.put(url, json={"value": value}, headers=ADMIN)
        assert res.status_code == 200, res.text

    with pytest.raises(ValidationFailedError, match=r"git\.identity"):
        client.portal.call(resolve)  # ty: ignore[unresolved-attribute]
    put("/api/settings/git.identity", {"name": "G", "email": "g@x.io"})
    assert client.portal.call(resolve) == "G"  # ty: ignore[unresolved-attribute]
    put("/api/projects/demo/settings/git.identity", {"name": "P", "email": "p@x.io"})
    assert client.portal.call(resolve) == "P"  # ty: ignore[unresolved-attribute]


def test_git_identity_save_override_reset_and_claim_delivery(
    client: TestClient,
) -> None:
    for slug in ("alpha", "beta"):
        client.put(
            f"/api/projects/{slug}",
            json={"name": slug, "description": "d"},
            headers=ADMIN,
        )

    def source(url: str) -> tuple[str, object]:
        got = client.get(url, headers=ADMIN).json()
        row = next(s for s in got["settings"] if s["key"] == "git.identity")
        return row["source"], row["value"]

    def claim(project: str) -> dict[str, str] | None:
        client.post(
            "/api/tasks",
            json={"title": "t", "project": project, "spec": "do it"},
            headers=ADMIN,
        )
        boot = client.post(
            "/api/agents/bootstrap-tokens", json={"role": "worker"}, headers=ADMIN
        ).json()
        agent = client.post(
            "/api/agents/register",
            json={"bootstrap_token": boot["token"], "capabilities": []},
        ).json()
        res = client.post(
            "/api/tasks/claim",
            json={"capabilities": [], "wait_s": 0},
            headers=bearer(agent["token"]),
        )
        assert res.status_code == 200, res.text
        assert res.json()["project"] == project
        return res.json()["git_identity"]

    glob = {"name": "G", "email": "g@x.io"}
    over = {"name": "P", "email": "p@x.io"}
    # Neither scope set: fail closed, nothing delivered.
    assert source("/api/projects/alpha/settings") == ("default", {})
    assert claim("alpha") is None

    res = client.put("/api/settings/git.identity", json={"value": glob}, headers=ADMIN)
    assert res.status_code == 200, res.text
    assert source("/api/settings") == ("global", glob)
    assert source("/api/projects/alpha/settings") == ("global", glob)
    assert claim("alpha") == glob

    res = client.put(
        "/api/projects/alpha/settings/git.identity",
        json={"value": over},
        headers=ADMIN,
    )
    assert res.status_code == 200, res.text
    assert source("/api/projects/alpha/settings") == ("project", over)
    assert claim("alpha") == over
    # The override reaches only its own project and leaves global alone.
    assert source("/api/projects/beta/settings") == ("global", glob)
    assert source("/api/settings") == ("global", glob)
    assert claim("beta") == glob

    res = client.delete("/api/projects/alpha/settings/git.identity", headers=ADMIN)
    assert res.status_code == 200, res.text
    assert source("/api/projects/alpha/settings") == ("global", glob)
    assert source("/api/settings") == ("global", glob)
    assert claim("alpha") == glob


def test_command_guard_enabled_claim_delivery(client: TestClient) -> None:
    for slug in ("alpha", "beta"):
        client.put(
            f"/api/projects/{slug}",
            json={"name": slug, "description": "d"},
            headers=ADMIN,
        )

    def claim(project: str) -> bool:
        client.post(
            "/api/tasks",
            json={"title": "t", "project": project, "spec": "do it"},
            headers=ADMIN,
        )
        boot = client.post(
            "/api/agents/bootstrap-tokens", json={"role": "worker"}, headers=ADMIN
        ).json()
        agent = client.post(
            "/api/agents/register",
            json={"bootstrap_token": boot["token"], "capabilities": []},
        ).json()
        res = client.post(
            "/api/tasks/claim",
            json={"capabilities": [], "wait_s": 0},
            headers=bearer(agent["token"]),
        )
        assert res.status_code == 200, res.text
        assert res.json()["project"] == project
        return res.json()["command_guard_enabled"]

    def put(url: str, value: bool) -> None:
        res = client.put(url, json={"value": value}, headers=ADMIN)
        assert res.status_code == 200, res.text

    assert claim("alpha") is True  # default on
    put("/api/settings/command_guard.enabled", False)
    assert claim("alpha") is False
    put("/api/projects/alpha/settings/command_guard.enabled", True)
    assert claim("alpha") is True  # project override beats global
    assert claim("beta") is False
    res = client.delete(
        "/api/projects/alpha/settings/command_guard.enabled", headers=ADMIN
    )
    assert res.status_code == 200, res.text
    assert claim("alpha") is False


@pytest.mark.parametrize(
    "value",
    [
        {"name": "", "email": "a@x.io"},
        {"name": "A", "email": "not an address"},
        {"name": "A", "email": "nope"},
        {"name": "A"},
        {"name": "A", "email": "a@x.io", "extra": 1},
    ],
)
def test_git_identity_schema_rejects_bad_shapes(
    client: TestClient, value: dict[str, object]
) -> None:
    res = client.put("/api/settings/git.identity", json={"value": value}, headers=ADMIN)
    assert res.status_code == 422, res.text


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
        json={"title": "Answer a question", "project": "demo", "spec": "why?"},
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


def test_preview_data_dir_is_refused_outside_the_preview_checkout(
    tmp_path: Path,
) -> None:
    preview = tmp_path / "work" / "argus"
    (preview / "data").mkdir(parents=True)
    worktree = tmp_path / "worktree"
    with pytest.raises(ValueError, match="preview install"):
        refuse_preview_data(preview / "data", worktree, preview)
    with pytest.raises(ValueError, match="preview install"):  # via a symlink
        link = tmp_path / "link"
        link.symlink_to(preview / "data")
        refuse_preview_data(link, worktree, preview)
    refuse_preview_data(preview / "data", preview, preview)  # the service itself
    refuse_preview_data(worktree / ".argus", worktree, preview)


@pytest.mark.parametrize("value", ["172.30.0.5", "10.0.0.1", "if:eth0", "if:br-1a2b"])
def test_route_bind_accepts_an_address_or_an_interface(value: str) -> None:
    assert route_bind(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "0.0.0.0",
        "0.1.2.3",
        "127.0.0.1",
        "169.254.1.1",
        "224.0.0.1",
        "240.0.0.1",
        "255.255.255.255",
        "::",
        "fe80::1",
        "2001:db8::1",
        "host.docker.internal",
        "172.030.0.5",
        "172.30.0.5:8080",
        "if:",
        "if:../eth0",
        "if:eth0\n",
        "if:sixteen-chars-xx",
    ],
)
def test_route_bind_refuses_what_the_channel_refuses(value: str) -> None:
    with pytest.raises(ValueError):
        route_bind(value)


def test_route_bind_env_is_optional_and_refused_at_startup_when_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "ARGUS_NANOCLAW_INTEGRATION_ROUTE_BIND"
    monkeypatch.setenv(name, " ")
    assert Settings.from_env().nanoclaw_integration_route_bind is None
    monkeypatch.setenv(name, "if:br-s")
    assert Settings.from_env().nanoclaw_integration_route_bind == "if:br-s"
    monkeypatch.setenv(name, "0.0.0.0")
    with pytest.raises(ValueError, match=name):
        Settings.from_env()
