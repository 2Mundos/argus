"""A paired reviewer's verdict counts only when its executor ran (pre-merge independent review 3.4)."""

import json
import sqlite3
from collections.abc import Generator
from pathlib import Path

import aiosqlite
import pytest
from argus_api.main import app
from argus_api.tasks import independent_review as ir
from argus_core.execution_chains import PairedAlternative
from fastapi.testclient import TestClient

ADMIN_TOKEN = "test-admin-token-" + "e" * 32
HEAD = "a" * 40


def _paired(model: str = "gemini-4-hydrogen") -> dict:
    explicit = {"source": "explicit", "id": model}
    return {
        "id": "hydrogen",
        "mode": "paired",
        "runtime": "nanoclaw",
        "harness": {
            "adapter": "claude",
            "provider": "anthropic",
            "model": {"source": "explicit", "id": "claude-haiku-5-5"},
        },
        "executor": {"adapter": "agy", "provider": "google", "model": explicit},
    }


@pytest.fixture
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[tuple[TestClient, Path]]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        c.put("/api/projects/alpha", json={"name": "alpha"})
        yield c, tmp_path / "argus.db"


def _child(client: TestClient, db: Path, alternative: dict | None) -> str:
    parent = client.post("/api/tasks", json={"title": "p", "project": "alpha"}).json()
    res = client.post(
        "/api/tasks",
        json={
            "title": "r",
            "project": "alpha",
            "kind": "code_review",
            "parent_id": parent["id"],
            "input": {"task_id": parent["id"], "head_sha": HEAD},
        },
    )
    assert res.status_code == 201, res.text
    child = res.json()["id"]
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE tasks SET attempt = 1 WHERE id = ?", (child,))
    if alternative is not None:
        route = {
            "source": "review",
            "alternatives": [alternative],
            "review": {"selected": alternative["id"]},
        }
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE tasks SET execution_route = ? WHERE id = ?",
                (json.dumps(route), child),
            )
    return child


def _run(
    db: Path,
    child: str,
    *,
    layer: str,
    executor: str | None,
    provider: str,
    model: str,
    state: str = "succeeded",
) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO model_executions (id, task_id, key, attempt, layer, state,"
            " executor, provider, configured_model, recorded_at, updated_at)"
            " VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, '2026-01-01T00:00:00Z',"
            " '2026-01-01T00:00:00Z')",
            (
                f"{child}-{layer}-{state}",
                child,
                f"{layer}-{state}",
                layer,
                state,
                executor,
                provider,
                model,
            ),
        )


async def _void(db: Path, child: str) -> bool:
    async with aiosqlite.connect(db) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM tasks WHERE id = ?", (child,)) as cur:
            row = await cur.fetchone()
        assert row is not None
        return await ir.executor_not_run(conn, row)


def test_fixture_alternative_is_valid() -> None:
    PairedAlternative.model_validate(_paired())


async def test_matching_succeeded_delegate_is_accepted(env) -> None:
    client, db = env
    child = _child(client, db, _paired())
    _run(
        db,
        child,
        layer="harness",
        executor=None,
        provider="anthropic",
        model="claude-haiku-5-5",
    )
    _run(
        db,
        child,
        layer="delegate",
        executor="agy",
        provider="google",
        model="gemini-4-hydrogen",
    )
    assert await _void(db, child) is False


async def test_harness_only_is_void(env) -> None:
    client, db = env
    child = _child(client, db, _paired())
    _run(
        db,
        child,
        layer="harness",
        executor=None,
        provider="anthropic",
        model="claude-haiku-5-5",
    )
    assert await _void(db, child) is True


async def test_other_executor_model_or_state_is_void(env) -> None:
    client, db = env
    child = _child(client, db, _paired())
    _run(
        db,
        child,
        layer="delegate",
        executor="agy",
        provider="google",
        model="gemini-3-other",
    )
    assert await _void(db, child) is True
    other = _child(client, db, _paired())
    _run(
        db,
        other,
        layer="delegate",
        executor="codex",
        provider="openai",
        model="gemini-4-hydrogen",
    )
    assert await _void(db, other) is True
    failed = _child(client, db, _paired())
    _run(
        db,
        failed,
        layer="delegate",
        executor="agy",
        provider="google",
        model="gemini-4-hydrogen",
        state="failed",
    )
    assert await _void(db, failed) is True


async def test_delegate_of_an_earlier_attempt_is_void(env) -> None:
    client, db = env
    child = _child(client, db, _paired())
    _run(
        db,
        child,
        layer="delegate",
        executor="agy",
        provider="google",
        model="gemini-4-hydrogen",
    )
    assert await _void(db, child) is False
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE tasks SET attempt = 2 WHERE id = ?", (child,))
    assert await _void(db, child) is True


async def test_single_mode_review_is_unchanged(env) -> None:
    client, db = env
    child = _child(client, db, None)
    _run(db, child, layer="harness", executor=None, provider="openai", model="gpt-5.5")
    assert await _void(db, child) is False
