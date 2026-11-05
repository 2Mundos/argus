#!/usr/bin/env python3
"""Argus repeat guard: a Claude Code PostToolUse hook that reminds the model when
it calls the same tool with the same arguments again and again
(docs/drafts/2026-11-04.repeat-call-reminder.draft.md). Advisory only: a call is
never blocked or delayed. Standard library only, so it runs with the image's
python3.

The hook reads the hook JSON on stdin and dispatches on hook_event_name.
PostToolUse counts consecutive identical calls per session and, at the counts in
THRESHOLDS, prints additionalContext on stdout and one line on stderr.
UserPromptSubmit clears the session's count. Any internal error does nothing.
"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path

STATE_DIR = Path("/tmp/argus-repeat-guard")  # container-local, not the workspace
THRESHOLDS = (3, 5, 8)
EXCLUDED = {"heartbeat", "wait_for_events", "TodoWrite"}  # repetition is the point
MAX_ARGS_CHARS = 500


def _base_name(tool_name: str) -> str:
    return tool_name.rsplit("__", 1)[-1] if tool_name.startswith("mcp__") else tool_name


def _state_path(session_id: str) -> Path:
    return STATE_DIR / (re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:200] + ".json")


def _load(path: Path) -> dict:
    try:
        state = json.loads(path.read_text())
        if isinstance(state, dict) and isinstance(state.get("count"), int):
            return state
    except Exception:  # noqa: S110 - a missing or corrupt file counts as 0
        pass
    return {"key": None, "count": 0}


def _save(path: Path, state: dict) -> None:
    os.makedirs(path.parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def reminder(tool_name: str, arguments: str, count: int) -> str:
    if count == THRESHOLDS[0]:
        return (
            f"You have made the same {tool_name} call {count} times in a row. "
            "Check the previous result and change your approach."
        )
    return (
        f"You have made the same {tool_name} call {count} times in a row with the "
        f"same arguments: {arguments[:MAX_ARGS_CHARS]}. Read the previous result "
        "and change your approach or finish."
    )


def post_tool_use(event: dict) -> None:
    tool_name = str(event["tool_name"])
    if _base_name(tool_name) in EXCLUDED:
        return
    arguments = json.dumps(
        event.get("tool_input"), sort_keys=True, separators=(",", ":")
    )
    key = f"{tool_name}\n{arguments}"
    path = _state_path(str(event["session_id"]))
    state = _load(path)
    count = state["count"] + 1 if state.get("key") == key else 1
    _save(path, {"key": key, "count": count})
    if count in THRESHOLDS:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": reminder(tool_name, arguments, count),
            }
        }
        sys.stdout.write(json.dumps(output))
        sys.stderr.write(f"repeat_guard: {tool_name} called {count} times in a row\n")


def main() -> int:
    try:
        event = json.load(sys.stdin)
        name = event.get("hook_event_name")
        if name == "PostToolUse":
            post_tool_use(event)
        elif name == "UserPromptSubmit":
            _state_path(str(event["session_id"])).unlink(missing_ok=True)
    except Exception:  # noqa: S110 - fail open on any guard error
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
