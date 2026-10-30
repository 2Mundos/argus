"""Task inputs (docs/specs/task-inputs.spec.md 3.1-3.3, 3.6, 3.7): integrity on
attach, the inputs view, receipts, required-input references and isolation."""

import hashlib
import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api.main import app
from fastapi.testclient import TestClient

ADMIN_TOKEN = "test-admin-token-" + "i" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
LIMIT = 1024


def utf8_fixture(size: int) -> bytes:
    """Exactly `size` bytes of patch-like UTF-8 text with 2-, 3- and 4-byte
    characters, so read boundaries fall inside characters."""
    line = "+ café €10 \U0001d11e naïve 日本語\n"
    out = b"diff --git a/notes.txt b/notes.txt\n"
    n = 0
    while len(out) < size:
        out += f"{n:05d} {line}".encode()
        n += 1
    out = out[:size]
    while True:  # never end inside a character: trim, then pad with ASCII
        try:
            out.decode()
            break
        except UnicodeDecodeError:
            out = out[:-1]
    return out + b"x" * (size - len(out))


PATCH = utf8_fixture(62_300)


def make_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, max_bytes: int
) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ARGUS_ARTIFACT_MAX_BYTES", str(max_bytes))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    yield from make_client(tmp_path, monkeypatch, LIMIT)


@pytest.fixture
def big(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    yield from make_client(tmp_path, monkeypatch, 1 << 20)


def register(client: TestClient, role: str = "worker") -> dict[str, str]:
    boot = client.post(
        "/api/agents/bootstrap-tokens", json={"role": role}, headers=ADMIN
    ).json()
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    return {"Authorization": f"Bearer {reg.json()['token']}"}


def new_task(client: TestClient, caps: list[str] | None = None) -> dict:
    return client.post(
        "/api/tasks",
        json={"project": "global", "title": "t", "required_capabilities": caps or []},
        headers=ADMIN,
    ).json()


def attach(client, task, name="in.txt", body=b"input\n", headers=ADMIN, **params):
    return client.post(
        f"/api/tasks/{task['id']}/attachments",
        params={"name": name, **params},
        content=body,
        headers={**headers, "Content-Type": "text/plain; charset=utf-8"},
    )


def share(client, task, name="in.txt", body=b"input\n") -> dict:
    res = attach(client, task, name=name, body=body, worker_readable="true")
    assert res.status_code == 201, res.text
    return res.json()


def claim(client: TestClient, worker, caps: list[str] | None = None) -> dict:
    res = client.post(
        "/api/tasks/claim", json={"capabilities": caps or []}, headers=worker
    )
    assert res.status_code == 200, res.text
    return res.json()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def code(res) -> str:
    return res.json()["detail"]["code"]


def db_exec(tmp_path: Path, sql: str, params: tuple = ()) -> list:
    con = sqlite3.connect(next(tmp_path.glob("*.db")))
    try:
        rows = con.execute(sql, params).fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def stored(tmp_path: Path) -> tuple[int, list[Path]]:
    rows = db_exec(tmp_path, "SELECT count(*) FROM artifacts")[0][0]
    return rows, [p for p in (tmp_path / "artifacts").rglob("*") if p.is_file()]


def receipt(client, worker, task, art, attempt, state, **extra):
    return client.post(
        f"/api/tasks/{task['id']}/inputs/{art['id']}/receipt",
        json={"attempt": attempt, "state": state, **extra},
        headers=worker,
    )


# -- 3.1 upload with integrity -----------------------------------------------------


def test_expected_size_and_hash_accept_the_exact_bytes(client: TestClient) -> None:
    task = new_task(client)
    body = "café \U0001d11e\n".encode()
    res = attach(
        client, task, body=body, expected_size=len(body), expected_sha256=sha(body)
    )
    assert res.status_code == 201, res.text
    assert (res.json()["size"], res.json()["sha256"]) == (len(body), sha(body))


@pytest.mark.parametrize(
    "params",
    [
        {"expected_size": 7},  # a short (truncated) upload: fewer bytes than claimed
        {"expected_size": 5},
        {"expected_sha256": "0" * 64},
        {"expected_size": 6, "expected_sha256": "f" * 64},
    ],
)
def test_integrity_mismatch_discards_the_staged_file(
    client: TestClient, tmp_path: Path, params: dict
) -> None:
    task = new_task(client)
    res = attach(client, task, body=b"input\n", **params)
    assert (res.status_code, code(res)) == (422, "integrity_mismatch")
    assert "input" not in res.text  # never the content
    assert stored(tmp_path) == (0, [])
    assert not list((tmp_path / "artifacts" / ".staging").glob("*"))
    events = client.get("/api/events", params={"subject_id": task["id"]}, headers=ADMIN)
    assert "task.attachment" not in events.text


@pytest.mark.parametrize("bad", ["A" * 64, "a" * 63, "g" * 64])
def test_expected_sha256_must_be_lowercase_hex(client: TestClient, bad: str) -> None:
    res = attach(client, new_task(client), expected_sha256=bad)
    assert res.status_code == 422


@pytest.mark.parametrize(
    ("size", "status"), [(0, 201), (1, 201), (LIMIT, 201), (LIMIT + 1, 413)]
)
def test_boundary_sizes(
    client: TestClient, tmp_path: Path, size: int, status: int
) -> None:
    body = b"a" * size
    res = attach(
        client,
        new_task(client),
        body=body,
        expected_size=size,
        expected_sha256=sha(body),
    )
    assert res.status_code == status, res.text
    if status == 201:
        assert res.json()["size"] == size
    else:
        assert stored(tmp_path) == (0, [])


def test_multibyte_utf8_size_and_hash_are_byte_exact(big: TestClient) -> None:
    task = new_task(big)
    art = share(big, task, name="fix.patch", body=PATCH)
    assert (art["size"], art["sha256"]) == (62_300, sha(PATCH))
    assert len(PATCH.decode()) < len(PATCH)  # characters != bytes
    # the same check with the expected values computed by the uploader
    again = attach(
        big,
        task,
        name="fix2.patch",
        body=PATCH,
        expected_size=len(PATCH),
        expected_sha256=sha(PATCH),
    )
    assert again.status_code == 201


def test_text_pages_reassemble_byte_identical(big: TestClient) -> None:
    task = new_task(big)
    art = share(big, task, name="fix.patch", body=PATCH)
    worker = register(big)
    claim(big, worker)
    parts, offset, reads = [], 0, 0
    while offset is not None:
        page = big.get(
            f"/api/artifacts/{art['id']}/text",
            params={"offset": offset, "max_bytes": 4099},  # odd: splits characters
            headers=worker,
        ).json()
        assert page["offset"] == offset and not page["binary"]
        parts.append(page["text"].encode())
        assert len(parts[-1]) == page["length"]
        offset, reads = page["next_offset"], reads + 1
    assert reads > 1
    assert b"".join(parts) == PATCH
    assert sha(b"".join(parts)) == art["sha256"]


def test_range_reads_reassemble_byte_identical(big: TestClient) -> None:
    task = new_task(big)
    art = share(big, task, name="fix.patch", body=PATCH)
    worker = register(big)
    claim(big, worker)
    url = f"/api/artifacts/{art['id']}/content"
    got, start, step = b"", 0, 10_007
    while start < len(PATCH):
        end = min(start + step, len(PATCH)) - 1
        res = big.get(url, headers={**worker, "Range": f"bytes={start}-{end}"})
        assert res.status_code == 206
        assert res.headers["content-range"] == f"bytes {start}-{end}/{len(PATCH)}"
        got += res.content
        start = end + 1
    assert got == PATCH
    # a resume from a partial size, as the channel does
    tail = big.get(url, headers={**worker, "Range": "bytes=62000-"})
    assert tail.status_code == 206 and PATCH[:62000] + tail.content == PATCH


# -- 3.2 inputs view -----------------------------------------------------------------


def test_inputs_view_lists_shared_attachments_only(client: TestClient) -> None:
    task = new_task(client)
    orch = register(client, "orchestrator")
    shared = share(client, task, name="a.txt", body=b"aaa")
    attach(client, task, name="private.txt", body=b"secret", headers=orch)
    for h in (ADMIN, orch):
        view = client.get(f"/api/tasks/{task['id']}/inputs", headers=h).json()
        assert view["unshared_attachments"] == 1
        assert [i["id"] for i in view["inputs"]] == [shared["id"]]
    entry = view["inputs"][0]
    assert entry == {
        "id": shared["id"],
        "task_id": task["id"],
        "name": "a.txt",
        "media_type": "text/plain; charset=utf-8",
        "size": 3,
        "sha256": sha(b"aaa"),
        "attached_by": shared["attached_by"],
        "created_at": shared["created_at"],
        "receipts": [],
    }
    worker = register(client)
    claim(client, worker)
    mine = client.get(f"/api/tasks/{task['id']}/inputs", headers=worker)
    assert mine.status_code == 200
    assert mine.json()["unshared_attachments"] is None  # workers never learn it
    assert [i["id"] for i in mine.json()["inputs"]] == [shared["id"]]
    assert "private.txt" not in mine.text


def test_inputs_view_unknown_task_is_404(client: TestClient) -> None:
    res = client.get("/api/tasks/nope/inputs", headers=ADMIN)
    assert res.status_code == 404


def test_cross_task_worker_cannot_list_or_read(client: TestClient) -> None:
    mine, other = new_task(client), new_task(client)
    worker = register(client)
    assert claim(client, worker)["id"] == mine["id"]
    art = share(client, other)
    res = client.get(f"/api/tasks/{other['id']}/inputs", headers=worker)
    assert (res.status_code, code(res)) == (403, "not_leased")
    res = client.get(f"/api/artifacts/{art['id']}/content", headers=worker)
    assert (res.status_code, code(res)) == (403, "not_leased")
    # nor report on it, even naming its own attempt
    res = receipt(client, worker, other, art, 1, "materialized")
    assert (res.status_code, code(res)) == (403, "not_leased")


def test_unshared_attachment_is_attachment_scope_and_not_an_input(
    client: TestClient,
) -> None:
    task = new_task(client)
    private = attach(client, task, name="p.txt").json()
    worker = register(client)
    attempt = claim(client, worker)["attempt"]
    res = client.get(f"/api/artifacts/{private['id']}/content", headers=worker)
    assert (res.status_code, code(res)) == (403, "attachment_scope")
    view = client.get(f"/api/tasks/{task['id']}/inputs", headers=worker).json()
    assert view["inputs"] == []
    res = receipt(client, worker, task, private, attempt, "materialized")
    assert (res.status_code, code(res)) == (404, "input_not_found")


def test_unauthenticated_inputs_view_is_refused(client: TestClient) -> None:
    task = new_task(client)
    assert client.get(f"/api/tasks/{task['id']}/inputs").status_code in (401, 403)


# -- 3.3 receipts ----------------------------------------------------------------------


def test_receipts_record_states_and_are_idempotent(client: TestClient) -> None:
    task = new_task(client)
    body = "été\n".encode()
    art = share(client, task, body=body)
    worker = register(client)
    attempt = claim(client, worker)["attempt"]
    proof = {"sha256": sha(body), "size": len(body)}
    assert (
        receipt(client, worker, task, art, attempt, "materialized").status_code == 200
    )
    for _ in range(2):
        res = receipt(client, worker, task, art, attempt, "verified", **proof)
        assert res.status_code == 200, res.text
    res = receipt(client, worker, task, art, attempt, "consumed", **proof)
    states = [(r["attempt"], r["state"]) for r in res.json()["receipts"]]
    assert states == [
        (attempt, "materialized"),
        (attempt, "verified"),
        (attempt, "consumed"),
    ]
    assert all(r["reporter"].startswith("agent:") for r in res.json()["receipts"])
    events = client.get(
        "/api/events", params={"subject_id": task["id"], "limit": 200}, headers=ADMIN
    ).json()
    inputs = [e for e in events if e["kind"] == "task.input"]
    assert len(inputs) == 3  # the repeated `verified` was not written twice
    for e in inputs:
        assert set(e["data"]) == {"artifact_id", "attempt", "state"}
    assert "in.txt" not in str(inputs)
    view = client.get(f"/api/tasks/{task['id']}/inputs", headers=ADMIN).json()
    assert view["inputs"][0]["receipts"] == res.json()["receipts"]


def test_workers_see_receipt_events_without_names_or_hashes(client: TestClient) -> None:
    task = new_task(client)
    art = share(client, task, name="secret-name.txt", body=b"abc")
    worker = register(client)
    attempt = claim(client, worker)["attempt"]
    receipt(client, worker, task, art, attempt, "failed", reason="corrupt")
    feed = client.get("/api/events", params={"limit": 200}, headers=worker).json()
    inputs = [e for e in feed if e["kind"] == "task.input"]
    assert [e["data"] for e in inputs] == [
        {
            "artifact_id": art["id"],
            "attempt": attempt,
            "state": "failed",
            "reason": "corrupt",
        }
    ]
    text = str(feed)
    assert "secret-name.txt" not in text and sha(b"abc") not in text
    # another worker with no lease on the task sees none of it
    stranger = register(client)
    other = client.get("/api/events", params={"limit": 200}, headers=stranger).text
    assert "task.input" not in other


def test_failed_receipt_latest_reason_wins_and_repeats_are_dropped(
    client: TestClient,
) -> None:
    task = new_task(client)
    art = share(client, task)
    worker = register(client)
    attempt = claim(client, worker)["attempt"]
    for reason in ("unavailable", "unavailable", "limit"):
        res = receipt(client, worker, task, art, attempt, "failed", reason=reason)
        assert res.status_code == 200
    assert [r["reason"] for r in res.json()["receipts"]] == ["limit"]
    events = client.get(
        "/api/events", params={"subject_id": task["id"]}, headers=ADMIN
    ).json()
    assert len([e for e in events if e["kind"] == "task.input"]) == 2


@pytest.mark.parametrize(
    "body",
    [
        {"state": "verified"},  # needs sha256 and size
        {"state": "consumed", "size": 6},
        {"state": "failed"},  # needs a reason
        {"state": "failed", "reason": "something"},
        {"state": "materialized", "reason": "invalid"},
        {"state": "attached"},
        {"state": "verified", "size": 6, "sha256": "X" * 64},
        {"state": "materialized", "path": "/etc/passwd"},
    ],
)
def test_receipt_body_is_validated(client: TestClient, body: dict) -> None:
    task = new_task(client)
    art = share(client, task)
    worker = register(client)
    attempt = claim(client, worker)["attempt"]
    res = client.post(
        f"/api/tasks/{task['id']}/inputs/{art['id']}/receipt",
        json={"attempt": attempt, **body},
        headers=worker,
    )
    assert res.status_code == 422


@pytest.mark.parametrize(
    "proof", [{"size": 7}, {"sha256": "0" * 64}, {"size": 6, "sha256": "1" * 64}]
)
def test_receipt_cannot_claim_another_hash_or_size(
    client: TestClient, proof: dict
) -> None:
    task = new_task(client)
    art = share(client, task, body=b"input\n")
    worker = register(client)
    attempt = claim(client, worker)["attempt"]
    full = {"size": 6, "sha256": sha(b"input\n"), **proof}
    res = receipt(client, worker, task, art, attempt, "verified", **full)
    assert (res.status_code, code(res)) == (422, "integrity_mismatch")
    view = client.get(f"/api/tasks/{task['id']}/inputs", headers=ADMIN).json()
    assert view["inputs"][0]["receipts"] == []


def test_receipt_is_fenced_like_progress(client: TestClient, tmp_path: Path) -> None:
    task = new_task(client)
    art = share(client, task)
    first, second = register(client), register(client)
    attempt = claim(client, first)["attempt"]
    res = receipt(client, first, task, art, attempt + 1, "materialized")
    assert (res.status_code, code(res)) == (409, "stale_attempt")
    # an admin is not an agent
    assert receipt(client, ADMIN, task, art, attempt, "materialized").status_code == 403
    # an expired lease refuses the next write
    db_exec(
        tmp_path,
        "UPDATE tasks SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
        (task["id"],),
    )
    res = receipt(client, first, task, art, attempt, "materialized")
    assert (res.status_code, code(res)) == (409, "lease_expired")
    db_exec(
        tmp_path,
        "UPDATE tasks SET lease_expires_at = '2999-01-01T00:00:00+00:00' WHERE id = ?",
        (task["id"],),
    )
    released = client.post(
        f"/api/tasks/{task['id']}/release", json={"reason": "handoff"}, headers=ADMIN
    )
    assert released.status_code == 200
    # a released holder keeps its read grant but may not write
    res = receipt(client, first, task, art, attempt, "materialized")
    assert res.status_code == 409
    nxt = claim(client, second)
    assert nxt["id"] == task["id"]
    res = receipt(client, first, task, art, nxt["attempt"], "materialized")
    assert (res.status_code, code(res)) == (409, "not_assignee")
    # the next attempt's worker discovers the same input, needing nothing else
    view = client.get(f"/api/tasks/{task['id']}/inputs", headers=second).json()
    assert [i["id"] for i in view["inputs"]] == [art["id"]]
    ok = receipt(client, second, task, art, nxt["attempt"], "materialized")
    assert ok.status_code == 200
    assert [r["attempt"] for r in ok.json()["receipts"]] == [nxt["attempt"]]


def test_receipt_for_another_tasks_input_is_404(client: TestClient) -> None:
    task, other = new_task(client), new_task(client)
    foreign = share(client, other)
    worker = register(client)
    attempt = claim(client, worker)["attempt"]
    res = receipt(client, worker, task, foreign, attempt, "materialized")
    assert (res.status_code, code(res)) == (404, "input_not_found")


# -- 3.6 required input reference ---------------------------------------------------


def patch_wip(client, task, ref: str, headers=ADMIN):
    cur = client.get(f"/api/tasks/{task['id']}/continuation", headers=ADMIN).json()
    return client.patch(
        f"/api/tasks/{task['id']}/continuation",
        json={
            "expected_revision": cur["revision"],
            "reason": "required input",
            "wip": {"summary": "recovered patch", "ref": ref},
        },
        headers=headers,
    )


def test_required_input_reference_is_accepted_when_valid(client: TestClient) -> None:
    task = new_task(client, ["task-inputs"])
    art = share(client, task, body=b"patch\n")
    ref = f"attachment:{art['id']}:{sha(b'patch\n')}"
    res = patch_wip(client, task, ref, headers=register(client, "orchestrator"))
    assert res.status_code == 200, res.text
    assert res.json()["continuation"]["wip"]["ref"] == ref
    # only a worker able to materialize it claims the task
    plain = register(client)
    assert client.post("/api/tasks/claim", json={}, headers=plain).status_code == 204


@pytest.mark.parametrize(
    ("case", "want"),
    [
        ("malformed", "invalid_input_ref"),
        ("upper", "invalid_input_ref"),
        ("unknown", "input_not_found"),
        ("other_task", "input_not_found"),
        ("unshared", "input_not_shared"),
        ("hash", "integrity_mismatch"),
        ("capability", "input_capability_required"),
    ],
)
def test_required_input_reference_is_validated(
    client: TestClient, case: str, want: str
) -> None:
    task = new_task(client, [] if case == "capability" else ["task-inputs"])
    other = new_task(client, ["task-inputs"])
    art = share(client, other if case == "other_task" else task, body=b"x")
    if case == "unshared":
        art = attach(client, task, name="p.txt", body=b"x").json()
    good = sha(b"x")
    ref = {
        "malformed": f"attachment:{art['id']}",
        "upper": f"attachment:{art['id']}:{good.upper()}",
        "unknown": f"attachment:00000000-0000-4000-8000-000000000000:{good}",
        "hash": f"attachment:{art['id']}:{'0' * 64}",
    }.get(case, f"attachment:{art['id']}:{good}")
    before = client.get(f"/api/tasks/{task['id']}/continuation", headers=ADMIN).json()
    res = patch_wip(client, task, ref)
    assert (res.status_code, code(res)) == (422, want)
    after = client.get(f"/api/tasks/{task['id']}/continuation", headers=ADMIN).json()
    assert after["revision"] == before["revision"]


def test_checkpoint_references_are_unchanged(client: TestClient) -> None:
    task = new_task(client)
    res = patch_wip(client, task, "checkpoint:abc123")
    assert res.status_code == 200
    assert res.json()["continuation"]["wip"]["ref"] == "checkpoint:abc123"


def test_workers_cannot_set_or_replace_a_required_input(client: TestClient) -> None:
    task = new_task(client, ["task-inputs"])
    art = share(client, task, body=b"x")
    worker = register(client)
    attempt = claim(client, worker, ["task-inputs"])["attempt"]
    url = f"/api/tasks/{task['id']}/observations"
    ref = f"attachment:{art['id']}:{sha(b'x')}"
    res = client.post(
        url,
        json={"attempt": attempt, "wip": {"summary": "s", "ref": ref}},
        headers=worker,
    )
    assert (res.status_code, code(res)) == (422, "input_ref_forbidden")
    assert patch_wip(client, task, ref).status_code == 200
    res = client.post(
        url,
        json={"attempt": attempt, "wip": {"summary": "s", "ref": "deadbeef"}},
        headers=worker,
    )
    assert (res.status_code, code(res)) == (422, "input_ref_forbidden")
    kept = client.get(f"/api/tasks/{task['id']}/continuation", headers=ADMIN).json()
    assert kept["wip"]["ref"] == ref
    # an observation without WIP still lands
    res = client.post(url, json={"attempt": attempt}, headers=worker)
    assert res.status_code == 200
