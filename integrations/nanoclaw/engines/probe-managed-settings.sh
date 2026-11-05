#!/bin/sh
# Probe Claude Code managed settings: probe-managed-settings.sh PATH
# PATH must be an absolute, existing file of valid JSON that registers command_guard.py
# once as a PreToolUse Bash hook and still registers repeat_guard.py. Needs python3 only.
set -eu
path="${1:?usage: probe-managed-settings.sh PATH}"
case "$path" in /*) ;; *) echo "not an absolute path: $path" >&2; exit 1 ;; esac
[ -f "$path" ] || { echo "no such file: $path" >&2; exit 1; }
python3 -I - "$path" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        settings = json.load(f)
except ValueError as e:
    sys.exit(f"{path}: not valid JSON: {e}")
hooks = settings.get("hooks") or {}


def commands(entries):
    for entry in entries or []:
        for hook in entry.get("hooks") or []:
            yield entry.get("matcher"), str(hook.get("command"))


guards = [
    m
    for m, c in commands(hooks.get("PreToolUse"))
    if "command_guard.py" in c and "Bash" in str(m).split("|")
]
if len(guards) != 1:
    sys.exit(f"{path}: command_guard.py registered {len(guards)} times under PreToolUse Bash, want 1")
if not any("repeat_guard.py" in c for k in ("PostToolUse", "UserPromptSubmit") for _, c in commands(hooks.get(k))):
    sys.exit(f"{path}: repeat_guard.py is not registered")
PY
