"""Repeat guard log counter (integrations/nanoclaw/engines/repeat_guard_counts.py),
exercised on sample lines and through the real script."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ENGINES = Path(__file__).resolve().parents[1] / "engines"
SCRIPT = ENGINES / "repeat_guard_counts.py"
spec = importlib.util.spec_from_file_location("repeat_guard_counts", SCRIPT)
assert spec and spec.loader
counts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(counts)


def hook_line(tool: str, count: int) -> str:
    # Hard-coded copy of the sys.stderr.write format in repeat_guard.py.
    return f"repeat_guard: {tool} called {count} times in a row\n"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_thresholds_other_and_noise():
    result = counts.count_lines(
        [
            hook_line("Bash", 3),
            hook_line("Bash", 3),
            hook_line("Bash", 5),
            hook_line("Read", 8),
            hook_line("Read", 4),
            hook_line("Read", 9),
            "some unrelated stderr\n",
            "repeat_guard: Bash called x times in a row\n",
            "",
        ]
    )
    assert result["thresholds"] == {3: 2, 5: 1, 8: 1}
    assert result["other"] == 2
    assert result["total"] == 6
    assert result["tools"] == {"Bash": 3, "Read": 1}


def test_crlf_whitespace_and_tool_name_shapes():
    result = counts.count_lines(
        [
            hook_line("mcp__server__tool", 3).replace("\n", "\r\n"),
            "  " + hook_line("my tool with spaces", 5),
            hook_line("weird called 3 times in a row", 8),
        ]
    )
    assert result["thresholds"] == {3: 1, 5: 1, 8: 1}
    assert result["tools"] == {
        "mcp__server__tool": 1,
        "my tool with spaces": 1,
        "weird called 3 times in a row": 1,
    }


def write_logs(tmp_path: Path) -> list[str]:
    a = tmp_path / "a.log"
    b = tmp_path / "b.log"
    a.write_text(hook_line("Bash", 3) + "noise\n" + hook_line("Bash", 5))
    b.write_bytes(
        (hook_line("Bash", 3) + hook_line("Grep", 6)).encode() + b"\xff\xfe bad\n"
    )
    return [str(a), str(b)]


def test_multiple_files_aggregate_in_table(tmp_path):
    proc = run(*write_logs(tmp_path))
    assert proc.returncode == 0
    rows = {line.split()[0]: line.split()[1] for line in proc.stdout.splitlines()[1:5]}
    assert rows == {"3": "2", "5": "1", "8": "0", "other": "1"}
    assert "total  4" in proc.stdout
    assert "Bash  3" in proc.stdout


def test_missing_path_exits_2_without_counts(tmp_path):
    good, missing = write_logs(tmp_path)[0], str(tmp_path / "nope.log")
    proc = run(good, missing)
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert missing in proc.stderr


def test_json_matches_table(tmp_path):
    paths = write_logs(tmp_path)
    data = json.loads(run("--json", *paths).stdout)
    assert data == {
        "thresholds": {"3": 2, "5": 1, "8": 0},
        "other": 1,
        "total": 4,
        "tools": {"Bash": 3},
    }


def test_no_matches_exits_0(tmp_path):
    log = tmp_path / "empty.log"
    log.write_text("nothing here\n")
    proc = run(str(log))
    assert proc.returncode == 0
    assert "total  0" in proc.stdout
