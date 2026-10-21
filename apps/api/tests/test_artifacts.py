import hashlib
from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api.main import app
from fastapi.testclient import TestClient

ADMIN_TOKEN = "test-admin-token-" + "z" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def client(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ARGUS_ARTIFACT_MAX_BYTES", "1024")
    with TestClient(app) as c:
        yield c


@pytest.fixture
def worker(client: TestClient) -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={}, headers=ADMIN).json()
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    return {"Authorization": f"Bearer {reg.json()['token']}"}


@pytest.fixture
def claimed(client: TestClient, worker: dict[str, str]) -> dict:
    client.post("/api/tasks", json={"project": "global", "title": "t"}, headers=ADMIN)
    return client.post("/api/tasks/claim", json={}, headers=worker).json()


def upload(client, headers, task, name="report.md", body=b"# hi", attempt=None):
    return client.post(
        f"/api/tasks/{task['id']}/artifacts",
        params={"attempt": attempt or task["attempt"], "name": name},
        content=body,
        headers={**headers, "Content-Type": "text/markdown"},
    )


def test_upload_list_download_and_complete(
    client: TestClient, worker: dict[str, str], claimed: dict, data_dir: Path
) -> None:
    res = upload(client, worker, claimed)
    assert res.status_code == 201
    art = res.json()
    assert (art["size"], art["media_type"]) == (4, "text/markdown")
    assert art["sha256"] == hashlib.sha256(b"# hi").hexdigest()
    path = data_dir / "artifacts" / "tasks" / claimed["id"] / "1" / "report.md"
    assert path.read_bytes() == b"# hi"
    assert not any((data_dir / "artifacts" / ".staging").iterdir())

    listed = client.get(f"/api/tasks/{claimed['id']}/artifacts", headers=ADMIN).json()
    assert [a["id"] for a in listed] == [art["id"]]
    content = client.get(f"/api/artifacts/{art['id']}/content", headers=ADMIN)
    assert content.content == b"# hi"
    assert content.headers["content-type"].startswith("text/markdown")

    done = client.post(
        f"/api/tasks/{claimed['id']}/complete",
        json={
            "attempt": claimed["attempt"],
            "result": {"summary": "wrote it", "artifacts": [art["id"]]},
        },
        headers=worker,
    )
    assert done.status_code == 200
    assert done.json()["result"]["artifacts"] == [art["id"]]


def test_size_limit(
    client: TestClient, worker: dict[str, str], claimed: dict, data_dir: Path
) -> None:
    res = upload(client, worker, claimed, body=b"x" * 1025)
    assert res.status_code == 413
    assert res.json()["detail"]["code"] == "too_large"
    assert not any((data_dir / "artifacts" / ".staging").iterdir())


def test_agent_quota_refuses_over_cap(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ARGUS_ARTIFACT_MAX_BYTES", "1024")
    monkeypatch.setenv("ARGUS_ARTIFACT_AGENT_QUOTA_BYTES", "10")
    with TestClient(app) as client:
        boot = client.post(
            "/api/agents/bootstrap-tokens", json={}, headers=ADMIN
        ).json()
        worker = {
            "Authorization": (
                "Bearer "
                + client.post(
                    "/api/agents/register", json={"bootstrap_token": boot["token"]}
                ).json()["token"]
            )
        }
        client.post(
            "/api/tasks", json={"project": "global", "title": "t"}, headers=ADMIN
        )
        claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
        first = upload(client, worker, claimed, name="a.md", body=b"1234567890")
        assert first.status_code == 201, first.text
        second = upload(client, worker, claimed, name="b.md", body=b"x")
        assert second.status_code == 400
        assert second.json()["detail"]["code"] == "artifact_quota"


@pytest.mark.parametrize("name", ["../escape", ".hidden", "a/b", "", "x" * 201])
def test_bad_names_are_rejected(
    client: TestClient, worker: dict[str, str], claimed: dict, name: str
) -> None:
    res = upload(client, worker, claimed, name=name)
    assert res.status_code == 422


def test_upload_requires_the_current_lease(
    client: TestClient, worker: dict[str, str], claimed: dict, data_dir: Path
) -> None:
    res = upload(client, worker, claimed, attempt=claimed["attempt"] + 1)
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "stale_attempt"
    assert not any((data_dir / "artifacts" / ".staging").iterdir())
    assert upload(client, ADMIN, claimed).status_code == 403  # admins are not agents


def test_artifacts_are_immutable(
    client: TestClient, worker: dict[str, str], claimed: dict
) -> None:
    assert upload(client, worker, claimed).status_code == 201
    res = upload(client, worker, claimed, body=b"changed")
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "artifact_exists"


def test_roles_filters_and_in_result(
    client: TestClient, worker: dict[str, str], claimed: dict
) -> None:
    def put(name: str, role: str | None) -> dict:
        params = {"attempt": claimed["attempt"], "name": name}
        res = client.post(
            f"/api/tasks/{claimed['id']}/artifacts",
            params={**params, "role": role} if role else params,
            content=b"x",
            headers=worker,
        )
        assert res.status_code == 201, res.text
        return res.json()

    report, other = put("report.md", "report"), put("log.txt", None)
    assert (report["role"], other["role"]) == ("report", None)
    bad = client.post(
        f"/api/tasks/{claimed['id']}/artifacts",
        params={"attempt": claimed["attempt"], "name": "x", "role": "verdict"},
        content=b"x",
        headers=worker,
    )
    assert bad.status_code == 422
    client.post(
        f"/api/tasks/{claimed['id']}/complete",
        json={
            "attempt": claimed["attempt"],
            "result": {"summary": "s", "artifacts": [report["id"]]},
        },
        headers=worker,
    )
    path = f"/api/tasks/{claimed['id']}/artifacts"
    listed = {a["name"]: a for a in client.get(path, headers=ADMIN).json()}
    assert listed["report.md"]["in_result"] is True
    assert listed["log.txt"]["in_result"] is False
    by_role = client.get(path, params={"role": "report"}, headers=ADMIN).json()
    assert [a["id"] for a in by_role] == [report["id"]]
    assert client.get(path, params={"attempt": 9}, headers=ADMIN).json() == []
    meta = client.get(f"/api/artifacts/{report['id']}", headers=ADMIN).json()
    assert meta == {k: v for k, v in listed["report.md"].items() if k != "in_result"}


def test_text_reads_in_ranges_without_splitting_characters(
    client: TestClient, worker: dict[str, str], claimed: dict
) -> None:
    body = ("a" * 63 + "é" + "z" * 100).encode()  # é straddles byte 64
    art = upload(client, worker, claimed, body=body).json()
    path = f"/api/artifacts/{art['id']}/text"
    first = client.get(path, params={"max_bytes": 64}, headers=ADMIN).json()
    assert first["text"] == "a" * 63
    assert (first["length"], first["next_offset"]) == (63, 63)
    assert first["artifact"]["sha256"] == art["sha256"]
    rest = client.get(
        path, params={"offset": 63, "max_bytes": 1024}, headers=ADMIN
    ).json()
    assert rest["text"] == "é" + "z" * 100
    assert rest["next_offset"] is None
    assert (
        client.get(path, params={"max_bytes": 10**6}, headers=ADMIN).status_code == 422
    )


def test_binary_artifacts_return_no_bytes(
    client: TestClient, worker: dict[str, str], claimed: dict
) -> None:
    art = client.post(
        f"/api/tasks/{claimed['id']}/artifacts",
        params={"attempt": claimed["attempt"], "name": "shot.png"},
        content=b"\x89PNG",
        headers={**worker, "Content-Type": "image/png"},
    ).json()
    read = client.get(f"/api/artifacts/{art['id']}/text", headers=ADMIN).json()
    assert (read["binary"], read["text"], read["length"]) == (True, None, 0)


def test_content_honours_range(
    client: TestClient, worker: dict[str, str], claimed: dict
) -> None:
    art = upload(client, worker, claimed, body=b"0123456789").json()
    res = client.get(
        f"/api/artifacts/{art['id']}/content",
        headers={**ADMIN, "Range": "bytes=2-4"},
    )
    assert (res.status_code, res.content) == (206, b"234")


def test_verify_detects_changed_and_missing_bytes(
    client: TestClient, worker: dict[str, str], claimed: dict, data_dir: Path
) -> None:
    art = upload(client, worker, claimed).json()
    path = f"/api/artifacts/{art['id']}/verify"
    good = client.post(path, headers=ADMIN).json()
    assert good == {
        "ok": True,
        "size": 4,
        "sha256": art["sha256"],
        "expected_size": 4,
        "expected_sha256": art["sha256"],
    }
    stored = data_dir / "artifacts" / "tasks" / claimed["id"] / "1" / "report.md"
    stored.write_bytes(b"# ho")
    changed = client.post(path, headers=ADMIN).json()
    assert (changed["ok"], changed["size"]) == (False, 4)
    assert changed["sha256"] != art["sha256"]
    stored.unlink()
    missing = client.post(path, headers=ADMIN).json()
    assert (missing["ok"], missing["size"], missing["sha256"]) == (False, None, None)
