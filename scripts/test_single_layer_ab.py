"""The single-layer A/B tool: assignment, export, and counting rules.

The fixtures are synthetic; a figure computed here is not an experiment result.
"""

import json
import sqlite3
from collections import Counter
from pathlib import Path

import pytest
from argus_api.db import Database
from single_layer_ab import analyze, assign, assignment_doc, export, main, select

FIXTURES = Path(__file__).parent / "fixtures" / "single-layer-ab"


def candidates(n: int) -> list[dict]:
    levels = ["simple", "standard", "standard", "hard"]
    return [{"id": f"t{i:02d}", "difficulty": levels[i % 4]} for i in range(n)]


def test_assignment_is_deterministic_stratified_and_balanced() -> None:
    pool = candidates(40)
    doc = assignment_doc(pool, 20, "s1", "p7-a")
    assert doc == assignment_doc(list(reversed(pool)), 20, "s1", "p7-a")
    assert doc["engine"] == "claude"
    assert len(doc["arms"]) == 20
    # Strata keep their share of the pool: 10 simple, 20 standard, 10 hard of 40.
    assert Counter(doc["strata"].values()) == {"simple": 5, "standard": 10, "hard": 5}
    for level in ("simple", "standard", "hard"):
        arms = Counter(a for t, a in doc["arms"].items() if doc["strata"][t] == level)
        assert abs(arms["single_layer"] - arms["delegated"]) <= 1
    assert assignment_doc(pool, 20, "s2", "p7-a")["arms"] != doc["arms"]


def test_select_keeps_every_candidate_when_the_pool_is_small() -> None:
    pool = candidates(6)
    assert select(pool, 20, "s", "p7") == pool
    assert set(assign(pool, "s", "p7").values()) == {"single_layer", "delegated"}


@pytest.mark.parametrize(
    ("pool", "experiment", "error"),
    [
        (candidates(4), "bad id", "invalid experiment id"),
        ([{"id": "t1"}, {"id": "t1"}], "p7", "not unique"),
    ],
)
def test_assignment_rejects_bad_input(pool: list, experiment: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        assignment_doc(pool, 20, "s", experiment)


def test_analyze_counts_unknown_usage_fallbacks_and_censoring() -> None:
    attempts = [
        json.loads(line)
        for line in (FIXTURES / "attempts.jsonl").read_text().split("\n")
        if line
    ]
    assignment = json.loads((FIXTURES / "assignment.json").read_text())
    merged = json.loads((FIXTURES / "merged.json").read_text())
    report = analyze(attempts, assignment, merged)
    assert report["balance"] == {
        "hard": {"single_layer": 1, "delegated": 1},
        "standard": {"single_layer": 2, "delegated": 2},
    }
    sl, dl = report["arms"]["single_layer"], report["arms"]["delegated"]
    assert (sl["tasks_finished"], sl["tasks_succeeded"], sl["task_success_rate"]) == (
        3,
        2,
        0.667,
    )
    assert (sl["attempts"], sl["attempts_completed"], sl["attempt_success_rate"]) == (
        4,
        2,
        0.5,
    )
    # The fallback attempt stays in its assigned arm and is counted.
    assert (sl["attempts_off_arm"], sl["fallbacks"]) == (1, {"engine_not_claude": 1})
    # s2's second attempt has no usage: s2 is unknown, not 400 tokens.
    assert sl["tokens_per_succeeded_task"] == {
        "n": 1,
        "median": 1000.0,
        "mean": 1000.0,
        "unknown": 1,
    }
    assert sl["hours_created_to_pr_opened"] == {
        "n": 2,
        "median": 2.0,
        "mean": 2.0,
        "censored": 1,
        "median_biased_low": True,
    }
    assert sl["hours_created_to_merged"]["n"] == 1
    assert sl["hours_created_to_merged"]["censored"] == 2
    # The in-flight attempt is neither a success nor a failure.
    assert (dl["tasks_finished"], dl["attempts"], dl["attempts_ended"]) == (2, 3, 2)
    assert dl["task_success_rate"] == 1.0
    assert dl["cost_usd_per_succeeded_task"]["unknown"] == 1
    assert dl["hours_created_to_merged"]["median"] == 5.0
    # Without forge data there is no merge time at all.
    unmerged = analyze(attempts, assignment)["arms"]["delegated"]
    assert unmerged["hours_created_to_merged"] == {
        "n": 0,
        "median": None,
        "mean": None,
        "censored": 3,
        "median_biased_low": True,
    }


T0 = "2026-11-01T10:00:00.000000Z"


@pytest.fixture
async def experiment_db(tmp_path: Path) -> Path:
    path = tmp_path / "copy.db"
    await (await Database.open(path, readers=0)).close()
    db = sqlite3.connect(path)
    db.execute(
        "INSERT INTO tasks (id, kind, title, spec, acceptance, input,"
        " required_capabilities, priority, state, requires_approval, review,"
        " lease_ttl_s, attempt, max_attempts, created_by, created_at, updated_at,"
        " project, difficulty) VALUES ('t1', 'generic', 't', '', '[]', '{}', '[]', 0,"
        " 'succeeded', 0, 'none', 120, 1, 3, 'x', ?, ?, 'global', 'hard')",
        (T0, T0),
    )
    # A retry restarted `attempt`: two claims numbered 1, told apart by their windows.
    db.executemany(
        "INSERT INTO task_attempts (task_id, attempt, agent_id, claimed_at, definition)"
        " VALUES ('t1', 1, 'ag', ?, '{}')",
        [("2026-11-01T10:01:00.000000Z",), ("2026-11-01T11:00:00.000000Z",)],
    )
    events = [
        ("2026-11-01T10:30:00.000000Z", "task.fail", {"from": "claimed", "attempt": 1}),
        (
            "2026-11-01T11:20:00.000000Z",
            "task.progress",
            {"attempt": 1, "kind": "pr_opened", "data": {"number": 7, "url": "u/7"}},
        ),
        ("2026-11-01T11:30:00.000000Z", "task.complete", {"from": "claimed"}),
    ]
    db.executemany(
        "INSERT INTO events (at, actor, subject_type, subject_id, kind, data)"
        " VALUES (?, 'agent:ag', 'task', 't1', ?, ?)",
        [(at, kind, json.dumps(data)) for at, kind, data in events],
    )
    selections = [
        (
            "k1",
            "2026-11-01T10:02:00.000000Z",
            "single-layer experiment p7: arm=single_layer",
        ),
        (
            "k2",
            "2026-11-01T11:01:00.000000Z",
            "single-layer experiment p7: arm=delegated"
            " assigned=single_layer fallback=engine_not_claude",
        ),
        (
            "k3",
            "2026-11-01T11:02:00.000000Z",
            "single-layer experiment other: arm=single_layer",
        ),
    ]
    db.executemany(
        "INSERT INTO model_selections (id, task_id, key, attempt, layer, kind,"
        " reason_code, explanation, at, recorded_at)"
        " VALUES (?, 't1', ?, 1, 'harness', 'initial', 'policy', ?, ?, ?)",
        [(k, k, e, at, at) for k, at, e in selections],
    )
    db.executemany(
        "INSERT INTO account_usage (task_id, attempt, input_tokens, output_tokens,"
        " cost_usd, at, measurement, unmeasured_reason)"
        " VALUES ('t1', 1, ?, ?, ?, ?, ?, ?)",
        [
            (100, 20, 0.3, "2026-11-01T10:30:00.000000Z", "measured", None),
            (None, None, None, "2026-11-01T11:30:00.000000Z", "not_reported", "gone"),
        ],
    )
    db.commit()
    db.close()
    return path


async def test_export_reads_each_claim_window(
    experiment_db: Path, tmp_path: Path
) -> None:
    assignment = tmp_path / "a.json"
    assignment.write_text(
        json.dumps(
            {"experiment": "p7", "arms": {"t1": "single_layer", "gone": "delegated"}}
        )
    )
    out = tmp_path / "attempts.jsonl"
    args = ["export", "--db", str(experiment_db), "--assignment", str(assignment)]
    assert main([*args, "-o", str(out)]) == 0
    first, second = (json.loads(line) for line in out.read_text().splitlines())
    assert first == {
        "task_id": "t1",
        "attempt": 1,
        "claim": 1,
        "task_state": "succeeded",
        "difficulty": "hard",
        "created_at": T0,
        "claimed_at": "2026-11-01T10:01:00.000000Z",
        "ended": "task.fail",
        "arm": "single_layer",
        "fallback": None,
        "tokens": 120,
        "cost_usd": 0.3,
        "pr_opened_at": None,
        "pr_url": None,
    }
    assert (second["ended"], second["arm"], second["fallback"]) == (
        "task.complete",
        "delegated",
        "engine_not_claude",
    )
    assert (second["tokens"], second["cost_usd"]) == (None, None)  # not_reported
    assert (second["pr_opened_at"], second["pr_url"]) == (
        "2026-11-01T11:20:00.000000Z",
        "u/7",
    )
    # Each experiment reads only its own marker.
    db = sqlite3.connect(f"file:{experiment_db}?mode=ro", uri=True)
    other = export(db, {"experiment": "other", "arms": {"t1": "single_layer"}})
    db.close()
    assert [r["arm"] for r in other] == [None, "single_layer"]
