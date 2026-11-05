"""Repeat guard hook (docs/drafts/2026-11-04.repeat-call-reminder.draft.md),
exercised through its real stdin/stdout contract."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

ENGINES = Path(__file__).resolve().parents[1] / "engines"
spec = importlib.util.spec_from_file_location("repeat_guard", ENGINES / "repeat_guard.py")
assert spec and spec.loader
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "STATE_DIR", tmp_path / "state")
    return tmp_path / "state"


def run(monkeypatch, capsys, event: dict) -> tuple[dict | None, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert guard.main() == 0
    captured = capsys.readouterr()
    return (json.loads(captured.out) if captured.out else None), captured.err


def call(tool="Bash", tool_input=None, session="s1"):
    return {
        "hook_event_name": "PostToolUse",
        "session_id": session,
        "tool_name": tool,
        "tool_input": {"command": "ls"} if tool_input is None else tool_input,
    }


def test_thresholds_emit_only_at_3_5_8(monkeypatch, capsys):
    for n in range(1, 10):
        out, err = run(monkeypatch, capsys, call())
        if n not in (3, 5, 8):
            assert out is None and err == ""
            continue
        body = out["hookSpecificOutput"]
        assert set(body) == {"hookEventName", "additionalContext"}
        assert body["hookEventName"] == "PostToolUse"
        assert f"{n} times" in err and "Bash" in err
        if n == 3:
            assert "\n" not in body["additionalContext"]
            assert "ls" not in body["additionalContext"]
        else:
            assert "Bash" in body["additionalContext"]
            assert '{"command":"ls"}' in body["additionalContext"]


def test_arguments_are_truncated_at_500(monkeypatch, capsys):
    event = call(tool_input={"command": "x" * 2000})
    for _ in range(4):
        run(monkeypatch, capsys, event)
    out, _ = run(monkeypatch, capsys, event)
    assert out["hookSpecificOutput"]["additionalContext"].count("x") < 600


def test_key_order_does_not_matter(monkeypatch, capsys):
    run(monkeypatch, capsys, call("Edit", {"a": 1, "b": 2}))
    run(monkeypatch, capsys, call("Edit", {"b": 2, "a": 1}))
    out, _ = run(monkeypatch, capsys, call("Edit", {"b": 2, "a": 1}))
    assert out is not None


def test_different_call_resets_to_one(monkeypatch, capsys, state_dir):
    run(monkeypatch, capsys, call())
    run(monkeypatch, capsys, call())
    run(monkeypatch, capsys, call(tool_input={"command": "pwd"}))
    assert json.loads(next(state_dir.iterdir()).read_text())["count"] == 1
    run(monkeypatch, capsys, call())
    out, _ = run(monkeypatch, capsys, call())
    assert out is None  # only 2 identical since the reset


@pytest.mark.parametrize(
    "tool", ["heartbeat", "wait_for_events", "TodoWrite", "mcp__argus__heartbeat"]
)
def test_excluded_tools_never_emit_or_touch_state(monkeypatch, capsys, state_dir, tool):
    for _ in range(10):
        out, err = run(monkeypatch, capsys, call(tool))
        assert out is None and err == ""
    assert not state_dir.exists()


def test_excluded_tool_does_not_break_streak(monkeypatch, capsys):
    run(monkeypatch, capsys, call())
    run(monkeypatch, capsys, call("heartbeat"))
    run(monkeypatch, capsys, call())
    out, _ = run(monkeypatch, capsys, call())
    assert out is not None


def test_sessions_are_independent(monkeypatch, capsys):
    run(monkeypatch, capsys, call(session="a"))
    run(monkeypatch, capsys, call(session="b"))
    run(monkeypatch, capsys, call(session="a"))
    out, _ = run(monkeypatch, capsys, call(session="b"))
    assert out is None


def test_user_prompt_submit_clears_state(monkeypatch, capsys, state_dir):
    run(monkeypatch, capsys, call())
    run(monkeypatch, capsys, call())
    assert list(state_dir.iterdir())
    out, _ = run(
        monkeypatch, capsys, {"hook_event_name": "UserPromptSubmit", "session_id": "s1"}
    )
    assert out is None and not list(state_dir.iterdir())
    out, _ = run(monkeypatch, capsys, call())
    assert out is None  # count restarted at 1
    # clearing with no state is fine
    run(monkeypatch, capsys, {"hook_event_name": "UserPromptSubmit", "session_id": "x"})


def test_unsafe_session_id_stays_inside_state_dir(monkeypatch, capsys, state_dir):
    run(monkeypatch, capsys, call(session="../../etc/passwd"))
    [path] = list(state_dir.iterdir())
    assert path.parent == state_dir and "/" not in path.name


@pytest.mark.parametrize("content", [None, "", "not json", "[1]", '{"count": "x"}'])
def test_missing_or_corrupt_state_counts_as_zero(
    monkeypatch, capsys, state_dir, content
):
    if content is not None:
        state_dir.mkdir()
        guard._state_path("s1").write_text(content)
    out, _ = run(monkeypatch, capsys, call())
    assert out is None
    assert json.loads(guard._state_path("s1").read_text())["count"] == 1


def test_unreadable_input_fails_open(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert guard.main() == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_state_write_error_fails_open(monkeypatch, capsys, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setattr(guard, "STATE_DIR", blocker / "sub")
    out, err = run(monkeypatch, capsys, call())
    assert out is None and err == ""


def test_hooks_are_registered():
    settings = json.loads((ENGINES / "claude-managed-settings.json").read_text())
    command = "python3 /usr/local/lib/argus/repeat_guard.py"
    hooks = settings["hooks"]
    pre = hooks["PreToolUse"]
    assert pre[0]["matcher"] == "Bash"
    assert pre[0]["hooks"][0]["command"].endswith("command_guard.py")
    post = hooks["PostToolUse"]
    assert post[0]["matcher"] == "*"
    assert post[0]["hooks"] == [{"type": "command", "command": command}]
    assert hooks["UserPromptSubmit"][0]["hooks"] == [
        {"type": "command", "command": command}
    ]


def test_image_installs_the_hook():
    assert "repeat_guard.py" in (ENGINES / "Dockerfile.tools-layer").read_text()
    assert "repeat_guard.py" in (ENGINES / "apply-tools-layer.sh").read_text()
