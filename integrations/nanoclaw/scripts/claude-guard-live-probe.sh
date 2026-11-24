#!/bin/sh
# Live command guard probe (docs/specs/command-guard.spec.md 6): Claude Code, as
# installed in an agent image, runs one model turn against fake-anthropic.mjs (a local
# Messages API, dummy key, no credentials) in a fresh HOME, with the CLI's default
# permission mode and no --bare, guard-off or permission bypass. The fake API asks for a
# benign marker write, then `rm --no-preserve-root --version > <denied marker>`.
# User, project and local settings set `disableAllHooks` and an empty PreToolUse list,
# so a pass also shows that only the managed settings can register the hook.
#
#   claude-guard-live-probe.sh [--image IMAGE] [--delegate]
#
# --image runs the probe in a throwaway container of IMAGE (docker run --rm
# --pull=never --network=none; CONTAINER_RUNTIME_BIN, default docker); without it, the
# probe runs on this filesystem. --delegate runs the exact command line that
# argus-delegate builds for its Claude executor (buildCommand from the installed
# argus-delegate) instead of a plain `claude -p`. Exits 0 when the PreToolUse:Bash hook
# ran, the Argus denial reason came back as an is_error tool_result, the benign marker
# holds its text, the denied marker is absent and the CLI exited 0.
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
image=""
delegate=""
while [ $# -gt 0 ]; do
  case "$1" in
    --image) image="${2:?--image needs a value}"; shift 2 ;;
    --delegate) delegate="--delegate"; shift ;;
    *) echo "usage: claude-guard-live-probe.sh [--image IMAGE] [--delegate]" >&2; exit 2 ;;
  esac
done

if [ -n "$image" ]; then
  case "$image" in -*) echo "not an image reference: $image" >&2; exit 2 ;; esac
  exec "${CONTAINER_RUNTIME_BIN:-docker}" run --rm --pull=never --network=none \
    -v "$here/fake-anthropic.mjs:/opt/argus-probe/fake-anthropic.mjs:ro" \
    -v "$here/claude-guard-live-probe.sh:/opt/argus-probe/claude-guard-live-probe.sh:ro" \
    --entrypoint /bin/sh "$image" /opt/argus-probe/claude-guard-live-probe.sh $delegate
fi

work="$(mktemp -d)"
api_pid=""
cleanup() {
  [ -n "$api_pid" ] && kill "$api_pid" 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT
mkdir -p "$work/home/.claude" "$work/ws/.claude"
benign="$work/ws/benign.marker"
denied="$work/ws/denied.marker"
override='{"disableAllHooks": true, "hooks": {"PreToolUse": []}}'
for f in "$work/home/.claude/settings.json" "$work/ws/.claude/settings.json" \
  "$work/ws/.claude/settings.local.json"; do
  printf '%s\n' "$override" >"$f"
done

FAKE_ANTHROPIC_LOG="$work/api.log" FAKE_ANTHROPIC_BASH="$(python3 -I -c '
import json, shlex, sys
print(json.dumps([f"printf %s\\\\n argus-guard-benign > {shlex.quote(sys.argv[1])}",
                  f"rm --no-preserve-root --version > {shlex.quote(sys.argv[2])}"]))
' "$benign" "$denied")" node "$here/fake-anthropic.mjs" >"$work/port" &
api_pid=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ -s "$work/port" ] && break
  sleep 0.5
done
port="$(cat "$work/port")"

prompt="Run the command guard probe."
if [ -n "$delegate" ]; then
  # One argument per line; the delegate's command line has no newlines in it. Inputs
  # go by environment: argus-delegate runs its CLI when process.argv[1] names it.
  PROBE_DELEGATE="$(command -v argus-delegate)" PROBE_CWD="$work/ws" \
    PROBE_LAST="$work/last.txt" node -e '
const { PROBE_DELEGATE: file, PROBE_CWD: cwd, PROBE_LAST: last } = process.env;
import(require("node:url").pathToFileURL(file).href).then((m) => {
  const cmd = m.buildCommand("claude", { cwd, model: null, readOnly: false, lastMessageFile: last });
  process.stdout.write([cmd.bin, ...cmd.args].join("\n") + "\n");
});
' >"$work/argv"
else
  printf '%s\n' claude -p --output-format stream-json --verbose >"$work/argv"
fi
printf '%s\n' --include-hook-events --debug-file "$work/debug.log" >>"$work/argv"
echo "argv: $(tr '\n' ' ' <"$work/argv")"

set +e
(
  cd "$work/ws"
  # One argument per line in $work/argv (no argument holds a newline).
  IFS='
'
  # Argv words are split on newlines only; no glob expansion of the paths in them.
  set -f
  # shellcheck disable=SC2046
  printf '%s' "$prompt" | env -i PATH="$PATH" HOME="$work/home" \
    ANTHROPIC_BASE_URL="http://127.0.0.1:$port" ANTHROPIC_API_KEY=sk-ant-local-dummy \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    timeout 180 $(cat "$work/argv") >"$work/out.jsonl" 2>"$work/err"
)
code=$?
set -e

python3 -I - "$work" "$code" <<'PY'
import json
import os
import re
import sys

work, code = sys.argv[1], int(sys.argv[2])
BLOCK = "Blocked by Argus command guard: "
events = []
for line in open(os.path.join(work, "out.jsonl"), encoding="utf-8"):
    try:
        events.append(json.loads(line))
    except ValueError:
        pass
init = next((e for e in events if e.get("subtype") == "init"), {})
print(f"claude_code_version: {init.get('claude_code_version')}")
print(f"permission_mode: {init.get('permissionMode')}")
debug = open(os.path.join(work, "debug.log"), encoding="utf-8", errors="replace").read()
watched = re.search(r"Watching for changes in setting files ([^\n]*)", debug)
print(f"settings files: {watched.group(1) if watched else None}")
started = [e for e in events if e.get("subtype") == "hook_started" and e.get("hook_name") == "PreToolUse:Bash"]
responses = [e for e in events if e.get("subtype") == "hook_response" and e.get("hook_name") == "PreToolUse:Bash"]
for e in started:
    print("hook_started:", json.dumps({k: e.get(k) for k in ("hook_name", "hook_event")}))
deny = None
for e in responses:
    print("hook_response:", json.dumps({k: e.get(k) for k in ("hook_name", "exit_code", "outcome", "output")}))
    try:
        out = json.loads(e.get("output") or "{}").get("hookSpecificOutput") or {}
    except ValueError:
        out = {}
    if out.get("permissionDecision") == "deny" and str(out.get("permissionDecisionReason", "")).startswith(BLOCK):
        deny = out["permissionDecisionReason"]
results = [
    b
    for e in events
    if e.get("type") == "user"
    for b in (e.get("message") or {}).get("content") or []
    if isinstance(b, dict) and b.get("type") == "tool_result"
]
# Claude Code writes the denial as an is_error tool_result: the hook-error wrapper around
# the guard's message, nothing else (same shape as argus-command-blocked.ts).
HOOK_ERROR = ("<tool_use_error>PreToolUse:Bash hook error: " + BLOCK, "</tool_use_error>")


def wrapped_denial(content):
    if isinstance(content, list):
        content = "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return isinstance(content, str) and content.startswith(HOOK_ERROR[0]) and content.endswith(HOOK_ERROR[1])


blocked = [b for b in results if b.get("is_error") and wrapped_denial(b.get("content"))]
for b in blocked:
    print("tool_result:", json.dumps({k: b.get(k) for k in ("tool_use_id", "is_error", "content")}))
benign_path = os.path.join(work, "ws", "benign.marker")
benign = open(benign_path, encoding="utf-8").read() if os.path.exists(benign_path) else None
denied = os.path.exists(os.path.join(work, "ws", "denied.marker"))
print(f"benign marker: {benign!r}")
print(f"denied marker present: {denied}")
print(f"cli exit: {code}")
checks = {
    "pretooluse_bash_hook_ran": len(started) >= 2 and len(responses) >= 2,
    "argus_deny_reason": deny is not None,
    "is_error_tool_result": len(blocked) == 1,
    "benign_marker": benign == "argus-guard-benign\n",
    "denied_marker_absent": not denied,
    "cli_exit_0": code == 0,
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'} {name}")
sys.exit(0 if all(checks.values()) else 1)
PY
