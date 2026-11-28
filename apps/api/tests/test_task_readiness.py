"""The merge and review decisions on the task read (docs/specs/worker-lifecycle.spec.md 3.7).

`decide_merge` and `forge_review_policy` run the cases shared with the TypeScript
helpers (integrations/nanoclaw/fixtures/merge-decision/cases.json); the REST tests check
the read carries what the settings resolve and what the delivery snapshot shows."""

import json
from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api.main import app
from argus_api.tasks import readiness
from argus_core.settings import SettingValue
from argus_core.tasks import Deliverable, PromptTrust, Task
from fastapi.testclient import TestClient
from test_forge_review_policy import FORGE
from test_mcp import ADMIN_TOKEN
from test_task_deliverable import create
from test_task_delivery import (
    HEAD,
    URL,
    forge,
    full_forge,
    open_pr,
    review_task,
    setup_repository,
)

CASES = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "integrations"
        / "nanoclaw"
        / "fixtures"
        / "merge-decision"
        / "cases.json"
    ).read_text()
)
AUTO_MERGE = "/api/settings/delivery.auto_merge"
PROJECT_AUTO_MERGE = "/api/projects/demo/settings/delivery.auto_merge"


@pytest.mark.parametrize("case", CASES["merge"], ids=lambda c: c["name"])
def test_merge_decision_matches_the_typescript_cases(case: dict) -> None:
    fields = {
        "deliverable": "response",
        "prompt_trust": "orchestrator",
        "required_capabilities": ["repository:write"],
        "input": {},
        **{k: v for k, v in case["task"].items() if k != "auto_merge"},
    }
    task = Task.model_construct(
        **{
            **fields,
            "deliverable": Deliverable(fields["deliverable"]),
            "prompt_trust": PromptTrust(fields["prompt_trust"]),
        }
    )
    setting = SettingValue(
        key="delivery.auto_merge",
        value=case["task"].get("auto_merge", False),
        source="global",
    )
    got = readiness.decide_merge(task, setting)
    assert (got.authorized, got.may_merge) == (case["authorized"], case["may_merge"])


@pytest.mark.parametrize("case", CASES["review"], ids=lambda c: c["name"])
def test_review_policy_matches_the_typescript_cases(case: dict) -> None:
    got = readiness.forge_review_policy(case["forge_agentic_review_required"])
    assert got == case["policy"]


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        setup_repository(c)
        yield c


def read(client: TestClient, level: str, **extra) -> dict:
    task_id = create(client, level, **extra).json()["id"]
    res = client.get(f"/api/tasks/{task_id}")
    assert res.status_code == 200, res.text
    return res.json()


def test_pr_green_task_may_not_merge(client: TestClient) -> None:
    got = read(client, "pr_green")
    assert got["merge"]["authorized"] is False
    assert got["merge"]["may_merge"] is False
    assert got["merge"]["deliverable"] == "pr_green"
    assert got["merge"]["granted_by"] is None
    assert got["pr_gate"] is None


def test_pr_green_task_with_allow_merge_input_may_merge(client: TestClient) -> None:
    got = read(client, "pr_green", input={"delivery": {"allow_merge": True}})
    assert got["merge"]["allow_merge"] is True
    assert got["merge"]["granted_by"] == "allow_merge"
    assert got["merge"]["may_merge"] is True


def test_pr_merged_task_may_merge_under_global_auto_merge(client: TestClient) -> None:
    assert read(client, "pr_merged")["merge"]["may_merge"] is False
    assert client.put(AUTO_MERGE, json={"value": True}).status_code == 200
    got = read(client, "pr_merged")["merge"]
    assert (got["authorized"], got["may_merge"]) == (True, True)
    assert (got["auto_merge"], got["auto_merge_source"]) == (True, "global")
    assert got["granted_by"] == "auto_merge"


def test_project_override_changes_the_answer(client: TestClient) -> None:
    assert client.put(AUTO_MERGE, json={"value": True}).status_code == 200
    assert client.put(PROJECT_AUTO_MERGE, json={"value": False}).status_code == 200
    got = read(client, "pr_merged")["merge"]
    assert (got["authorized"], got["auto_merge"]) == (False, False)
    assert got["auto_merge_source"] == "project"
    assert client.put(AUTO_MERGE, json={"value": False}).status_code == 200
    assert client.put(PROJECT_AUTO_MERGE, json={"value": True}).status_code == 200
    got = read(client, "pr_merged")["merge"]
    assert (got["authorized"], got["auto_merge_source"]) == (True, "project")


def test_review_off_versus_required_with_a_bound_pr_without_approvals(
    client: TestClient,
) -> None:
    forge(
        client,
        full_forge(open_pr(), statuses=[{"context": "ci", "state": "success"}]),
    )
    task_id = review_task(client, f"see {URL}")
    off = client.get(f"/api/tasks/{task_id}").json()
    assert off["forge_review"] == "off"
    assert off["pr_gate"]["head_sha"] == HEAD
    assert off["pr_gate"]["reasons"] == []
    assert off["pr_gate"]["ready"] is True

    assert client.put(FORGE, json={"value": True}).status_code == 200
    required = client.get(f"/api/tasks/{task_id}").json()
    assert required["forge_review"] == "required"
    assert required["pr_gate"]["reasons"] == ["review_required"]
    assert required["pr_gate"]["approvals_for_head"] == 0
    assert required["pr_gate"]["ready"] is False


def test_failing_ci_is_a_gate_reason(client: TestClient) -> None:
    forge(
        client,
        full_forge(open_pr(), statuses=[{"context": "ci", "state": "failure"}]),
    )
    task_id = review_task(client, f"see {URL}")
    gate = client.get(f"/api/tasks/{task_id}").json()["pr_gate"]
    assert gate["reasons"] == ["ci_failed"]


@pytest.mark.parametrize("setting", [AUTO_MERGE, PROJECT_AUTO_MERGE])
@pytest.mark.parametrize("allow", [True, False])
def test_owner_denial_vetoes_settings_and_conflicting_allow(client, setting, allow):
    assert client.put(setting, json={"value": True}).status_code == 200
    got = read(
        client,
        "pr_merged",
        input={
            "delivery": {
                "deny_merge": True,
                "allow_merge": allow,
            }
        },
    )
    assert got["input"]["delivery"]["deny_merge"] is True
    decision = got["merge"]
    assert decision["auto_merge"] is True
    assert decision["deny_merge"] is True
    assert decision["authorized"] is False
    assert decision["may_merge"] is False
    assert decision["granted_by"] is None


def test_non_boolean_denial_is_rejected_at_authoring(client: TestClient) -> None:
    res = create(client, "pr_merged", input={"delivery": {"deny_merge": "true"}})
    assert res.status_code == 422
