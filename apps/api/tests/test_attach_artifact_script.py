"""scripts/attach_artifact.py against a live API (docs/specs/task-inputs.spec.md 3.1)."""

import hashlib
import json
import socket
import threading
import time
from collections.abc import Generator
from pathlib import Path

import attach_artifact
import httpx2 as httpx
import pytest
import uvicorn
from argus_api.main import app

ADMIN_TOKEN = "test-admin-token-" + "s" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
BODY = "diff --git a/x b/x\n+café \U0001d11e\n".encode() * 2000


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[httpx.Client]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ARGUS_ARTIFACT_MAX_BYTES", str(1 << 20))
    for var in ("NO_PROXY", "no_proxy"):  # loopback, whatever proxy the host sets
        monkeypatch.setenv(var, "127.0.0.1,localhost")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "API did not start"
        time.sleep(0.02)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=ADMIN) as c:
            yield c
    finally:
        server.should_exit = True
        thread.join(10)


@pytest.fixture
def task(api: httpx.Client) -> str:
    res = api.post(
        "/api/tasks",
        json={
            "project": "demo",
            "title": "t",
            "required_capabilities": ["task-inputs"],
        },
    )
    return res.json()["id"]


@pytest.fixture
def patch(tmp_path: Path) -> Path:
    path = tmp_path / "fix.patch"
    path.write_bytes(BODY)
    return path


def run(
    api: httpx.Client,
    task: str,
    file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *extra: str,
) -> tuple[int, dict | None, str]:
    monkeypatch.setenv("ARGUS_TOKEN", ADMIN_TOKEN)
    argv = ["--url", str(api.base_url), "--task", task, "--file", str(file), *extra]
    code = attach_artifact.main(argv)
    out, err = capsys.readouterr()
    assert ADMIN_TOKEN not in out + err
    assert "café" not in out + err  # never content
    return code, json.loads(out) if out.strip() else None, err


def test_uploads_byte_exact_and_shares(api, task, patch, monkeypatch, capsys) -> None:
    code, out, _ = run(api, task, patch, monkeypatch, capsys, "--share-with-workers")
    assert code == 0 and out is not None
    digest = hashlib.sha256(BODY).hexdigest()
    assert out["status"] == "attached"
    assert (out["size"], out["sha256"], out["worker_readable"]) == (
        len(BODY),
        digest,
        True,
    )
    assert out["ref"] == f"attachment:{out['id']}:{digest}"
    assert out["media_type"] == "text/x-diff"
    stored = api.get(f"/api/artifacts/{out['id']}/content")
    assert stored.content == BODY
    view = api.get(f"/api/tasks/{task}/inputs").json()
    assert [i["id"] for i in view["inputs"]] == [out["id"]]
    # the reference it printed is accepted as the task's required input
    cur = api.get(f"/api/tasks/{task}/continuation").json()
    res = api.patch(
        f"/api/tasks/{task}/continuation",
        json={
            "expected_revision": cur["revision"],
            "reason": "required input",
            "wip": {"summary": "recovered patch", "ref": out["ref"]},
        },
    )
    assert res.status_code == 200, res.text


def test_retry_is_idempotent(api, task, patch, monkeypatch, capsys) -> None:
    first = run(api, task, patch, monkeypatch, capsys, "--name", "p.patch")
    again = run(api, task, patch, monkeypatch, capsys, "--name", "p.patch")
    assert (first[0], again[0]) == (0, 0)
    assert first[1] and again[1] and again[1]["status"] == "exists"
    assert again[1]["id"] == first[1]["id"]
    arts = api.get(f"/api/tasks/{task}/artifacts").json()
    assert len(arts) == 1


def test_same_name_other_bytes_is_a_conflict(
    api, task, patch, tmp_path, monkeypatch, capsys
) -> None:
    assert run(api, task, patch, monkeypatch, capsys)[0] == 0
    other = tmp_path / "other" / "fix.patch"
    other.parent.mkdir()
    other.write_bytes(BODY + b"\n")
    code, out, err = run(api, task, other, monkeypatch, capsys)
    assert (code, out) == (3, None)
    assert "other bytes" in err
    # the same bytes but another sharing is also not "done"
    code, _, err = run(api, task, patch, monkeypatch, capsys, "--share-with-workers")
    assert code == 3 and "worker_readable" in err


def test_empty_file_and_errors(api, task, tmp_path, monkeypatch, capsys) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    code, out, _ = run(api, task, empty, monkeypatch, capsys)
    assert code == 0 and out and out["size"] == 0
    code, _, err = run(api, "no-such-task", empty, monkeypatch, capsys)
    assert code == 1 and "404" in err
    code, _, _ = run(api, task, tmp_path / "missing", monkeypatch, capsys)
    assert code == 2
    monkeypatch.setenv("ARGUS_TOKEN", "wrong-token")
    argv = ["--url", str(api.base_url), "--task", task, "--file", str(empty)]
    assert attach_artifact.main(argv) == 1
    assert "wrong-token" not in capsys.readouterr().err


def test_verify_disagreement_fails(api, task, patch, monkeypatch, capsys) -> None:
    def broken(self, method, path, **kw):
        if path.endswith("/verify"):
            return 200, {"ok": False, "size": 1, "sha256": "0" * 64}
        return real(self, method, path, **kw)

    real = attach_artifact.Api.call
    monkeypatch.setattr(attach_artifact.Api, "call", broken)
    code, out, err = run(api, task, patch, monkeypatch, capsys)
    assert (code, out) == (1, None) and "stored bytes differ" in err
