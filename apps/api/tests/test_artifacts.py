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
    boot = client.post("/agents/bootstrap-tokens", json={}, headers=ADMIN).json()
    reg = client.post("/agents/register", json={"bootstrap_token": boot["token"]})
    return {"Authorization": f"Bearer {reg.json()['token']}"}


@pytest.fixture
def claimed(client: TestClient, worker: dict[str, str]) -> dict:
    client.post("/tasks", json={"title": "t"}, headers=ADMIN)
    return client.post("/tasks/claim", json={}, headers=worker).json()


def upload(client, headers, task, name="report.md", body=b"# hi", attempt=None):
    return client.post(
        f"/tasks/{task['id']}/artifacts",
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

    listed = client.get(f"/tasks/{claimed['id']}/artifacts", headers=ADMIN).json()
    assert [a["id"] for a in listed] == [art["id"]]
    content = client.get(f"/artifacts/{art['id']}/content", headers=ADMIN)
    assert content.content == b"# hi"
    assert content.headers["content-type"].startswith("text/markdown")

    done = client.post(
        f"/tasks/{claimed['id']}/complete",
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
