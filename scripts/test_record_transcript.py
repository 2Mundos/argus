"""The transcript recorder, on a synthetic delegate log."""

import json
import sqlite3
from pathlib import Path

import pytest
import record_transcript
from record_transcript import MAX_BYTES, STDERR_MAX_BYTES, main, render, scrub, trim


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
    event = {
        "type": "user",
        "content": [{"type": "tool_result", "content": "x" * 5000}],
    }
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


def test_scrub_masks_credentials_emails_home_paths_and_extra_values() -> None:
    text = "cd /home/pedro/work/acme-secret-repo; mail a.b@example.com; Authorization: Bearer abcdefghij123456"
    out = scrub(text, ("acme-secret-repo",))
    assert "pedro" not in out
    assert "/home/user/work/[redacted]" in out
    assert "example.com" not in out
    assert "abcdefghij123456" not in out


def test_main_writes_the_attempt_events_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        record_transcript, "derive", lambda engine, events: '{"d": 1}\n'
    )
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
    argv = [
        "t1",
        "1",
        "claude",
        "success",
        "--source",
        "task t1, claude 2.1",
        "--db",
        str(db),
        "--root",
        str(root),
    ]
    assert main(argv) == 0
    rows = (root / "claude" / "success" / "events.jsonl").read_text().splitlines()
    assert len(rows) == 3
    assert json.loads(rows[0]) == {"type": "system", "subtype": "init"}
    assert json.loads(rows[1])["out"].endswith("[7000 chars trimmed]")
    assert rows[2] == "plain stderr line"
    case = root / "claude" / "success"
    assert (case / "source.md").read_text() == "task t1, claude 2.1\n"
    assert json.loads((case / "expected.json").read_text()) == {"d": 1}


def test_missing_log_and_bad_case_name_are_refused(tmp_path: Path) -> None:
    db = make_db(tmp_path / "a.db", [("t1", 1, "worker", "only worker")])
    with pytest.raises(SystemExit, match="no delegate log lines"):
        main(
            [
                "t1",
                "1",
                "claude",
                "c",
                "--source",
                "s",
                "--db",
                str(db),
                "--root",
                str(tmp_path),
            ]
        )
    with pytest.raises(SystemExit):
        main(
            [
                "t1",
                "1",
                "claude",
                "../x",
                "--source",
                "s",
                "--db",
                str(db),
                "--root",
                str(tmp_path),
            ]
        )


def record_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stderr: bytes | None,
    events: str = '{"type": "system"}',
) -> Path:
    monkeypatch.setattr(
        record_transcript, "derive", lambda engine, events: '{"d": 1}\n'
    )
    db_path = tmp_path / "a.db"
    db_path.unlink(missing_ok=True)  # the helper records more than once per test
    db = make_db(db_path, [("t1", 1, "delegate", events)])
    argv = ["t1", "1", "claude", "c", "--source", "s", "--db", str(db)]
    argv += ["--root", str(tmp_path / "root")]
    if stderr is not None:
        err = tmp_path / "run.err"
        err.write_bytes(stderr)
        argv += ["--stderr", str(err)]
    assert main(argv) == 0
    return tmp_path / "root" / "claude" / "c"


def test_stderr_is_redacted_in_every_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    err = (
        b"boom in /home/pedro/work/x.py\nmail pedro@example.com\n"
        b"key sk-testfakekey1234567890 Authorization: Bearer abcdefghij123456\n"
    )
    case = record_case(tmp_path, monkeypatch, err)
    text = (case / "stderr.txt").read_text()
    assert "/home/user/work/x.py" in text
    assert "[redacted email]" in text
    for f in case.iterdir():
        body = f.read_text()
        for leak in ("pedro", "sk-testfakekey1234567890", "example.com"):
            assert leak not in body, (f.name, leak)


def test_stderr_keeps_only_the_last_16_kib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    err = b"HEAD-MARK\n" + b"a" * 40000 + b"\nTAIL-MARK\n"
    text = (record_case(tmp_path, monkeypatch, err) / "stderr.txt").read_text()
    assert len(text.encode()) <= STDERR_MAX_BYTES
    assert text.endswith("TAIL-MARK\n")
    assert "HEAD-MARK" not in text


def test_stderr_cut_never_splits_a_character(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    err = "é".encode() * STDERR_MAX_BYTES  # 2-byte characters, odd cut point
    text = (record_case(tmp_path, monkeypatch, err) / "stderr.txt").read_text()
    assert "\ufffd" not in text
    assert len(text.encode()) <= STDERR_MAX_BYTES


def test_big_events_and_big_stderr_stay_within_the_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = json.dumps({"type": "user", "out": "y" * 3000})
    monkeypatch.setattr(
        record_transcript, "derive", lambda engine, events: '{"d": 1}\n'
    )
    db = make_db(tmp_path / "a.db", [("t1", 1, "delegate", big)] * 120)
    err = tmp_path / "run.err"
    err.write_bytes(b"e" * 100_000)
    argv = ["t1", "1", "claude", "c", "--source", "s", "--db", str(db)]
    argv += ["--root", str(tmp_path / "root"), "--stderr", str(err)]
    try:
        main(argv)
    except SystemExit:
        return
    case = tmp_path / "root" / "claude" / "c"
    assert sum(f.stat().st_size for f in case.iterdir()) <= MAX_BYTES
    assert (case / "stderr.txt").stat().st_size <= STDERR_MAX_BYTES


def test_no_stderr_gives_no_file_and_removes_a_stale_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = record_case(tmp_path, monkeypatch, None)
    assert not (case / "stderr.txt").exists()
    (case / "stderr.txt").write_text("old stderr\n")
    record_case(tmp_path, monkeypatch, None)
    assert not (case / "stderr.txt").exists()
    (case / "stderr.txt").write_text("old stderr\n")
    record_case(tmp_path, monkeypatch, b"")
    assert not (case / "stderr.txt").exists()


def test_non_utf8_stderr_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = record_case(tmp_path, monkeypatch, b"bad \xff byte\n")
    assert "bad" in (case / "stderr.txt").read_text()
