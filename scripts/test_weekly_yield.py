"""The weekly yield report: each metric hand-counted on a fixture database."""

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from argus_api.db import Database
from weekly_yield import main, report

SINCE, UNTIL = date(2026, 11, 4), date(2026, 11, 10)


def add_task(
    db: sqlite3.Connection, task_id: str, created: str, state: str, project: str
):
    db.execute(
        "INSERT INTO tasks (id, kind, title, spec, acceptance, input,"
        " required_capabilities, priority, state, requires_approval, review,"
        " lease_ttl_s, attempt, max_attempts, created_by, created_at, updated_at,"
        " project) VALUES (?, 'generic', 't', '', '[]', '{}', '[]', 0, ?, 0, 'none',"
        " 120, 1, 3, 'x', ?, ?, ?)",
        (task_id, state, created, created, project),
    )


def add_events(db: sqlite3.Connection, task_id: str, events: list[tuple[str, dict]]):
    db.executemany(
        "INSERT INTO events (at, actor, subject_type, subject_id, kind, data)"
        " VALUES ('2026-11-05T00:00:00.000000Z', 'a', 'task', ?, ?, ?)",
        [(task_id, kind, json.dumps(data)) for kind, data in events],
    )


def add_decisions(db: sqlite3.Connection, task_id: str, n: int):
    for i in range(n):
        db.execute(
            "INSERT INTO decisions (id, subject_type, subject_id, kind, question,"
            " audience, state, requested_by, created_at) VALUES (?, 'task', ?,"
            " 'question', 'q', 'any', 'open', 'a', '2026-11-05T00:00:00.000000Z')",
            (f"{task_id}-d{i}", task_id),
        )


CLAIM, DONE, FAIL = ("task.claim", {}), ("task.complete", {}), ("task.fail", {})
PR = ("task.progress", {"kind": "pr_opened", "data": {"url": "u/1"}})


@pytest.fixture
async def yield_db(tmp_path: Path) -> Path:
    path = tmp_path / "copy.db"
    await (await Database.open(path, readers=0)).close()
    db = sqlite3.connect(path)
    day = "2026-11-%sT%s.000000Z"
    # In window: 5 tasks; boundaries are 11-04 00:00:00 (in) and 11-11 00:00:00 (out).
    add_task(db, "a", day % ("04", "00:00:00"), "succeeded", "p")  # first-claim success
    add_task(
        db, "b", day % ("10", "23:59:59"), "succeeded", "p"
    )  # failed once, then done
    add_task(db, "c", day % ("06", "12:00:00"), "failed", "p")  # failed, env_ block
    add_task(db, "d", day % ("07", "12:00:00"), "cancelled", "p")  # never claimed
    add_task(
        db, "e", day % ("08", "12:00:00"), "ready", "p"
    )  # claim with no outcome yet
    # Out: before, after, and global.
    add_task(db, "before", day % ("03", "23:59:59"), "succeeded", "p")
    add_task(db, "after", day % ("11", "00:00:00"), "succeeded", "p")
    add_task(db, "glob", day % ("06", "12:00:00"), "succeeded", "global")
    add_events(db, "a", [CLAIM, PR, DONE])
    add_events(db, "b", [CLAIM, FAIL, CLAIM, DONE])
    add_events(db, "c", [CLAIM, ("task.block", {"reason": "env_oom"}), FAIL])
    add_events(db, "e", [CLAIM])
    add_events(db, "before", [CLAIM, FAIL])
    add_events(db, "glob", [CLAIM, FAIL])
    add_decisions(db, "a", 2)
    add_decisions(db, "b", 1)
    add_decisions(db, "c", 5)  # not succeeded: not counted
    add_decisions(db, "glob", 9)
    db.commit()
    db.close()
    return path


def run(path: Path, merged: dict | None = None):
    db = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    return report(db, SINCE, UNTIL, merged)


async def test_metrics_are_hand_counted(yield_db: Path) -> None:
    # a merged 90 min after creation (by PR URL), b 30 min after (by task id).
    r = run(yield_db, {"u/1": "2026-11-04T01:30:00Z", "b": "2026-11-11T00:29:59Z"})
    assert r["window"] == {"from": "2026-11-04", "to": "2026-11-10"}
    assert r["tasks"] == 5
    # a ok; b fails first; c fails; d never claimed; e in flight -> 1 of 3.
    assert r["first_claim_success"] == {
        "value": 0.333,
        "succeeded": 1,
        "denominator": 3,
    }
    # Merged 90 and 30 min: median 60, nearest-rank p90 is the larger.
    assert r["minutes_to_merged_pr"] == {
        "median": 60.0,
        "p90": 90.0,
        "denominator": 2,
        "merge_data": True,
    }
    # a and b succeeded; 2 + 1 decisions.
    assert r["decisions_per_succeeded_task"] == {
        "value": 1.5,
        "decisions": 3,
        "denominator": 2,
    }
    assert r["env_block_share"] == {"value": 0.2, "blocked": 1, "denominator": 5}


async def test_undefined_values_are_unknown_never_zero(
    yield_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    r = run(yield_db)  # no merge data
    assert r["minutes_to_merged_pr"]["median"] is None
    assert r["minutes_to_merged_pr"]["p90"] is None
    assert r["minutes_to_merged_pr"]["merge_data"] is False
    # A window with no tasks: every value is unknown.
    db = sqlite3.connect(f"file:{yield_db}?mode=ro&immutable=1", uri=True)
    empty = report(db, date(2026, 12, 1), date(2026, 12, 2), {})
    assert empty["tasks"] == 0
    assert empty["first_claim_success"]["value"] is None
    assert empty["decisions_per_succeeded_task"]["value"] is None
    assert empty["env_block_share"]["value"] is None
    assert empty["minutes_to_merged_pr"]["median"] is None
    merged = tmp_path / "merged.json"
    merged.write_text("{}")
    args = ["--db", str(yield_db), "--since", "2026-12-01", "--until", "2026-12-02"]
    assert main([*args, "--merged", str(merged)]) == 0
    out = capsys.readouterr().out
    assert out.count("unknown") == 5
    assert "0.0" not in out


async def test_cli_never_writes_the_database(
    yield_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = yield_db.read_bytes()
    args = ["--db", str(yield_db), "--since", "2026-11-04", "--until", "2026-11-10"]
    assert main([*args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["tasks"] == 5
    assert main(args) == 0
    assert "tasks: 5" in capsys.readouterr().out
    assert yield_db.read_bytes() == before
