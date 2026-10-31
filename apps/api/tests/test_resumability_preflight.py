"""Read-only task resumability preflight (docs/specs/task-resumability-preflight.spec.md)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from argus_api.main import app
from argus_core.resumability import classify_artifact
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[3]
AVAILABILITY_FIXTURE = (
    REPO_ROOT / "integrations/nanoclaw/fixtures/resumability/availability.json"
)

ADMIN_TOKEN = "test-admin-token-" + "r" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
LIMIT = 1 << 20


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ARGUS_ARTIFACT_MAX_BYTES", str(LIMIT))
    with TestClient(app) as c:
        yield c


def register(client: TestClient, role: str = "worker") -> dict[str, str]:
    boot = client.post(
        "/api/agents/bootstrap-tokens", json={"role": role}, headers=ADMIN
    ).json()
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    return {"Authorization": f"Bearer {reg.json()['token']}"}


def new_task(client: TestClient, **kw) -> dict:
    body = {"project": "global", "title": "resume-me", **kw}
    return client.post("/api/tasks", json=body, headers=ADMIN).json()


def share(client: TestClient, task: dict, name: str, body: bytes) -> dict:
    res = client.post(
        f"/api/tasks/{task['id']}/attachments",
        params={"name": name, "worker_readable": "true"},
        content=body,
        headers={**ADMIN, "Content-Type": "text/plain; charset=utf-8"},
    )
    assert res.status_code == 201, res.text
    return res.json()


def claim(
    client: TestClient, worker: dict[str, str], caps: list[str] | None = None
) -> dict:
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


def stored_file(tmp_path: Path, artifact: dict) -> Path:
    files = [
        p
        for p in (tmp_path / "artifacts").rglob("*")
        if p.is_file() and p.stat().st_size == artifact["size"]
    ]
    assert files, "expected a stored artifact file"
    # Prefer the path that matches the artifact name.
    for p in files:
        if p.name == artifact["name"]:
            return p
    return files[0]


def preflight(client: TestClient, task_id: str, headers=ADMIN, **params):
    return client.get(
        f"/api/tasks/{task_id}/resumability", headers=headers, params=params
    )


def snapshot_task(tmp_path: Path, task_id: str) -> tuple:
    row = db_exec(
        tmp_path,
        "SELECT updated_at, continuation, attempt FROM tasks WHERE id = ?",
        (task_id,),
    )[0]
    events = db_exec(
        tmp_path,
        "SELECT count(*) FROM events WHERE subject_id = ?",
        (task_id,),
    )[0][0]
    arts = db_exec(
        tmp_path,
        "SELECT count(*) FROM artifacts WHERE task_id = ?",
        (task_id,),
    )[0][0]
    cont = json.loads(row[1]) if row[1] else {}
    return row[0], cont.get("revision", 0), events, arts, row[2]


# -- read-only -----------------------------------------------------------------


def test_preflight_is_read_only(client: TestClient, tmp_path: Path) -> None:
    task = new_task(client, required_capabilities=["task-inputs"])
    art = share(client, task, "a.txt", b"hello-input\n")
    before = snapshot_task(tmp_path, task["id"])
    res = preflight(client, task["id"])
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["task_id"] == task["id"]
    assert any(i["artifact_id"] == art["id"] for i in body["inputs"])
    assert snapshot_task(tmp_path, task["id"]) == before
    kinds = {
        r[0]
        for r in db_exec(
            tmp_path,
            "SELECT kind FROM events WHERE subject_id = ? AND kind LIKE 'task.%'",
            (task["id"],),
        )
    }
    # Preflight must not write task.* events (attachment attach may have earlier ones).
    after_kinds = {
        r[0]
        for r in db_exec(
            tmp_path,
            "SELECT kind FROM events WHERE subject_id = ? AND kind LIKE 'task.%'",
            (task["id"],),
        )
    }
    assert after_kinds == kinds
    assert snapshot_task(tmp_path, task["id"])[2] == before[2]


# -- access --------------------------------------------------------------------


def test_access_boundaries(client: TestClient) -> None:
    task = new_task(client)
    w1, w2 = register(client), register(client)
    orch = register(client, "orchestrator")
    claimed = claim(client, w1)
    assert claimed["id"] == task["id"]

    assert preflight(client, task["id"], headers=w1).status_code == 200
    denied = preflight(client, task["id"], headers=w2)
    assert denied.status_code == 403 and code(denied) == "not_leased"
    assert preflight(client, task["id"], headers=orch).status_code == 200
    assert preflight(client, task["id"], headers=ADMIN).status_code == 200

    bare = TestClient(app)
    assert bare.get(f"/api/tasks/{task['id']}/resumability").status_code == 401
    assert preflight(client, "00000000-0000-0000-0000-000000000000").status_code == 404


# -- secrets -------------------------------------------------------------------


def test_secrets_are_redacted(client: TestClient, tmp_path: Path) -> None:
    secretish = (
        "Authorization: Bearer super-secret-token-value "
        "sk-abcdefghijklmnopqrstuvwxyz012345 "
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789 "
        "postgres://user:p@ssw0rd@host/db token=abc123secrettoken"
    )
    task = new_task(client, acceptance=["do the thing"])
    orch = register(client, "orchestrator")
    w = register(client)
    claimed = claim(client, w)
    # Plant secrets in continuation WIP + artifact note via orchestrator patch.
    patch = client.patch(
        f"/api/tasks/{task['id']}/continuation",
        json={
            "expected_revision": claimed["continuation"]["revision"],
            "reason": "seed",
            "wip": {"summary": secretish, "ref": "/tmp/workspace-path"},
            "artifacts": [
                {
                    "kind": "branch",
                    "ref": "feat/x",
                    "state": "local_only",
                    "note": secretish,
                }
            ],
        },
        headers=orch,
    )
    assert patch.status_code == 200, patch.text
    ask = client.post(
        f"/api/tasks/{task['id']}/ask",
        json={
            "attempt": claimed["attempt"],
            "question": f"use {secretish}?",
            "options": ["yes", "no"],
        },
        headers=w,
    )
    assert ask.status_code == 201, ask.text

    body = preflight(client, task["id"]).json()
    dumped = json.dumps(body)
    assert "super-secret-token-value" not in dumped
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in dumped
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in dumped
    assert "p@ssw0rd" not in dumped
    assert "abc123secrettoken" not in dumped
    assert "[redacted]" in dumped
    assert str(tmp_path / "artifacts") not in dumped
    assert "hello-input" not in dumped  # no content bytes from attachments


# -- availability --------------------------------------------------------------


def test_attachment_availability_cases(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = new_task(client, required_capabilities=["task-inputs"])
    body = b"exact-bytes-for-hash\n"
    art = share(client, task, "ok.txt", body)
    path = stored_file(tmp_path, art)

    ok = preflight(client, task["id"]).json()
    inp = next(i for i in ok["inputs"] if i["artifact_id"] == art["id"])
    assert inp["availability"] == "available"
    assert inp["evidence_class"] == "current_read"
    assert inp["consumer_path"] == "task_inputs_materializer"

    path.write_bytes(body[:-4])  # truncated
    trunc = preflight(client, task["id"]).json()
    assert (
        next(i for i in trunc["inputs"] if i["artifact_id"] == art["id"])[
            "availability"
        ]
        == "truncated"
    )

    flipped = bytearray(body)
    flipped[0] ^= 0xFF
    path.write_bytes(bytes(flipped))
    corrupt = preflight(client, task["id"]).json()
    assert (
        next(i for i in corrupt["inputs"] if i["artifact_id"] == art["id"])[
            "availability"
        ]
        == "corrupted"
    )

    path.unlink()
    missing = preflight(client, task["id"]).json()
    assert (
        next(i for i in missing["inputs"] if i["artifact_id"] == art["id"])[
            "availability"
        ]
        == "missing"
    )
    assert "artifact_missing" in missing["cause_codes"]

    # Recreate for inaccessible / unknown cases.
    art2 = share(client, task, "bound.txt", body)
    path2 = stored_file(tmp_path, art2)
    if os.geteuid() != 0:
        os.chmod(path2, 0o000)
        try:
            locked = preflight(client, task["id"]).json()
            got = next(i for i in locked["inputs"] if i["artifact_id"] == art2["id"])
            assert got["availability"] == "inaccessible"
        finally:
            os.chmod(path2, 0o644)

    monkeypatch.setattr("argus_api.tasks.routes.DEFAULT_READ_BOUND", 4)
    huge = share(client, task, "big.txt", b"x" * 64)
    unknown = preflight(client, task["id"]).json()
    got = next(i for i in unknown["inputs"] if i["artifact_id"] == huge["id"])
    assert got["availability"] == "unknown"
    assert got["evidence_class"] == "unknown"


def test_receipt_is_history_not_current_read(
    client: TestClient, tmp_path: Path
) -> None:
    task = new_task(client, required_capabilities=["task-inputs"])
    w = register(client)
    art = share(client, task, "in.txt", b"payload\n")
    claimed = claim(client, w, caps=["task-inputs"])
    rec = client.post(
        f"/api/tasks/{task['id']}/inputs/{art['id']}/receipt",
        json={
            "attempt": claimed["attempt"],
            "state": "verified",
            "sha256": art["sha256"],
            "size": art["size"],
        },
        headers=w,
    )
    assert rec.status_code == 200, rec.text
    stored_file(tmp_path, art).unlink()
    body = preflight(client, task["id"]).json()
    inp = next(i for i in body["inputs"] if i["artifact_id"] == art["id"])
    assert inp["availability"] == "missing"
    assert inp["evidence_class"] == "current_read"
    assert inp["receipts"]
    assert all(r["evidence_class"] == "receipt_only" for r in inp["receipts"])


# -- WIP -----------------------------------------------------------------------


def test_wip_published_local_checkpoint_and_attachment(
    client: TestClient, tmp_path: Path
) -> None:
    orch = register(client, "orchestrator")

    # published — WIP ref must be covered by a matching artifact
    t1 = new_task(client)
    client.patch(
        f"/api/tasks/{t1['id']}/continuation",
        json={
            "expected_revision": 0,
            "reason": "pr",
            "artifacts": [
                {
                    "kind": "branch",
                    "ref": "feature/x",
                    "state": "open",
                    "head_sha": "abc1234",
                },
                {
                    "kind": "pr",
                    "ref": "42",
                    "state": "open",
                    "head_sha": "abc1234",
                    "url": "https://example.test/pr/42",
                },
            ],
            "wip": {"summary": "on the PR", "ref": "feature/x"},
        },
        headers=orch,
    )
    pub = preflight(client, t1["id"]).json()
    assert pub["wip"]["kind"] == "published"
    assert pub["wip"]["transfer"] == "published_ref"

    # local_only
    t2 = new_task(client)
    client.patch(
        f"/api/tasks/{t2['id']}/continuation",
        json={
            "expected_revision": 0,
            "reason": "local",
            "wip": {"summary": "dirty tree", "ref": "/workspace/agent/scratch"},
        },
        headers=orch,
    )
    local = preflight(client, t2["id"]).json()
    assert local["wip"]["kind"] == "local_only"
    assert local["wip"]["transfer"] == "not_a_transfer"
    assert "wip_local_only" in local["cause_codes"]
    assert any("not a transfer" in (r.get("detail") or "") for r in local["reasons"])

    # checkpoint unknown
    t3 = new_task(client)
    cid = "11111111-1111-1111-1111-111111111111"
    csha = "a" * 64
    client.patch(
        f"/api/tasks/{t3['id']}/continuation",
        json={
            "expected_revision": 0,
            "reason": "ckpt",
            "wip": {
                "summary": "checkpointed",
                "ref": f"checkpoint:{cid}:{csha}",
            },
        },
        headers=orch,
    )
    ck = preflight(client, t3["id"]).json()
    assert ck["wip"]["kind"] == "unknown"
    assert "checkpoint_store_not_visible" in ck["cause_codes"]
    assert ck["wip"]["transfer"] == "not_a_transfer"

    # attachment WIP ref verified
    t4 = new_task(client, required_capabilities=["task-inputs"])
    art = share(client, t4, "wip.patch", b"diff content\n")
    client.patch(
        f"/api/tasks/{t4['id']}/continuation",
        json={
            "expected_revision": 0,
            "reason": "input",
            "wip": {
                "summary": "required input",
                "ref": f"attachment:{art['id']}:{art['sha256']}",
            },
        },
        headers=orch,
    )
    att = preflight(client, t4["id"]).json()
    assert att["wip"]["availability"] == "available"
    assert att["wip"]["transfer"] == "attachment_verified"
    assert att["wip"]["evidence_class"] == "current_read"


# -- staleness / decisions -----------------------------------------------------


def test_staleness_and_decision_changes(client: TestClient) -> None:
    task = new_task(client, acceptance=["ship it"])
    w = register(client)
    orch = register(client, "orchestrator")
    claimed = claim(client, w)
    ask = client.post(
        f"/api/tasks/{task['id']}/ask",
        json={
            "attempt": claimed["attempt"],
            "question": "which approach?",
            "options": ["A", "B"],
        },
        headers=w,
    ).json()
    # Answering releases/requeues typically; answer then re-claim to get attempt 2.
    client.post(
        f"/api/decisions/{ask['id']}/answer",
        json={"text": "A", "choice": "A"},
        headers=orch,
    )
    # Task may be ready again; claim next attempt.
    again = client.post("/api/tasks/claim", json={}, headers=w)
    if again.status_code == 200:
        second = again.json()
        stale_attempt = preflight(
            client, task["id"], expected_attempt=claimed["attempt"]
        ).json()
        assert stale_attempt["decision"] == "stale"
        assert "attempt_mismatch" in stale_attempt["cause_codes"]
        stale_ver = preflight(
            client, task["id"], expected_version="1999-01-01T00:00:00+00:00"
        ).json()
        assert stale_ver["decision"] == "stale"
        assert "stale_preflight" in stale_ver["cause_codes"]
        body = preflight(client, second["id"]).json()
        assert any(
            d["relative_to_attempt"] in ("at_or_after", "before", "unresolved")
            for d in body["decision_changes"]
        )
        assert body["decision_changes"]
    else:
        # If still blocked/in_review, assess on current task version/attempt.
        body = preflight(client, task["id"]).json()
        assert body["decision_changes"]
        stale_ver = preflight(
            client, task["id"], expected_version="1999-01-01T00:00:00+00:00"
        ).json()
        assert stale_ver["decision"] == "stale"


# -- caps / capability ---------------------------------------------------------


def test_response_caps_and_capability_missing(client: TestClient) -> None:
    orch = register(client, "orchestrator")
    task = new_task(client)  # no task-inputs capability
    arts = [share(client, task, f"f{i}.txt", f"body-{i}\n".encode()) for i in range(3)]
    # Many continuation artifacts to exercise the cap.
    client.patch(
        f"/api/tasks/{task['id']}/continuation",
        json={
            "expected_revision": 0,
            "reason": "many",
            "artifacts": [
                {"kind": "commit", "ref": f"{i:040x}", "state": "unknown"}
                for i in range(25)
            ],
        },
        headers=orch,
    )
    body = preflight(client, task["id"]).json()
    assert len(body["artifacts"]) <= 20
    assert len(body["reasons"]) <= 10
    assert len(body["next_steps"]) <= 5
    assert len(body["inputs"]) <= 20
    assert "input_capability_missing" in body["cause_codes"]
    for inp in body["inputs"]:
        assert inp["consumer_path"] == "none"
        assert inp["capability"] == "capability_not_required"
    assert {a["id"] for a in arts} == {i["artifact_id"] for i in body["inputs"]}


# -- required vs optional inputs -----------------------------------------------


def _seed_required_optional(
    client: TestClient, orch: dict[str, str]
) -> tuple[dict, dict, dict]:
    """Task with acceptance + branch so gaps do not force `unknown`."""
    task = new_task(
        client,
        required_capabilities=["task-inputs"],
        acceptance=["ship it"],
    )
    required = share(client, task, "required.txt", b"required-bytes\n")
    optional = share(client, task, "optional.txt", b"optional-bytes\n")
    patch = client.patch(
        f"/api/tasks/{task['id']}/continuation",
        json={
            "expected_revision": 0,
            "reason": "wip-input",
            "artifacts": [
                {
                    "kind": "branch",
                    "ref": "feature/ready",
                    "state": "open",
                    "head_sha": "abc1234",
                }
            ],
            "wip": {
                "summary": "required attachment",
                "ref": f"attachment:{required['id']}:{required['sha256']}",
            },
        },
        headers=orch,
    )
    assert patch.status_code == 200, patch.text
    return task, required, optional


def test_optional_missing_input_does_not_block(
    client: TestClient, tmp_path: Path
) -> None:
    """Optional shared input missing + required available → resumable."""
    orch = register(client, "orchestrator")
    task, required, optional = _seed_required_optional(client, orch)
    stored_file(tmp_path, optional).unlink()
    body = preflight(client, task["id"]).json()
    assert body["decision"] == "resumable"
    by_id = {i["artifact_id"]: i for i in body["inputs"]}
    assert by_id[required["id"]]["availability"] == "available"
    assert by_id[required["id"]]["required"] is True
    assert by_id[optional["id"]]["availability"] == "missing"
    assert by_id[optional["id"]]["required"] is False
    assert "artifact_missing" in body["cause_codes"]
    assert sum(1 for i in body["inputs"] if i["required"]) == 1


def test_required_missing_input_is_not_resumable(
    client: TestClient, tmp_path: Path
) -> None:
    """When the missing input is the WIP-required one → not_resumable."""
    orch = register(client, "orchestrator")
    task, required, optional = _seed_required_optional(client, orch)
    stored_file(tmp_path, required).unlink()
    body = preflight(client, task["id"]).json()
    assert body["decision"] == "not_resumable"
    by_id = {i["artifact_id"]: i for i in body["inputs"]}
    assert by_id[required["id"]]["availability"] == "missing"
    assert by_id[required["id"]]["required"] is True
    assert by_id[optional["id"]]["availability"] == "available"
    assert by_id[optional["id"]]["required"] is False


def test_wip_ref_must_match_covering_artifact(client: TestClient) -> None:
    """Unrelated PR does not cover a WIP branch ref; matching branch does."""
    orch = register(client, "orchestrator")

    t1 = new_task(client)
    client.patch(
        f"/api/tasks/{t1['id']}/continuation",
        json={
            "expected_revision": 0,
            "reason": "unrelated-pr",
            "artifacts": [
                {
                    "kind": "pr",
                    "ref": "99",
                    "state": "open",
                    "head_sha": "abc1234",
                    "url": "https://example.test/pr/99",
                }
            ],
            "wip": {"summary": "other branch", "ref": "feature/uncovered"},
        },
        headers=orch,
    )
    uncovered = preflight(client, t1["id"]).json()
    assert uncovered["wip"]["kind"] == "local_only"
    assert "wip_local_only" in uncovered["cause_codes"]

    t2 = new_task(client)
    client.patch(
        f"/api/tasks/{t2['id']}/continuation",
        json={
            "expected_revision": 0,
            "reason": "matching-branch",
            "artifacts": [
                {
                    "kind": "branch",
                    "ref": "feature/covered",
                    "state": "open",
                    "head_sha": "def5678",
                }
            ],
            "wip": {"summary": "same branch", "ref": "feature/covered"},
        },
        headers=orch,
    )
    covered = preflight(client, t2["id"]).json()
    assert covered["wip"]["kind"] == "published"
    assert covered["wip"]["transfer"] == "published_ref"


def test_worker_and_manager_responses_identical(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker and manager preflight bodies are byte-identical for the same task."""
    from datetime import UTC, datetime

    task = new_task(client, required_capabilities=["task-inputs"])
    share(client, task, "shared.txt", b"same-for-both\n")
    # Unshared attachment exists so manager list_inputs would compute a count
    # workers must never see — the preflight must not expose or diverge on it.
    res = client.post(
        f"/api/tasks/{task['id']}/attachments",
        params={"name": "private.txt", "worker_readable": "false"},
        content=b"managers-only\n",
        headers={**ADMIN, "Content-Type": "text/plain; charset=utf-8"},
    )
    assert res.status_code == 201, res.text
    w = register(client)
    claim(client, w, caps=["task-inputs"])

    frozen = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr("argus_api.tasks.routes.utcnow", lambda: frozen)
    worker_body = preflight(client, task["id"], headers=w).content
    manager_body = preflight(client, task["id"], headers=ADMIN).content
    assert worker_body == manager_body


# -- review follow-up #2 -------------------------------------------------------


def test_required_too_large_is_unknown(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Required input over the read bound → decision unknown (not not_resumable)."""
    orch = register(client, "orchestrator")
    task = new_task(
        client,
        required_capabilities=["task-inputs"],
        acceptance=["ship it"],
    )
    required = share(client, task, "big-required.txt", b"x" * 64)
    client.patch(
        f"/api/tasks/{task['id']}/continuation",
        json={
            "expected_revision": 0,
            "reason": "wip-input",
            "artifacts": [
                {
                    "kind": "branch",
                    "ref": "feature/ready",
                    "state": "open",
                    "head_sha": "abc1234",
                }
            ],
            "wip": {
                "summary": "required over bound",
                "ref": f"attachment:{required['id']}:{required['sha256']}",
            },
        },
        headers=orch,
    )
    monkeypatch.setattr("argus_api.tasks.routes.DEFAULT_READ_BOUND", 4)
    body = preflight(client, task["id"]).json()
    by_id = {i["artifact_id"]: i for i in body["inputs"]}
    assert by_id[required["id"]]["availability"] == "unknown"
    assert by_id[required["id"]]["required"] is True
    assert body["decision"] == "unknown"


def test_required_input_pinned_past_inputs_cap(client: TestClient) -> None:
    """A required attachment past the twentieth listed input still appears in inputs[]."""
    orch = register(client, "orchestrator")
    task = new_task(
        client,
        required_capabilities=["task-inputs"],
        acceptance=["ship it"],
    )
    for i in range(20):
        share(client, task, f"opt-{i:02d}.txt", f"optional-{i}\n".encode())
    required = share(client, task, "required-last.txt", b"required-pinned\n")
    client.patch(
        f"/api/tasks/{task['id']}/continuation",
        json={
            "expected_revision": 0,
            "reason": "pin-required",
            "artifacts": [
                {
                    "kind": "branch",
                    "ref": "feature/ready",
                    "state": "open",
                    "head_sha": "abc1234",
                }
            ],
            "wip": {
                "summary": "required past cap",
                "ref": f"attachment:{required['id']}:{required['sha256']}",
            },
        },
        headers=orch,
    )
    body = preflight(client, task["id"]).json()
    assert len(body["inputs"]) <= 20
    hit = next(i for i in body["inputs"] if i["artifact_id"] == required["id"])
    assert hit["required"] is True
    assert hit["availability"] == "available"


def test_required_input_load_failure_emits_assessment(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_artifact failure for a required input still yields an InputAssessment."""
    orch = register(client, "orchestrator")
    task, required, _optional = _seed_required_optional(client, orch)
    real_get = __import__(
        "argus_api.artifacts.service", fromlist=["get_artifact"]
    ).get_artifact

    async def boom(conn, artifact_id: str):
        if artifact_id == required["id"]:
            raise RuntimeError("simulated row load failure")
        return await real_get(conn, artifact_id)

    monkeypatch.setattr("argus_api.tasks.resumability.get_artifact", boom)
    body = preflight(client, task["id"]).json()
    hit = next(i for i in body["inputs"] if i["artifact_id"] == required["id"])
    assert hit["required"] is True
    assert hit["availability"] == "unknown"
    assert hit["evidence_class"] == "unobserved"
    assert any("artifact_row_unreadable" in u for u in body["unknowns"])
    assert body["decision"] == "unknown"


def test_wip_missing_attachment_row_is_unobserved(
    client: TestClient, tmp_path: Path
) -> None:
    """WIP attachment:<id> with no row must not claim current_read."""
    orch = register(client, "orchestrator")
    task = new_task(
        client,
        required_capabilities=["task-inputs"],
        acceptance=["ship it"],
    )
    art = share(client, task, "gone.txt", b"will-delete-row\n")
    patch = client.patch(
        f"/api/tasks/{task['id']}/continuation",
        json={
            "expected_revision": 0,
            "reason": "missing-row",
            "artifacts": [
                {
                    "kind": "branch",
                    "ref": "feature/ready",
                    "state": "open",
                    "head_sha": "abc1234",
                }
            ],
            "wip": {
                "summary": "points at nothing",
                "ref": f"attachment:{art['id']}:{art['sha256']}",
            },
        },
        headers=orch,
    )
    assert patch.status_code == 200, patch.text
    # Drop the artifact row (and store file) so the WIP ref has no matching row.
    for p in (tmp_path / "artifacts").rglob("*"):
        if p.is_file() and p.name == art["name"]:
            p.unlink()
            break
    db_exec(tmp_path, "DELETE FROM artifacts WHERE id = ?", (art["id"],))
    body = preflight(client, task["id"]).json()
    assert body["wip"]["availability"] == "unknown"
    assert body["wip"]["evidence_class"] == "unobserved"
    assert "artifact_unknown" in body["cause_codes"]
    hit = next(i for i in body["inputs"] if i["artifact_id"] == art["id"])
    assert hit["required"] is True
    assert hit["evidence_class"] == "unobserved"
    assert body["decision"] == "unknown"


def test_optional_unknown_does_not_decide(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Optional input over the bound + required available → not unknown from that."""
    orch = register(client, "orchestrator")
    monkeypatch.setattr("argus_api.tasks.routes.DEFAULT_READ_BOUND", 8)
    task = new_task(
        client,
        required_capabilities=["task-inputs"],
        acceptance=["ship it"],
    )
    required = share(client, task, "req.txt", b"tiny")
    optional = share(client, task, "opt-big.txt", b"z" * 64)
    client.patch(
        f"/api/tasks/{task['id']}/continuation",
        json={
            "expected_revision": 0,
            "reason": "optional-unknown",
            "artifacts": [
                {
                    "kind": "branch",
                    "ref": "feature/ready",
                    "state": "open",
                    "head_sha": "abc1234",
                }
            ],
            "wip": {
                "summary": "small required",
                "ref": f"attachment:{required['id']}:{required['sha256']}",
            },
        },
        headers=orch,
    )
    body = preflight(client, task["id"]).json()
    by_id = {i["artifact_id"]: i for i in body["inputs"]}
    assert by_id[required["id"]]["availability"] == "available"
    assert by_id[optional["id"]]["availability"] == "unknown"
    assert by_id[optional["id"]]["required"] is False
    assert "artifact_unknown" in body["cause_codes"]
    assert body["decision"] == "resumable"


def test_artifacts_budget_keeps_attachments(client: TestClient) -> None:
    """Continuation artifacts must not hide all attachments from artifacts[]."""
    orch = register(client, "orchestrator")
    task = new_task(client, required_capabilities=["task-inputs"])
    arts = [share(client, task, f"a{i}.txt", f"body-{i}\n".encode()) for i in range(3)]
    client.patch(
        f"/api/tasks/{task['id']}/continuation",
        json={
            "expected_revision": 0,
            "reason": "fill-continuation",
            "artifacts": [
                {"kind": "commit", "ref": f"{i:040x}", "state": "unknown"}
                for i in range(20)
            ],
        },
        headers=orch,
    )
    body = preflight(client, task["id"]).json()
    assert len(body["artifacts"]) <= 20
    cont = [a for a in body["artifacts"] if a["kind"] != "attachment"]
    atts = [a for a in body["artifacts"] if a["kind"] == "attachment"]
    assert len(cont) <= 15
    assert len(atts) >= 1
    assert {a["id"] for a in arts} == {i["artifact_id"] for i in body["inputs"]}


def test_interruption_and_observed_after_attempt(client: TestClient) -> None:
    """Report reuses interruption and marks continuation artifacts vs attempt."""
    orch = register(client, "orchestrator")
    w = register(client)
    task = new_task(client, acceptance=["ship it"])
    claimed = claim(client, w)
    client.patch(
        f"/api/tasks/{task['id']}/continuation",
        json={
            "expected_revision": claimed["continuation"]["revision"],
            "reason": "seed-art",
            "artifacts": [
                {
                    "kind": "branch",
                    "ref": "feature/x",
                    "state": "open",
                    "head_sha": "abc1234",
                }
            ],
        },
        headers=orch,
    )
    # Release creates an interruption on the continuation.
    rel = client.post(
        f"/api/tasks/{task['id']}/release",
        json={"reason": "recycle", "attempt": claimed["attempt"]},
        headers=ADMIN,
    )
    assert rel.status_code == 200, rel.text
    body = preflight(client, task["id"]).json()
    assert body["interruption"] is not None
    assert "attempt" in body["interruption"]
    assert "reason" in body["interruption"]
    assert "at" in body["interruption"]
    cont_arts = [a for a in body["artifacts"] if a["kind"] == "branch"]
    assert cont_arts
    assert "observed_after_attempt_start" in cont_arts[0]


def test_live_lease_reason_names_holder(client: TestClient) -> None:
    w = register(client)
    task = new_task(client)
    claimed = claim(client, w)
    body = preflight(client, task["id"]).json()
    assert "lease_held_elsewhere" in body["cause_codes"]
    detail = next(
        r["detail"] for r in body["reasons"] if r["code"] == "lease_held_elsewhere"
    )
    assert "a live lease is held by" in detail
    assert claimed["assignee_agent_id"] in detail or "agent:" in str(
        body["lease"]["assignee_agent_id"]
    )


def test_reasons_prefer_required_when_truncated(
    client: TestClient, tmp_path: Path
) -> None:
    """When many reasons exist, a required-input failure is kept under the cap."""
    orch = register(client, "orchestrator")
    task = new_task(
        client,
        required_capabilities=["task-inputs"],
        acceptance=["ship it"],
    )
    required = share(client, task, "need.txt", b"need\n")
    # Many optional missing + many unobserved continuation artifacts → >10 reasons.
    for i in range(8):
        art = share(client, task, f"opt{i}.txt", f"o{i}\n".encode())
        stored_file(tmp_path, art).unlink()
    client.patch(
        f"/api/tasks/{task['id']}/continuation",
        json={
            "expected_revision": 0,
            "reason": "many-reasons",
            "artifacts": [
                {"kind": "commit", "ref": f"{i:040x}", "state": "unknown"}
                for i in range(12)
            ],
            "wip": {
                "summary": "required gone",
                "ref": f"attachment:{required['id']}:{required['sha256']}",
            },
        },
        headers=orch,
    )
    stored_file(tmp_path, required).unlink()
    body = preflight(client, task["id"]).json()
    assert len(body["reasons"]) <= 10
    assert any(
        r["code"] == "artifact_missing" and required["id"] in r["subject"]
        for r in body["reasons"]
    )
    assert body["decision"] == "not_resumable"


# -- NanoClaw availability fixture (producer; vitest consumes) -----------------


def _availability_cases() -> list[dict[str, Any]]:
    """Real classify_artifact outputs for every store-read case the materializer covers."""
    recorded_size = 16
    recorded_sha = sha(b"0123456789abcdef")
    cases: list[tuple[str, dict[str, Any]]] = [
        (
            "available",
            {
                "recorded_size": recorded_size,
                "recorded_sha256": recorded_sha,
                "observed_size": recorded_size,
                "observed_sha256": recorded_sha,
                "error": None,
            },
        ),
        (
            "missing",
            {
                "recorded_size": recorded_size,
                "recorded_sha256": recorded_sha,
                "observed_size": None,
                "observed_sha256": None,
                "error": "missing",
            },
        ),
        (
            "truncated",
            {
                "recorded_size": recorded_size,
                "recorded_sha256": recorded_sha,
                "observed_size": recorded_size - 5,
                "observed_sha256": None,
                "error": None,
            },
        ),
        (
            "corrupted",
            {
                "recorded_size": recorded_size,
                "recorded_sha256": recorded_sha,
                "observed_size": recorded_size,
                "observed_sha256": sha(b"x123456789abcdef"),
                "error": None,
            },
        ),
        (
            "inaccessible_permission",
            {
                "recorded_size": recorded_size,
                "recorded_sha256": recorded_sha,
                "observed_size": None,
                "observed_sha256": None,
                "error": "permission",
            },
        ),
        (
            "is_directory",
            {
                "recorded_size": recorded_size,
                "recorded_sha256": recorded_sha,
                "observed_size": None,
                "observed_sha256": None,
                "error": "is_directory",
            },
        ),
        (
            "too_large",
            {
                "recorded_size": recorded_size,
                "recorded_sha256": recorded_sha,
                "observed_size": None,
                "observed_sha256": None,
                "error": "too_large",
            },
        ),
        (
            "os_error",
            {
                "recorded_size": recorded_size,
                "recorded_sha256": recorded_sha,
                "observed_size": None,
                "observed_sha256": None,
                "error": "os_error",
            },
        ),
    ]
    out: list[dict[str, Any]] = []
    for name, kwargs in cases:
        avail, ev, cause = classify_artifact(**kwargs)
        out.append(
            {
                "case": name,
                **kwargs,
                "availability": avail.value,
                "evidence_class": ev.value,
                "cause": cause.value if cause is not None else None,
            }
        )
    out.sort(key=lambda c: c["case"])
    return out


def _write_availability_fixture(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, dir=path.parent
    ) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    subprocess.run(
        ["pnpm", "exec", "biome", "check", "--write", str(tmp_path)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    tmp_path.replace(path)


def test_availability_fixture_matches_classify_artifact() -> None:
    """Producer for integrations/nanoclaw/fixtures/resumability/availability.json.

    Regenerated with ARGUS_UPDATE_FIXTURES=1; otherwise CI fails on drift.
    """
    payload = {"cases": _availability_cases()}
    if os.environ.get("ARGUS_UPDATE_FIXTURES") == "1":
        _write_availability_fixture(AVAILABILITY_FIXTURE, payload)
        return
    assert AVAILABILITY_FIXTURE.is_file(), (
        f"{AVAILABILITY_FIXTURE} missing. "
        "Re-run with ARGUS_UPDATE_FIXTURES=1 to create it."
    )
    on_disk = json.loads(AVAILABILITY_FIXTURE.read_text())
    assert payload == on_disk, (
        f"{AVAILABILITY_FIXTURE} drifted from classify_artifact. "
        "Re-run with ARGUS_UPDATE_FIXTURES=1 if the change is intentional."
    )
