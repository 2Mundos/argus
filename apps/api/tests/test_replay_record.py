"""Recording an attempt as a replay fixture: redaction and relative timestamps."""

import json
import sqlite3
from pathlib import Path

import pytest
from argus_api.db import Database
from argus_api.replay import fixture
from record_attempt import main, record

T0 = "2026-01-01T10:00:00.000000Z"


def test_clean_text_redacts_secrets_and_absolute_paths() -> None:
    token = "ghp_" + "a" * 24
    assert fixture.clean_text(f"push with {token}") == "push with [redacted]"
    assert fixture.clean_text("Authorization: Bearer abcdefgh12345678") == (
        "Authorization: [redacted]"
    )
    assert fixture.clean_text("API_TOKEN=hunter2hunter2") == "API_TOKEN=[redacted]"
    assert (
        fixture.clean_text("edit /home/alice/work/repo/app.py:12 and C:\\Users\\bob\\x")
        == "edit <path> and <path>"
    )
    assert fixture.clean_text("a/b/c.py and https://x.test/home") == (
        "a/b/c.py and https://x.test/home"
    )
    assert fixture.clean({"k": ["/tmp/x", {"n": 1, "s": "sk-" + "b" * 20}]}) == {
        "k": ["<path>", {"n": 1, "s": "[redacted]"}]
    }


def test_offsets_are_relative_to_the_first_step() -> None:
    assert fixture.offsets(
        ["2026-01-01T10:00:02.5Z", "2026-01-01T10:00:00Z", "2026-01-01T10:01:00Z"]
    ) == [2.5, 0.0, 60.0]
    built = fixture.build(
        [
            ("2026-01-01T10:00:05Z", {"type": "log", "source": "worker", "line": "b"}),
            ("2026-01-01T10:00:01Z", {"type": "log", "source": "worker", "line": "a"}),
        ],
        {"outcome": "fail", "error": "boom"},
    )
    assert [(s["t"], s["line"]) for s in built["steps"]] == [(0.0, "a"), (4.0, "b")]


@pytest.fixture
async def attempt_db(tmp_path: Path) -> Path:
    path = tmp_path / "copy.db"
    await (await Database.open(path, readers=0)).close()
    db = sqlite3.connect(path)
    secret = "ghp_" + "c" * 24
    db.execute(
        "INSERT INTO tasks (id, kind, title, spec, acceptance, input,"
        " required_capabilities, priority, state, requires_approval, review,"
        " lease_ttl_s, attempt, max_attempts, result, created_by, created_at,"
        " updated_at, project) VALUES ('t1', 'generic', 't', '', '[]', '{}', '[]', 0,"
        " 'succeeded', 0, 'none', 120, 2, 3, ?, 'x', ?, ?, 'global')",
        (
            json.dumps(
                {
                    "summary": f"done in /home/bob/repo with {secret}",
                    "data": None,
                    "artifacts": ["a1"],
                    "usage": {"account_id": "acct", "input_tokens": 5},
                }
            ),
            T0,
            T0,
        ),
    )
    db.executemany(
        "INSERT INTO task_attempts (task_id, attempt, agent_id, claimed_at, definition)"
        " VALUES ('t1', ?, 'ag', ?, '{}')",
        [(1, "2026-01-01T09:00:00.000000Z"), (2, T0)],
    )
    db.executemany(
        "INSERT INTO task_log (task_id, attempt, seq, source, at, line)"
        " VALUES ('t1', ?, ?, 'worker', ?, ?)",
        [
            (1, 1, "2026-01-01T09:00:03.000000Z", "an earlier attempt"),
            (2, 2, "2026-01-01T10:00:04.250000Z", f"open /home/bob/x {secret}"),
        ],
    )
    db.executemany(
        "INSERT INTO events (at, actor, subject_type, subject_id, kind, data)"
        " VALUES (?, 'agent:ag', 'task', 't1', ?, ?)",
        [
            (T0, "task.claimed", "{}"),
            (
                "2026-01-01T10:00:01.500000Z",
                "task.progress",
                json.dumps({"attempt": 2, "message": "half", "percent": 50}),
            ),
        ],
    )
    db.commit()
    db.close()
    return path


async def test_record_reads_one_attempt_with_relative_clean_steps(
    attempt_db: Path,
) -> None:
    db = sqlite3.connect(f"file:{attempt_db}?mode=ro", uri=True)
    got = record(db, "t1", 2)
    db.close()

    assert [(s["t"], s["type"]) for s in got["steps"]] == [
        (0.0, "event"),
        (1.5, "progress"),
        (4.25, "log"),
    ]
    assert got["steps"][2]["line"] == "open <path> [redacted]"
    assert got["final"] == {
        "outcome": "complete",
        "result": {
            "summary": "done in <path> with [redacted]",
            "data": None,
            "input_tokens": 5,
        },
    }
    assert "acct" not in json.dumps(got)


async def test_main_writes_a_fixture_the_player_can_load(
    attempt_db: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out.json"
    assert main(["t1", "2", "--db", str(attempt_db), "-o", str(out)]) == 0
    assert fixture.load(out)["steps"][-1]["type"] == "log"
    with pytest.raises(SystemExit, match="latest attempt"):
        main(["t1", "1", "--db", str(attempt_db)])
