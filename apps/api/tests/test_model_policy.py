"""Preferred models API (docs/specs/model-selection.spec.md)."""

import json
import os
from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api import model_policy
from argus_api.main import app
from fastapi.testclient import TestClient
from test_launch import ADMIN, agent_token, bearer

FIXTURE = (
    Path(model_policy.__file__).resolve().parents[4]
    / "integrations/nanoclaw/engines/model-policy.fixture.json"
)
POLICY = {
    "harness": {"codex": ["luna", "sol"], "claude": ["sonnet"]},
    "implementation": {"codex": ["sol-6.1"]},
}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    for name in list(os.environ):
        if name.startswith(("ARGUS_", "DOCKER_HOST")) and not name.startswith(
            "ARGUS_TEST_"
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN["Authorization"].split()[1])
    with TestClient(app) as c:
        yield c


def file_of(client: TestClient) -> Path:
    return Path(client.get("/api/models", headers=ADMIN).json()["path"])


def test_revision_matches_the_fixture_the_node_tests_read() -> None:
    cases = json.loads(FIXTURE.read_text())
    assert len(cases) >= 3
    for c in cases:
        assert model_policy.revision(c["policy"]) == c["revision"], c["name"]
        canonical = json.dumps(
            model_policy.validate(c["policy"]), separators=(",", ":")
        )
        assert canonical == c["canonical"], c["name"]


def test_defaults_are_the_shipped_policy(client: TestClient) -> None:
    body = client.get("/api/models", headers=ADMIN).json()
    assert body["source"] == "defaults"
    assert body["env_overrides"] is False
    assert body["file_revision"] is None
    assert body["notice"] is None
    assert body["path"].endswith("model-policy.json")
    assert body["revision"] == json.loads(FIXTURE.read_text())[0]["revision"]
    assert body["implementation"]["codex"] == ["gpt-6.1-sol"]


def test_any_token_reads_but_only_admin_writes(client: TestClient) -> None:
    worker = bearer(agent_token(client, "worker"))
    assert client.get("/api/models", headers=worker).status_code == 200
    assert client.put("/api/models", json=POLICY, headers=worker).status_code == 403
    assert client.delete("/api/models", headers=worker).status_code == 403
    assert client.get("/api/models").status_code == 401
    assert not file_of(client).exists()


def test_put_writes_the_file_and_changes_the_revision(client: TestClient) -> None:
    before = client.get("/api/models", headers=ADMIN).json()
    res = client.put("/api/models", json=POLICY, headers=ADMIN)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["source"] == "file"
    assert body["revision"] != before["revision"]
    assert body["revision"] == model_policy.revision(POLICY)
    assert body["file_revision"] == body["revision"]
    assert json.loads(file_of(client).read_text()) == model_policy.validate(POLICY)
    assert client.get("/api/models", headers=ADMIN).json() == body
    kinds = client.get(
        "/api/events", params={"kind": "models.policy_saved"}, headers=ADMIN
    ).json()
    assert "revision" in json.dumps(kinds)


def test_env_wins_over_the_file_and_says_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.put("/api/models", json=POLICY, headers=ADMIN)
    env = {"harness": {"claude": ["from-env"]}, "implementation": {}}
    monkeypatch.setenv("ARGUS_MODEL_POLICY", json.dumps(env))
    body = client.get("/api/models", headers=ADMIN).json()
    assert body["source"] == "env"
    assert body["env_overrides"] is True
    assert body["harness"] == {"claude": ["from-env"]}
    assert body["revision"] == model_policy.revision(env)
    assert body["file_revision"] == model_policy.revision(POLICY)
    assert "wins over the file" in body["notice"]
    saved = client.put("/api/models", json=POLICY, headers=ADMIN).json()
    assert saved["source"] == "env"
    assert saved["notice"]


def test_malformed_env_fails_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARGUS_MODEL_POLICY", "not json")
    res = client.get("/api/models", headers=ADMIN)
    assert res.status_code == 422
    assert "ARGUS_MODEL_POLICY" in res.text


def chains(**over: object) -> dict:
    return {
        "harness": {"codex": ["a"]},
        "implementation": {"codex": ["a"]},
        **over,
    }


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            chains(implementation={"codex": []}),
            "invalid model chain: implementation.codex",
        ),
        (chains(harness={"claude": ["x", "x"]}), "invalid model chain: harness.claude"),
        (chains(harness={"codex": [str(i) for i in range(11)]}), "harness.codex"),
        (chains(harness={"codex": ["--bad"]}), "harness.codex"),
        (chains(harness={"codex": ["bad\nmodel"]}), "harness.codex"),
        (chains(harness={"codex": ["a b"]}), "harness.codex"),
        (chains(harness={"codex": [42]}), "harness.codex"),
        (chains(harness={"codex": "a"}), "harness.codex"),
        (
            chains(implementation={"typo": ["a"]}),
            "invalid model chain: implementation.typo",
        ),
        (chains(harness=[]), "invalid harness map"),
    ],
)
def test_invalid_policy_is_422_and_writes_nothing(
    client: TestClient, body: dict, message: str
) -> None:
    client.put("/api/models", json=POLICY, headers=ADMIN)
    path = file_of(client)
    saved = path.read_text()
    res = client.put("/api/models", json=body, headers=ADMIN)
    assert res.status_code == 422, res.text
    assert message in res.json()["detail"]["message"]
    assert path.read_text() == saved
    assert not path.with_name(path.name + ".tmp").exists()


def test_invalid_first_put_leaves_no_file(client: TestClient) -> None:
    res = client.put("/api/models", json=chains(harness={"codex": []}), headers=ADMIN)
    assert res.status_code == 422
    assert not file_of(client).exists()
    extra = client.put("/api/models", json={**POLICY, "x": {}}, headers=ADMIN)
    assert extra.status_code == 422
    assert not file_of(client).exists()


def test_delete_restores_the_defaults(client: TestClient) -> None:
    defaults = client.get("/api/models", headers=ADMIN).json()
    client.put("/api/models", json=POLICY, headers=ADMIN)
    res = client.delete("/api/models", headers=ADMIN)
    assert res.status_code == 200
    assert res.json() == defaults
    assert not file_of(client).exists()
    assert client.delete("/api/models", headers=ADMIN).json() == defaults
