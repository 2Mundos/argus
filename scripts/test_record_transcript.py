"""The transcript recorder, on a synthetic delegate log."""

import json
import sqlite3
from pathlib import Path

import pytest
from record_transcript import MAX_BYTES, main, render, trim


def make_db(path: Path, lines: list[tuple[str, int, str, str]]) -> Path:
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE task_log (task_id TEXT, attempt INTEGER, seq INTEGER,"
        " source TEXT, at TEXT, line TEXT)"
    )
    db.executemany(
        "INSERT INTO task_log VALUES (?, ?, ?, ?, '2026-11-05T00:00:00Z', ?)",
        [(t, a, i, s, line) for i, (t, a, s, line) in enumerate(lines)],
    )
    db.commit()
    db.close()
    return path


def test_trim_cuts_long_strings_and_keeps_structure() -> None:
    event = {"type": "user", "content": [{"type": "tool_result", "content": "x" * 5000}]}
    out = trim(event, 100)
    assert out["type"] == "user"
    assert out["content"][0]["type"] == "tool_result"
    assert out["content"][0]["content"].startswith("x" * 100 + "…")
    assert "4900 chars trimmed" in out["content"][0]["content"]
    assert trim({"short": "ok", "n": 3}, 100) == {"short": "ok", "n": 3}


def test_render_shrinks_the_cap_until_the_file_fits() -> None:
    big = json.dumps({"type": "user", "out": "y" * 3000})
    text = render([big] * 120)  # ~360 KB at the first cap
    assert len(text.encode()) <= MAX_BYTES
    assert len(text.splitlines()) == 120
    assert "chars trimmed" in text


def test_main_writes_the_attempt_events_only(tmp_path: Path) -> None:
    db = make_db(
        tmp_path / "a.db",
        [
            ("t1", 1, "delegate", json.dumps({"type": "system", "subtype": "init"})),
            ("t1", 1, "worker", "worker noise"),
            ("t1", 2, "delegate", json.dumps({"type": "other attempt"})),
            ("t1", 1, "delegate", json.dumps({"type": "user", "out": "z" * 9000})),
            ("t1", 1, "delegate", "plain stderr line"),
        ],
    )
    root = tmp_path / "transcripts"
    assert main(["t1", "1", "claude", "success", "--db", str(db), "--root", str(root)]) == 0
    rows = (root / "claude" / "success" / "events.jsonl").read_text().splitlines()
    assert len(rows) == 3
    assert json.loads(rows[0]) == {"type": "system", "subtype": "init"}
    assert json.loads(rows[1])["out"].endswith("[7000 chars trimmed]")
    assert rows[2] == "plain stderr line"


def test_missing_log_and_bad_case_name_are_refused(tmp_path: Path) -> None:
    db = make_db(tmp_path / "a.db", [("t1", 1, "worker", "only worker")])
    with pytest.raises(SystemExit, match="no delegate log lines"):
        main(["t1", "1", "claude", "c", "--db", str(db), "--root", str(tmp_path)])
    with pytest.raises(SystemExit):
        main(["t1", "1", "claude", "../x", "--db", str(db), "--root", str(tmp_path)])
