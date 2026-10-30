import asyncio
import hashlib
import sqlite3
from collections.abc import Generator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from argus_api.artifacts.service import check_media_type, list_artifacts
from argus_api.artifacts.store import LocalArtifactStore
from argus_api.db import Database
from argus_api.errors import ValidationFailedError
from argus_api.main import app
from fastapi.testclient import TestClient

ADMIN_TOKEN = "test-admin-token-" + "z" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
BINARY = b"\x00\xff\x10PK\x03\x04" + bytes(range(256))


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ARGUS_ARTIFACT_MAX_BYTES", "1024")
    monkeypatch.setenv("ARGUS_ATTACHMENT_MAX_PER_TASK", "3")
    with TestClient(app) as c:
        yield c


def register(client: TestClient, role: str) -> dict[str, str]:
    boot = client.post(
        "/api/agents/bootstrap-tokens", json={"role": role}, headers=ADMIN
    ).json()
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    return {"Authorization": f"Bearer {reg.json()['token']}"}


@pytest.fixture
def orchestrator(client: TestClient) -> dict[str, str]:
    return register(client, "orchestrator")


@pytest.fixture
def worker(client: TestClient) -> dict[str, str]:
    return register(client, "worker")


@pytest.fixture
def task(client: TestClient) -> dict:
    return client.post(
        "/api/tasks", json={"project": "global", "title": "t"}, headers=ADMIN
    ).json()


def attach(client, headers, task, name="shot.bin", body=BINARY, **params):
    return client.post(
        f"/api/tasks/{task['id']}/attachments",
        params={"name": name, **params},
        content=body,
        headers={**headers, "Content-Type": "application/octet-stream"},
    )


def store_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts"


def files(tmp_path: Path) -> list[Path]:
    return [p for p in store_root(tmp_path).rglob("*") if p.is_file()]


def rows(tmp_path: Path) -> int:
    con = sqlite3.connect(next(tmp_path.glob("*.db")))
    try:
        return con.execute("SELECT count(*) FROM artifacts").fetchone()[0]
    finally:
        con.close()


@pytest.mark.parametrize("who", ["orchestrator", "admin"])
def test_managers_attach_and_read_binary_and_text(
    client: TestClient, orchestrator, task, who: str
) -> None:
    h = orchestrator if who == "orchestrator" else ADMIN
    for name, body in (("shot.bin", BINARY), ("log.txt", b"line 1\nline 2\n")):
        res = attach(client, h, task, name=name, body=body)
        assert res.status_code == 201
        art = res.json()
        assert art["id"]
        assert (art["size"], art["sha256"]) == (
            len(body),
            hashlib.sha256(body).hexdigest(),
        )
        assert (art["source"], art["attempt"]) == ("attachment", 0)
        got = client.get(f"/api/artifacts/{art['id']}/content", headers=h)
        assert got.content == body
        ver = client.post(f"/api/artifacts/{art['id']}/verify", headers=h).json()
        assert ver["ok"] is True
    listed = client.get(f"/api/tasks/{task['id']}/artifacts", headers=h).json()
    assert {a["name"] for a in listed} == {"shot.bin", "log.txt"}
    text = client.get(f"/api/artifacts/{art['id']}/text", headers=h)
    assert text.status_code == 200


def test_provenance_is_the_caller_not_an_agent(
    client: TestClient, orchestrator, task
) -> None:
    art = attach(client, orchestrator, task).json()
    assert art["attached_by"].startswith("orchestrator:")
    assert not art["attached_by"].startswith("agent:")
    assert not art["created_by"].startswith("agent:")
    admin = attach(client, ADMIN, task, name="b.bin").json()
    assert admin["attached_by"].startswith("admin:")
    events = client.get(
        "/api/events", params={"subject_id": task["id"]}, headers=ADMIN
    ).json()
    text = str(events)
    assert "task.attachment" in text
    assert "PK" not in text


def test_workers_cannot_attach_or_read(
    client: TestClient, orchestrator, worker, task
) -> None:
    assert attach(client, worker, task).status_code == 403  # not leased
    art = attach(client, orchestrator, task).json()
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    assert claimed["id"] == task["id"]
    assert attach(client, worker, task, name="w.bin").status_code == 403  # leased
    base = f"/api/artifacts/{art['id']}"
    assert client.get(base, headers=worker).status_code == 403
    assert client.get(f"{base}/content", headers=worker).status_code == 403
    assert client.get(f"{base}/text", headers=worker).status_code == 403
    assert client.post(f"{base}/verify", headers=worker).status_code == 403
    listed = client.get(f"/api/tasks/{task['id']}/artifacts", headers=worker)
    assert listed.status_code == 200
    assert listed.json() == []
    # a worker may not list an attachment as its own output either
    done = client.post(
        f"/api/tasks/{task['id']}/complete",
        json={
            "attempt": claimed["attempt"],
            "result": {"summary": "x", "artifacts": [art["id"]]},
        },
        headers=worker,
    )
    assert done.status_code == 422


def test_worker_artifacts_stay_readable_by_the_worker(
    client: TestClient, orchestrator, worker, task
) -> None:
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    up = client.post(
        f"/api/tasks/{task['id']}/artifacts",
        params={"attempt": claimed["attempt"], "name": "r.md"},
        content=b"hi",
        headers=worker,
    ).json()
    assert up["source"] == "worker" and up["attached_by"] is None
    attach(client, orchestrator, task)
    listed = client.get(f"/api/tasks/{task['id']}/artifacts", headers=worker).json()
    assert [a["id"] for a in listed] == [up["id"]]
    assert (
        client.get(f"/api/artifacts/{up['id']}/content", headers=worker).content
        == b"hi"
    )


def test_unauthenticated_gets_401_or_403(client: TestClient, task) -> None:
    res = client.post(
        f"/api/tasks/{task['id']}/attachments", params={"name": "a"}, content=b"x"
    )
    assert res.status_code in (401, 403)


def test_unknown_task_is_404_and_leaves_nothing(
    client: TestClient, tmp_path: Path
) -> None:
    res = attach(client, ADMIN, {"id": "nope"})
    assert res.status_code == 404
    assert files(tmp_path) == []


def test_over_limit_leaves_no_row_or_file(
    client: TestClient, task, tmp_path: Path
) -> None:
    res = attach(client, ADMIN, task, body=b"x" * 1025)
    assert res.status_code == 413
    assert res.json()["detail"]["code"] == "too_large"
    assert files(tmp_path) == []
    assert rows(tmp_path) == 0


def test_duplicate_name_conflicts_and_keeps_original(
    client: TestClient, task, tmp_path: Path
) -> None:
    first = attach(client, ADMIN, task).json()
    res = attach(client, ADMIN, task, body=b"other")
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "artifact_exists"
    got = client.get(f"/api/artifacts/{first['id']}/content", headers=ADMIN)
    assert got.content == BINARY
    assert len(files(tmp_path)) == 1


@pytest.mark.parametrize(
    "name",
    [
        "..",
        "../x",
        "a/../b",
        "/etc/passwd",
        "a/b",
        "a\\b",
        ".hidden",
        "a\x00b",
        "a.txt\n",
        "caf\u00e9.txt",
        "\uff0e\uff0e",
        "a\u2215b",
        "",
        "x" * 201,
    ],
)
def test_unsafe_names_are_rejected_before_any_write(
    client: TestClient, task, tmp_path: Path, name: str
) -> None:
    res = attach(client, ADMIN, task, name=name)
    assert res.status_code == 422
    assert files(tmp_path) == []
    assert not any(p.name == "passwd" for p in tmp_path.rglob("*"))
    assert rows(tmp_path) == 0


def test_stream_failure_leaves_no_row_file_or_staging(
    client: TestClient, task, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = LocalArtifactStore.stage

    async def broken(self, chunks):
        async def failing():
            yield b"partial"
            raise RuntimeError("connection lost")

        return await real(self, failing())

    monkeypatch.setattr(LocalArtifactStore, "stage", broken)
    with pytest.raises(RuntimeError):
        attach(client, ADMIN, task)
    assert files(tmp_path) == []
    assert rows(tmp_path) == 0


def test_count_cap(client: TestClient, task, tmp_path: Path) -> None:
    for i in range(3):
        assert attach(client, ADMIN, task, name=f"f{i}").status_code == 201
    res = attach(client, ADMIN, task, name="f3")
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "attachment_limit"
    assert len(files(tmp_path)) == 3
    other = client.post(
        "/api/tasks", json={"project": "global", "title": "u"}, headers=ADMIN
    ).json()
    assert attach(client, ADMIN, other, name="f0").status_code == 201


def test_tampered_file_fails_verify(client: TestClient, task, tmp_path: Path) -> None:
    art = attach(client, ADMIN, task).json()
    stored = store_root(tmp_path) / "tasks" / task["id"] / "0" / "shot.bin"
    assert stored.read_bytes() == BINARY
    stored.write_bytes(BINARY[:-1] + b"!")
    ver = client.post(f"/api/artifacts/{art['id']}/verify", headers=ADMIN).json()
    assert ver["ok"] is False


def test_attachments_do_not_use_the_agent_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ARGUS_ARTIFACT_MAX_BYTES", "1024")
    monkeypatch.setenv("ARGUS_ARTIFACT_AGENT_QUOTA_BYTES", "10")
    with TestClient(app) as client:
        orch = register(client, "orchestrator")
        worker = register(client, "worker")
        task = client.post(
            "/api/tasks", json={"project": "global", "title": "t"}, headers=ADMIN
        ).json()
        for i in range(3):
            assert (
                attach(client, orch, task, name=f"f{i}", body=b"x" * 100).status_code
                == 201
            )
        claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
        res = client.post(
            f"/api/tasks/{task['id']}/artifacts",
            params={"attempt": claimed["attempt"], "name": "w.txt"},
            content=b"12345",
            headers=worker,
        )
        assert res.status_code == 201  # the worker's own quota is untouched


def test_attachment_event_is_in_task_feeds_without_identity(
    client: TestClient, orchestrator, worker, task
) -> None:
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    assert claimed["id"] == task["id"]
    art = attach(client, orchestrator, task, name="secret-shot.png").json()
    params = {"subject_type": "task", "limit": 200}
    managers = client.get("/api/events", params=params, headers=orchestrator)
    events = [e for e in managers.json() if e["kind"] == "task.attachment"]
    assert len(events) == 1
    assert events[0]["subject_id"] == task["id"]
    for key in ("name", "artifact_id", "size", "sha256"):
        assert key not in events[0]["data"]
    assert events[0]["data"] == {"source": "attachment", "worker_readable": False}
    assert events[0]["actor"].startswith("orchestrator:")
    own = client.get("/api/events", params=params, headers=worker).json()
    assert [e for e in own if e["kind"] == "task.attachment"] == []
    waited = client.get("/api/events/wait", params={"after": 0}, headers=worker).json()
    assert [e for e in waited["events"] if e["kind"] == "task.attachment"] == []
    feed = client.get("/api/events", params={"limit": 200}, headers=worker)
    assert feed.status_code == 200
    assert "secret-shot.png" not in feed.text and art["id"] not in feed.text
    assert art["sha256"][:12] not in feed.text
    assert "task.claim" in feed.text


def test_leased_worker_listing_has_own_artifacts_but_no_attachments(
    client: TestClient, orchestrator, worker, task
) -> None:
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    up = client.post(
        f"/api/tasks/{task['id']}/artifacts",
        params={"attempt": claimed["attempt"], "name": "r.md"},
        content=b"hi",
        headers=worker,
    ).json()
    att = attach(client, orchestrator, task).json()
    url = f"/api/tasks/{task['id']}/artifacts"
    assert [a["id"] for a in client.get(url, headers=worker).json()] == [up["id"]]
    both = {a["id"] for a in client.get(url, headers=orchestrator).json()}
    assert both == {up["id"], att["id"]}


def test_inbox_lists_worker_artifacts_not_attachments(
    client: TestClient, orchestrator, worker
) -> None:
    approval = client.post(
        "/api/tasks",
        json={"project": "global", "title": "a", "requires_approval": True},
        headers=ADMIN,
    ).json()
    att = attach(client, orchestrator, approval).json()  # attempt 0 == task attempt
    review = client.post(
        "/api/tasks",
        json={"project": "global", "title": "r", "review": "orchestrator"},
        headers=ADMIN,
    ).json()
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    assert claimed["id"] == review["id"]
    up = client.post(
        f"/api/tasks/{review['id']}/artifacts",
        params={"attempt": claimed["attempt"], "name": "r.md"},
        content=b"hi",
        headers=worker,
    ).json()
    done = client.post(
        f"/api/tasks/{review['id']}/complete",
        json={
            "attempt": claimed["attempt"],
            "result": {"summary": "s", "artifacts": [up["id"]]},
        },
        headers=worker,
    )
    assert done.status_code == 200, done.text
    items = {
        i["subject_id"]: i
        for i in client.get("/api/inbox", headers=ADMIN).json()["items"]
    }
    assert items[approval["id"]]["artifact_ids"] == []
    assert items[review["id"]]["artifact_ids"] == [up["id"]]
    assert att["id"] not in str(items)


def test_failed_commit_leaves_no_row_or_final_file(
    client: TestClient, task, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = Database.write

    @asynccontextmanager
    async def failing(self):
        async with real(self) as tx:
            yield tx
            raise RuntimeError("commit failed")  # after promote, rolls the row back

    monkeypatch.setattr(Database, "write", failing)
    with pytest.raises(RuntimeError):
        attach(client, ADMIN, task)
    assert files(tmp_path) == []
    assert rows(tmp_path) == 0


@pytest.mark.parametrize("media_type", ["image/png", "text/plain; charset=utf-8"])
def test_valid_media_type_is_stored(
    client: TestClient, orchestrator, task, media_type: str
) -> None:
    res = client.post(
        f"/api/tasks/{task['id']}/attachments",
        params={"name": "a.bin"},
        content=b"x",
        headers={**orchestrator, "Content-Type": media_type},
    )
    assert res.status_code == 201
    assert res.json()["media_type"] == media_type


@pytest.mark.parametrize(
    "media_type", ["text/plain\x00x", "nonsense", "a/b" + "x" * 300, "text/plain;"]
)
def test_bad_media_type_is_422_and_writes_nothing(
    client: TestClient, task, tmp_path: Path, media_type: str
) -> None:
    res = client.post(
        f"/api/tasks/{task['id']}/attachments",
        params={"name": "a.bin"},
        content=b"x",
        headers={**ADMIN, "Content-Type": media_type},
    )
    assert res.status_code == 422
    assert files(tmp_path) == [] and rows(tmp_path) == 0


@pytest.mark.parametrize(
    "media_type",
    [
        "text/plain; charset=utf-8",
        "text/plain ; charset=utf-8",
        "text/plain\t;\ta=b",
        'text/plain; charset="utf-8"',
        'text/plain; a="b c\\"d"',
        "application/vnd.x+json",
    ],
)
def test_check_media_type_accepts(media_type: str) -> None:
    assert check_media_type(media_type) == media_type


@pytest.mark.parametrize(
    "media_type",
    [
        "text/plain\x0b; a=b",
        "text/plain\x01",
        "text/plain\x7f",
        "text/plain\u2028",
        "text/plain\x85",
        "text/plain\xa0",
        "text/plain\r",
        "text/plain\n",
        "text/plain\x00",
        'text/plain; a="b\tc"',
        'text/plain; a="b\\\tc"',
        "text/plain; charset = utf-8",
        "text/plain\t;\ta = b",
        "text/*",
        "*/*",
        "a/b" + "x" * 300,
    ],
)
def test_check_media_type_rejects(media_type: str) -> None:
    with pytest.raises(ValidationFailedError):
        check_media_type(media_type)


def test_failed_commit_after_worker_upload_leaves_no_row_or_file(
    client: TestClient, worker, task, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    real = Database.write

    @asynccontextmanager
    async def failing(self):
        async with real(self) as tx:
            yield tx
            raise RuntimeError("commit failed")  # after promote, rolls the row back

    monkeypatch.setattr(Database, "write", failing)
    with pytest.raises(RuntimeError):
        client.post(
            f"/api/tasks/{task['id']}/artifacts",
            params={"attempt": claimed["attempt"], "name": "r.md"},
            content=b"hi",
            headers=worker,
        )
    assert files(tmp_path) == []
    assert rows(tmp_path) == 0


def test_list_artifacts_excludes_attachments_by_default(
    client: TestClient, orchestrator, task
) -> None:
    attach(client, orchestrator, task)

    async def run() -> tuple[list, list]:
        async with client.app.state.db.read() as conn:
            return (
                await list_artifacts(conn, task["id"]),
                await list_artifacts(conn, task["id"], include_attachments=True),
            )

    default, withall = asyncio.run(run())
    assert default == []
    assert len(withall) == 1


def test_attachments_are_not_shared_by_default(
    client: TestClient, orchestrator, task
) -> None:
    art = attach(client, orchestrator, task).json()
    assert art["worker_readable"] is False


def read_all(client: TestClient, headers, art: dict) -> list[int]:
    base = f"/api/artifacts/{art['id']}"
    return [
        client.get(base, headers=headers).status_code,
        client.get(f"{base}/content", headers=headers).status_code,
        client.get(f"{base}/text", headers=headers).status_code,
        client.post(f"{base}/verify", headers=headers).status_code,
    ]


def test_shared_attachment_is_readable_by_every_worker_leased_on_the_task(
    client: TestClient, orchestrator, task
) -> None:
    body = "input for the next attempt\n\u00e9\n".encode()
    shared = attach(
        client, orchestrator, task, name="in.txt", body=body, worker_readable="true"
    ).json()
    private = attach(client, orchestrator, task, name="secret.bin").json()
    assert (shared["worker_readable"], private["worker_readable"]) == (True, False)
    first, second = register(client, "worker"), register(client, "worker")
    claimed = client.post("/api/tasks/claim", json={}, headers=first).json()
    released = client.post(
        f"/api/tasks/{task['id']}/release", json={"reason": "handoff"}, headers=ADMIN
    )
    assert released.status_code == 200, released.text
    again = client.post("/api/tasks/claim", json={}, headers=second).json()
    assert again["id"] == task["id"] and again["attempt"] != claimed["attempt"]
    for worker in (first, second):  # an earlier attempt's grant still reads
        assert read_all(client, worker, shared) == [200] * 4
        assert read_all(client, worker, private) == [403] * 4
        got = client.get(f"/api/artifacts/{shared['id']}/content", headers=worker)
        assert got.content == body
        assert hashlib.sha256(got.content).hexdigest() == shared["sha256"]
        listed = client.get(f"/api/tasks/{task['id']}/artifacts", headers=worker)
        assert [a["id"] for a in listed.json()] == [shared["id"]]
    # the attachment is still not a worker's own output
    done = client.post(
        f"/api/tasks/{task['id']}/complete",
        json={
            "attempt": again["attempt"],
            "result": {"summary": "x", "artifacts": [shared["id"]]},
        },
        headers=second,
    )
    assert done.status_code == 422


def test_shared_attachment_is_denied_to_workers_on_other_tasks(
    client: TestClient, orchestrator, worker
) -> None:
    other = client.post(
        "/api/tasks", json={"project": "global", "title": "other"}, headers=ADMIN
    ).json()
    assert (
        client.post("/api/tasks/claim", json={}, headers=worker).json()["id"]
        == (other["id"])
    )
    task = client.post(
        "/api/tasks", json={"project": "global", "title": "t"}, headers=ADMIN
    ).json()
    shared = attach(client, orchestrator, task, worker_readable="true").json()
    assert read_all(client, worker, shared) == [403] * 4
    res = client.get(f"/api/artifacts/{shared['id']}/content", headers=worker)
    assert res.json()["detail"]["code"] == "not_leased"
    assert (
        client.get(f"/api/tasks/{task['id']}/artifacts", headers=worker).status_code
        == 403
    )
    # sharing does not let a worker attach
    assert (
        attach(client, worker, other, name="w.bin", worker_readable="true").status_code
        == 403
    )


def test_shared_attachment_event_names_no_file(
    client: TestClient, orchestrator, worker, task
) -> None:
    client.post("/api/tasks/claim", json={}, headers=worker)
    art = attach(client, orchestrator, task, worker_readable="true").json()
    events = client.get(
        "/api/events", params={"subject_id": task["id"]}, headers=ADMIN
    ).json()
    data = [e["data"] for e in events if e["kind"] == "task.attachment"]
    assert data == [{"source": "attachment", "worker_readable": True}]
    feed = client.get("/api/events", params={"limit": 200}, headers=worker).text
    assert "task.attachment" not in feed and art["id"] not in feed


def test_list_artifacts_include_shared(client: TestClient, orchestrator, task) -> None:
    attach(client, orchestrator, task, name="a.bin")
    shared = attach(client, orchestrator, task, name="b.bin", worker_readable="true")

    async def run() -> list:
        async with client.app.state.db.read() as conn:
            return await list_artifacts(conn, task["id"], include_shared=True)

    assert [a.id for a in asyncio.run(run())] == [shared.json()["id"]]
