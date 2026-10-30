"""The `stderr-tail` artifact follows the task read rule and stays out of events
(worker-lifecycle spec 3.12.1, worker-read-scope spec 3)."""

from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api.main import app
from fastapi.testclient import TestClient

ADMIN_TOKEN = "test-admin-token-" + "s" * 32
# Synthetic tail content; the marker must never surface outside the artifact.
TAIL = b"synthetic stderr line\nSYNTHETIC-TAIL-MARKER-7f3a fatal: no checkout\n"
MEDIA_TYPE = (
    "text/plain; charset=utf-8; truncated=false; complete=true"
    f"; bytes={len(TAIL)}; redaction_version=1"
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        c.put("/api/projects/alpha", json={"name": "alpha"})
        yield c


def register(client: TestClient, role: str = "worker") -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": role}).json()
    res = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    assert res.status_code == 201
    return {"Authorization": f"Bearer {res.json()['token']}"}


def lose_session(client: TestClient, worker: dict[str, str]) -> tuple[dict, dict]:
    """Claim, store the tail as the NanoClaw hook does, then fail session_lost."""
    res = client.post(
        "/api/tasks", json={"title": "t", "project": "alpha", "spec": "s"}
    )
    assert res.status_code == 201, res.text
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    up = client.post(
        f"/api/tasks/{claimed['id']}/artifacts",
        params={"attempt": claimed["attempt"], "name": "stderr-tail", "role": "other"},
        content=TAIL,
        headers={**worker, "Content-Type": MEDIA_TYPE},
    )
    assert up.status_code == 201, up.text
    failed = client.post(
        f"/api/tasks/{claimed['id']}/fail",
        json={
            "attempt": claimed["attempt"],
            "error": "session container c exited (code 137) without a reply"
            "; last output: fatal (full tail: artifact stderr-tail)",
            "reason": "session_lost",
        },
        headers=worker,
    )
    assert failed.status_code == 200, failed.text
    return claimed, up.json()


def test_stderr_tail_is_stored_with_role_and_metadata(client: TestClient) -> None:
    worker = register(client)
    _, artifact = lose_session(client, worker)
    assert artifact["name"] == "stderr-tail"
    assert artifact["role"] == "other"
    assert artifact["media_type"] == MEDIA_TYPE
    assert artifact["size"] == len(TAIL)


def test_holder_and_managers_read_the_stderr_tail(client: TestClient) -> None:
    holder, orch = register(client), register(client, "orchestrator")
    claimed, artifact = lose_session(client, holder)
    for headers in (holder, orch, None):  # None → fixture default admin
        kw = {} if headers is None else {"headers": headers}
        meta = client.get(f"/api/artifacts/{artifact['id']}", **kw)
        assert meta.status_code == 200
        content = client.get(f"/api/artifacts/{artifact['id']}/content", **kw)
        assert content.status_code == 200
        assert content.content == TAIL
        text = client.get(f"/api/artifacts/{artifact['id']}/text", **kw)
        assert text.json()["text"] == TAIL.decode()
        listed = client.get(f"/api/tasks/{claimed['id']}/artifacts", **kw)
        assert [a["name"] for a in listed.json()] == ["stderr-tail"]


def test_non_leased_worker_gets_not_leased(client: TestClient) -> None:
    holder, other = register(client), register(client)
    claimed, artifact = lose_session(client, holder)
    for path in (
        f"/api/artifacts/{artifact['id']}",
        f"/api/artifacts/{artifact['id']}/content",
        f"/api/artifacts/{artifact['id']}/text",
        f"/api/tasks/{claimed['id']}/artifacts",
    ):
        denied = client.get(path, headers=other)
        assert denied.status_code == 403, path
        assert denied.json()["detail"]["code"] == "not_leased", path
        assert "SYNTHETIC-TAIL-MARKER" not in denied.text


def test_stderr_tail_content_never_reaches_events(client: TestClient) -> None:
    holder = register(client)
    claimed, _ = lose_session(client, holder)
    events = client.get("/api/events", params={"subject_id": claimed["id"]})
    assert events.status_code == 200
    assert any(e["kind"] == "task.fail" for e in events.json()), events.text
    assert "SYNTHETIC-TAIL-MARKER" not in events.text
    everything = client.get("/api/events", params={"limit": 500})
    assert everything.status_code == 200
    assert "SYNTHETIC-TAIL-MARKER" not in everything.text
