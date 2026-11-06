"""Task defaults (docs/specs/task-defaults.spec.md): the settings, their precedence
at every creation entry point, the creation snapshot and the effective read."""

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from argus_api.main import app
from argus_api.tasks import defaults
from argus_core.kinds import TaskKind
from argus_core.settings import SettingValue
from argus_core.tasks import TaskCreate
from fastapi.testclient import TestClient
from test_mcp import ADMIN_TOKEN, register, tool
from test_task_delivery import HOST, OWNER, REPO, setup_repository

WRITE = ["repository:write"]
REPOSITORY_URL = f"https://{HOST}/{OWNER}/{REPO}"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        setup_repository(c)
        for slug, extra in (
            ("argus", {"repository_url": REPOSITORY_URL}),
            ("other", {"repository_url": REPOSITORY_URL}),
            ("bare", {}),
        ):
            res = c.put(f"/api/projects/{slug}", json={"name": slug, **extra})
            assert res.status_code == 200, res.text
        yield c


def put(client: TestClient, key: str, value, project: str | None = None) -> None:
    path = (
        f"/api/projects/{project}/settings/{key}" if project else f"/api/settings/{key}"
    )
    res = client.put(path, json={"value": value})
    assert res.status_code == 200, res.text


def reset(client: TestClient, key: str, project: str) -> None:
    res = client.delete(f"/api/projects/{project}/settings/{key}")
    assert res.status_code in (200, 204), res.text


def create(client: TestClient, project: str = "argus", **body) -> dict:
    res = client.post("/api/tasks", json={"project": project, "title": "t", **body})
    assert res.status_code == 201, res.text
    return res.json()


def refused(client: TestClient, project: str = "argus", **body) -> dict:
    res = client.post("/api/tasks", json={"project": project, "title": "t", **body})
    assert res.status_code == 422, res.text
    return res.json()["detail"]


def effective(client: TestClient, project: str, **params) -> dict:
    res = client.get(f"/api/projects/{project}/task-defaults", params=params)
    assert res.status_code == 200, res.text
    return res.json()


def owner_defaults(client: TestClient) -> None:
    put(client, "task_default_deliverable", "pr_merged", "argus")
    put(client, "task_default_required_capabilities", WRITE, "argus")


# -- the owner's example -----------------------------------------------------------


def test_project_default_deliverable_applies_when_omitted(client: TestClient) -> None:
    owner_defaults(client)
    task = create(client)
    assert (task["deliverable"], task["required_capabilities"]) == ("pr_merged", WRITE)


def test_explicit_deliverable_wins_over_the_project_default(
    client: TestClient,
) -> None:
    owner_defaults(client)
    assert create(client, deliverable="response")["deliverable"] == "response"


def test_another_project_is_unaffected(client: TestClient) -> None:
    owner_defaults(client)
    task = create(client, "other")
    assert (task["deliverable"], task["required_capabilities"]) == ("response", [])


@pytest.mark.parametrize(
    "level", ["knowledge", "artifact", "subtasks", "branch", "pr_open", "pr_green"]
)
def test_any_deliverable_can_be_the_default(client: TestClient, level: str) -> None:
    put(client, "task_default_deliverable", level, "other")
    put(client, "task_default_required_capabilities", WRITE, "other")
    assert create(client, "other")["deliverable"] == level


# -- settings ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("task_default_deliverable", "merged"),
        ("task_default_deliverable", None),
        ("task_default_priority", 1.5),
        ("task_default_priority", None),
        ("task_default_difficulty", "extreme"),
        ("task_default_required_capabilities", [""]),
        ("task_default_required_capabilities", ["a", "a"]),
        ("task_default_required_capabilities", None),
        ("task_default_max_attempts", 0),
        ("task_default_max_attempts", 21),
        ("task_default_timeout_s", 59),
        ("task_default_budget_usd", 0),
    ],
)
def test_invalid_values_are_refused(client: TestClient, key: str, value) -> None:
    for path in (f"/api/settings/{key}", f"/api/projects/argus/settings/{key}"):
        res = client.put(path, json={"value": value})
        assert res.status_code == 422, (path, res.text)


def test_writes_are_admin_only_and_reads_are_open(client: TestClient) -> None:
    orchestrator = {"Authorization": f"Bearer {register(client, 'orchestrator')}"}
    for path in (
        "/api/settings/task_default_priority",
        "/api/projects/argus/settings/task_default_priority",
    ):
        assert (
            client.put(path, json={"value": 3}, headers=orchestrator).status_code == 403
        )
    res = client.get("/api/projects/argus/task-defaults", headers=orchestrator)
    assert res.status_code == 200, res.text
    worker = {"Authorization": f"Bearer {register(client)}"}
    # A worker reads only projects of tasks it has leased.
    res = client.get("/api/projects/argus/task-defaults", headers=worker)
    assert res.status_code == 403, res.text


# -- precedence ----------------------------------------------------------------------


def test_built_in_values_without_settings(client: TestClient) -> None:
    got = effective(client, "argus")
    assert got["values"] == {
        "deliverable": "response",
        "priority": 0,
        "difficulty": None,
        "required_capabilities": [],
        "budget_usd": None,
        "max_attempts": 3,
        "timeout_s": None,
    }
    assert set(got["sources"].values()) == {"built_in", "inferred"}
    assert got["sources"]["difficulty"] == "inferred"
    assert got["problems"] == []


def test_partial_project_override_inherits_the_rest_from_global(
    client: TestClient,
) -> None:
    put(client, "task_default_priority", 5)
    put(client, "task_default_max_attempts", 7)
    put(client, "task_default_max_attempts", 2, "argus")
    got = effective(client, "argus")
    assert (got["values"]["priority"], got["sources"]["priority"]) == (5, "global")
    assert (got["values"]["max_attempts"], got["sources"]["max_attempts"]) == (
        2,
        "project",
    )
    task = create(client)
    assert (task["priority"], task["max_attempts"]) == (5, 2)
    reset(client, "task_default_max_attempts", "argus")  # back to inherit
    assert create(client)["max_attempts"] == 7
    assert effective(client, "argus")["sources"]["max_attempts"] == "global"


def test_explicit_zero_empty_and_null_differ_from_omission(client: TestClient) -> None:
    put(client, "task_default_priority", 7)
    put(client, "task_default_required_capabilities", ["gpu"])
    put(client, "task_default_budget_usd", 2.5)
    omitted = create(client)
    assert (
        omitted["priority"],
        omitted["required_capabilities"],
        omitted["budget_usd"],
    ) == (7, ["gpu"], 2.5)
    explicit = create(client, priority=0, required_capabilities=[], budget_usd=None)
    assert (
        explicit["priority"],
        explicit["required_capabilities"],
        explicit["budget_usd"],
    ) == (0, [], None)
    # A null that means "omitted" for these fields takes the default.
    nulls = create(client, priority=None, required_capabilities=None, max_attempts=None)
    assert (nulls["priority"], nulls["required_capabilities"]) == (7, ["gpu"])


def test_stored_override_values_are_not_inherit(client: TestClient) -> None:
    put(client, "task_default_priority", 7)
    put(client, "task_default_required_capabilities", ["gpu"])
    put(client, "task_default_budget_usd", 2.5)
    put(client, "task_default_priority", 0, "argus")
    put(client, "task_default_required_capabilities", [], "argus")
    put(client, "task_default_budget_usd", None, "argus")  # no budget here
    task = create(client)
    assert (task["priority"], task["required_capabilities"], task["budget_usd"]) == (
        0,
        [],
        None,
    )
    assert effective(client, "argus")["sources"]["budget_usd"] == "project"
    assert create(client, "other")["budget_usd"] == 2.5  # the existing key, global


def test_difficulty_is_inferred_unless_a_default_or_the_task_names_one(
    client: TestClient,
) -> None:
    inferred = create(client, title="Fix typo in README")
    assert inferred["difficulty_source"] == "inferred"
    put(client, "task_default_difficulty", "hard")
    task = create(client, title="Fix typo in README")
    assert (task["difficulty"], task["difficulty_source"]) == ("hard", "declared")
    assert create(client, difficulty="simple")["difficulty"] == "simple"
    # An explicit null, or a stored null in the project, infers again.
    nulled = create(client, title="Fix typo in README", difficulty=None)
    assert nulled["difficulty_source"] == "inferred"
    put(client, "task_default_difficulty", None, "argus")
    assert effective(client, "argus")["sources"]["difficulty"] == "inferred"
    assert create(client, title="Fix typo in README")["difficulty_source"] == "inferred"


def test_timeout_default_and_fallback(client: TestClient) -> None:
    put(client, "task_default_timeout_s", 600)
    assert create(client)["timeout_s"] == 600
    assert create(client, timeout_s=None)["timeout_s"] is None  # the server's
    put(client, "task_default_timeout_s", None, "argus")
    assert create(client)["timeout_s"] is None


def _kind(**kw) -> TaskKind:
    now = datetime.now(UTC)
    return TaskKind.model_validate(
        {
            "name": "k",
            "description": "",
            "input_schema": {},
            "default_capabilities": None,
            "default_lease_ttl_s": None,
            "default_max_attempts": None,
            "default_timeout_s": None,
            "default_review": None,
            "default_knowledge": [],
            "created_at": now,
            "updated_at": now,
        }
        | kw
    )


def _settings(**values) -> dict[str, SettingValue]:
    out = {
        defaults.setting_key(f): SettingValue(
            key=defaults.setting_key(f), value=None, source="default"
        )
        for f in defaults.FIELDS
    }
    for field, (value, source) in values.items():
        key = defaults.setting_key(field)
        out[key] = SettingValue(key=key, value=value, source=source)
    return out


def test_kind_defaults_rank_below_settings_and_above_built_in() -> None:
    kind = _kind(
        default_capabilities=["gpu"], default_max_attempts=5, default_timeout_s=900
    )
    body = TaskCreate(title="t", project="p")
    got, sources = defaults.resolve(body, kind, _settings())
    assert (got.required_capabilities, got.max_attempts, got.timeout_s) == (
        ["gpu"],
        5,
        900,
    )
    assert {sources[f] for f in ("required_capabilities", "timeout_s")} == {"kind"}
    got, sources = defaults.resolve(
        body,
        kind,
        _settings(timeout_s=(1200, "global"), max_attempts=(2, "project")),
    )
    assert (got.timeout_s, got.max_attempts) == (1200, 2)
    # A stored null timeout, and an explicit null one, fall back to the kind.
    got, _ = defaults.resolve(body, kind, _settings(timeout_s=(None, "project")))
    assert got.timeout_s == 900
    got, _ = defaults.resolve(
        TaskCreate(title="t", project="p", timeout_s=None),
        kind,
        _settings(timeout_s=(1200, "global")),
    )
    assert got.timeout_s == 900


# -- code deliverables ---------------------------------------------------------------


def test_default_code_deliverable_without_capability_is_explained(
    client: TestClient,
) -> None:
    put(client, "task_default_deliverable", "pr_merged", "argus")
    detail = refused(client)
    assert detail["code"] == "deliverable_capability"
    joined = " ".join(detail["details"])
    assert "task_default_deliverable at project argus scope" in joined
    assert "task_default_required_capabilities" in joined
    got = effective(client, "argus")
    assert [(p["code"], p["field"]) for p in got["problems"]] == [
        ("deliverable_capability", "required_capabilities")
    ]
    # Never granted silently; the creator may still name a deliverable.
    assert create(client, deliverable="response")["deliverable"] == "response"


def test_explicit_empty_capabilities_against_a_code_default_are_refused(
    client: TestClient,
) -> None:
    owner_defaults(client)
    detail = refused(client, required_capabilities=[])
    assert detail["code"] == "deliverable_capability"
    assert any("task_default_deliverable" in d for d in detail["details"])


def test_default_code_deliverable_needs_a_repository(client: TestClient) -> None:
    put(client, "task_default_deliverable", "branch")
    put(client, "task_default_required_capabilities", WRITE)
    detail = refused(client, "bare")
    assert detail["code"] == "deliverable_repository"
    assert any("at global scope" in d for d in detail["details"])
    assert effective(client, "bare")["problems"][0]["code"] == "deliverable_repository"
    assert create(client, "argus")["deliverable"] == "branch"


def test_explicit_code_deliverable_errors_carry_no_settings_hint(
    client: TestClient,
) -> None:
    detail = refused(client, deliverable="pr_open")
    assert detail["code"] == "deliverable_capability"
    assert not any("task_default" in d for d in detail["details"])


# -- entry points --------------------------------------------------------------------


def test_mcp_create_task_matches_rest(client: TestClient) -> None:
    owner_defaults(client)
    put(client, "task_default_priority", 9, "argus")
    got = tool(client, ADMIN_TOKEN, "create_task", title="t", project="argus")
    rest = create(client)
    for field in ("deliverable", "required_capabilities", "priority"):
        assert got[field] == rest[field], field
    assert got["priority"] == 9
    explicit = tool(
        client, ADMIN_TOKEN, "create_task", title="t", project="argus", priority=0
    )
    assert explicit["priority"] == 0


def test_subtask_takes_its_parents_project_defaults(client: TestClient) -> None:
    owner_defaults(client)
    parent = create(client, deliverable="response")
    res = client.post("/api/tasks", json={"title": "child", "parent_id": parent["id"]})
    assert res.status_code == 201, res.text
    child = res.json()
    assert (child["project"], child["deliverable"]) == ("argus", "pr_merged")


def _draft(client: TestClient, **body) -> dict:
    res = client.post(
        "/api/task-drafts",
        json={"title": "d", "spec": "s", "acceptance": ["a"], **body},
    )
    assert res.status_code == 201, res.text
    return res.json()


def _publish(client: TestClient, key: str, *drafts: dict) -> dict:
    res = client.post(
        "/api/task-drafts/publish",
        json={
            "idempotency_key": key,
            "drafts": [
                {"draft_id": d["id"], "expected_version": d["version"]} for d in drafts
            ],
        },
    )
    assert res.status_code == 200, res.text
    return res.json()


def test_drafts_take_the_defaults_of_their_project_at_publish(
    client: TestClient,
) -> None:
    owner_defaults(client)
    pool = {"image": "w:1", "capabilities": WRITE, "max_replicas": 0}
    assert client.put("/api/pools/writers", json=pool).status_code in (200, 201)
    draft = _draft(client, project="other")
    assert "deliverable" not in draft["body"] or draft["body"]["deliverable"] is None
    moved = client.patch(
        f"/api/task-drafts/{draft['id']}",
        json={"expected_version": draft["version"], "changes": {"project": "argus"}},
    ).json()
    pinned = _draft(client, project="argus", deliverable="response")
    out = _publish(client, "p1", moved, pinned)
    ids = [i["task_id"] for i in out["items"]]
    got = [client.get(f"/api/tasks/{i}").json()["deliverable"] for i in ids]
    assert got == ["pr_merged", "response"]
    # A replay after a settings change returns the stored answer, creating nothing.
    put(client, "task_default_deliverable", "response", "argus")
    again = _publish(client, "p1", moved, pinned)
    assert again["replayed"] is True
    assert [i["task_id"] for i in again["items"]] == ids
    assert client.get(f"/api/tasks/{ids[0]}").json()["deliverable"] == "pr_merged"


def test_routine_runs_keep_template_fields_and_take_defaults_for_the_rest(
    client: TestClient,
) -> None:
    owner_defaults(client)
    put(client, "task_default_priority", 4, "argus")
    template = {"spec": "Read the logs.", "deliverable": "response", "max_attempts": 1}
    res = client.put(
        "/api/projects/argus/routines/logs",
        json={"title": "Logs", "schedule": "@daily", "template": template},
    )
    assert res.status_code in (200, 201), res.text
    run = client.post(f"/api/routines/{res.json()['id']}/run").json()
    task = client.get(f"/api/tasks/{run['task_id']}").json()
    assert (task["deliverable"], task["max_attempts"], task["priority"]) == (
        "response",
        1,
        4,
    )


# -- snapshot and idempotency --------------------------------------------------------


def test_settings_changes_do_not_rewrite_existing_tasks(client: TestClient) -> None:
    owner_defaults(client)
    task = create(client)
    put(client, "task_default_deliverable", "response", "argus")
    assert client.get(f"/api/tasks/{task['id']}").json()["deliverable"] == "pr_merged"
    events = client.get(
        "/api/events", params={"subject_type": "task", "subject_id": task["id"]}
    ).json()
    [created] = [e for e in events if e["kind"] == "task.created"]
    assert created["data"]["defaults"]["deliverable"] == "project"
    assert "title" not in created["data"]["defaults"]


def test_idempotent_replay_returns_the_same_task_after_a_settings_change(
    client: TestClient,
) -> None:
    owner_defaults(client)
    key = {"Idempotency-Key": "c1"}
    body = {"project": "argus", "title": "t"}
    first = client.post("/api/tasks", json=body, headers=key)
    assert first.status_code == 201, first.text
    put(client, "task_default_deliverable", "response", "argus")
    again = client.post("/api/tasks", json=body, headers=key)
    assert again.status_code in (200, 201), again.text
    assert (again.json()["id"], again.json()["deliverable"]) == (
        first.json()["id"],
        "pr_merged",
    )
    # An explicit null budget is another request than an omitted one.
    other = client.post("/api/tasks", json=body | {"budget_usd": None}, headers=key)
    assert other.status_code == 409, other.text
