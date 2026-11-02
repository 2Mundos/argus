#!/usr/bin/env bash
# Quota resume spike (docs/research/2026-11-02.quota-resume-spike.research.md).
# Evidence, in a temp dir and against a local fake Messages API (no credentials, no
# provider account), that a Claude Code session resumes by id:
#
#   1. the real `claude` CLI: --resume <id> keeps the id and resends the first turn;
#      a fresh run and an unknown id do not;
#   2. the pinned NanoClaw agent runner: a NEW session dir whose outbound.db was
#      seeded by seedClaudeContinuation (the shipped function) resumes the first
#      session's conversation from the shared Claude state dir; an unseeded one
#      does not; a seed whose transcript is gone is cleared by the runner.
#
# Prints ids, booleans and counts only: never transcript text or the environment.
#
#   NANOCLAW_REF=/tmp/nc/nanoclaw NANOCLAW_SRC=/tmp/nc/state/src \
#     integrations/nanoclaw/scripts/quota-resume-spike.sh
#
# NANOCLAW_REF: a NanoClaw checkout at the pin (read only; its runner is copied).
# NANOCLAW_SRC: a checkout prepared by `provision.sh check` (for better-sqlite3 and
# the installed channel files). Needs claude, bun, node and pnpm on PATH.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REF="${NANOCLAW_REF:?NANOCLAW_REF is required}"
SRC="${NANOCLAW_SRC:?NANOCLAW_SRC is required}"
MARKER="MARKER-QR-$RANDOM$RANDOM"
MODEL="claude-haiku-4-5"
work="$(mktemp -d "${TMPDIR:-/tmp}/qr-spike.XXXXXX")"
fake_pid=""
cleanup() { [ -n "$fake_pid" ] && kill "$fake_pid" 2>/dev/null; rm -rf "$work"; }
trap cleanup EXIT

# Nothing from the caller's environment reaches claude: no proxy, no key, no config.
run_env=(env -i "PATH=$PATH" "HOME=$work/home" "TMPDIR=$work/tmp"
  "ANTHROPIC_API_KEY=gateway-managed" DISABLE_TELEMETRY=1
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 DISABLE_AUTOUPDATER=1)
mkdir -p "$work/home" "$work/tmp" "$work/cwd"

log="$work/requests.jsonl"
: > "$log"
mkfifo "$work/port"
FAKE_ANTHROPIC_LOG="$log" node "$here/fake-anthropic.mjs" > "$work/port" &
fake_pid=$!
read -r port < "$work/port"
run_env+=("ANTHROPIC_BASE_URL=http://127.0.0.1:$port")

jget() { node -e 'console.log(JSON.parse(require("fs").readFileSync(0, "utf8"))[process.argv[1]])' "$1"; }
requests() { wc -l < "$log" | tr -d ' '; }
# Whether request number $1 (1-based) carries the marker; prints true/false only.
has_marker() { sed -n "${1}p" "$log" | grep -q "$MARKER" && echo true || echo false; }
# The one transcript id under a Claude state dir (the session id is its filename).
only_session() { find "$1/projects" -name '*.jsonl' -printf '%f\n' | sed 's/\.jsonl$//'; }

echo "versions: claude=$(claude --version | cut -d' ' -f1) bun=$(bun --version) node=$(node --version)"
echo "nanoclaw: $(git -C "$REF" rev-parse HEAD)"
echo "marker: $MARKER"

echo "== 1. claude CLI"
cli=("${run_env[@]}" "CLAUDE_CONFIG_DIR=$work/cli-claude" claude --model "$MODEL" --output-format json)
(cd "$work/cwd" && "${cli[@]}" -p "Remember this marker: $MARKER. Reply ACK." > "$work/c1.json")
first="$(only_session "$work/cli-claude")"
echo "create: session=$first reported=$(jget session_id < "$work/c1.json") request1_marker=$(has_marker 1)"

(cd "$work/cwd" && "${cli[@]}" --resume "$first" -p "Which marker did I give you?" > "$work/c2.json")
echo "resume: same_id=$([ "$(jget session_id < "$work/c2.json")" = "$first" ] && echo true || echo false) is_error=$(jget is_error < "$work/c2.json") request$(requests)_marker=$(has_marker "$(requests)")"

(cd "$work/cwd" && "${cli[@]}" -p "Which marker did I give you?" > "$work/c3.json")
echo "fresh: new_id=$([ "$(jget session_id < "$work/c3.json")" != "$first" ] && echo true || echo false) request$(requests)_marker=$(has_marker "$(requests)")"

before="$(requests)"
set +e
(cd "$work/cwd" && "${cli[@]}" --resume 00000000-0000-4000-8000-000000000000 -p "x" > "$work/c4.json" 2> "$work/c4.err")
status=$?
set -e
# The runner's STALE_SESSION_RE (container/agent-runner/src/providers/claude.ts).
stale="$(cat "$work/c4.json" "$work/c4.err" | grep -Eiq 'no conversation found|ENOENT.*\.jsonl|session.*not found' && echo true || echo false)"
echo "unknown id: exit=$status no_request=$([ "$(requests)" = "$before" ] && echo true || echo false) matches_stale_session_re=$stale"

echo "== 2. pinned agent runner"
runner="$work/runner"
cp -r "$REF/container/agent-runner/." "$runner"
(cd "$runner" && bun install --frozen-lockfile > /dev/null 2>&1)
conn="$runner/src/mailbox/sqlite/connection.ts"
sed -i "s|^const DEFAULT_INBOUND_PATH = '/workspace/inbound.db';|const DEFAULT_INBOUND_PATH = process.env.SPIKE_INBOUND_DB!;|; s|^const DEFAULT_OUTBOUND_PATH = '/workspace/outbound.db';|const DEFAULT_OUTBOUND_PATH = process.env.SPIKE_OUTBOUND_DB!;|" "$conn"
[ "$(grep -c 'process.env.SPIKE_' "$conn")" = 2 ] || { echo "runner layout changed: mailbox paths not found" >&2; exit 1; }
cp "$here/quota-resume-spike-runner.ts" "$runner/src/spike-runner.ts"

# One group: a shared Claude state dir and one cwd, as .claude-shared and
# /workspace/agent are for every session of a NanoClaw agent group.
shared="$work/group/.claude-shared"
mkdir -p "$shared" "$work/group/agent"
session_env() { # $1: session dir
  mkdir -p "$1"
  echo "${run_env[@]}" "CLAUDE_CONFIG_DIR=$shared" "SPIKE_INBOUND_DB=$1/inbound.db" \
    "SPIKE_OUTBOUND_DB=$1/outbound.db" "SPIKE_CWD=$work/group/agent" \
    "SPIKE_HOST_SCHEMA=$REF/src/mailbox/sqlite/schema.ts" \
    "NANOCLAW_HEARTBEAT_PATH=$1/.heartbeat" "NANOCLAW_CONVERSATIONS_DIR=$1/conversations"
}
runner_turn() { # $1: session dir, $2: text
  # shellcheck disable=SC2046
  (cd "$runner/src" && $(session_env "$1") bun spike-runner.ts turn "$2" 2> "$1/runner.err" | tail -1)
}
prepare() { # shellcheck disable=SC2046
  (cd "$runner/src" && $(session_env "$1") bun spike-runner.ts prepare "$1")
}
seed() { # $1: outbound.db, $2: id; the shipped function, from the installed channel
  (cd "$SRC" && node --import tsx -e "
    const { seedClaudeContinuation } = await import('./src/channels/argus-quota-resume.ts');
    console.log(await seedClaudeContinuation(process.argv[1], process.argv[2]));" "$1" "$2" 2>/dev/null)
}

a="$work/sessions/a"
prepare "$a"
out_a="$(runner_turn "$a" "Remember this marker: $MARKER. Reply ACK.")"
id_a="$(jget continuation <<< "$out_a")"
echo "session A: continuation=$id_a message=$(jget message <<< "$out_a") request$(requests)_marker=$(has_marker "$(requests)")"

b="$work/sessions/b"
prepare "$b"
echo "session B seed: first=$(seed "$b/outbound.db" "$id_a") again=$(seed "$b/outbound.db" "$id_a") other=$(seed "$b/outbound.db" "ffffffff-0000-4000-8000-000000000000")"
out_b="$(runner_turn "$b" "Which marker did I give you?")"
echo "session B: message=$(jget message <<< "$out_b") same_id=$([ "$(jget continuation <<< "$out_b")" = "$id_a" ] && echo true || echo false) resumed_log=$(grep -c "Resuming agent session $id_a" "$b/runner.err") request$(requests)_marker=$(has_marker "$(requests)")"
echo "session B after run: seed_again=$(seed "$b/outbound.db" "$id_a")"

c="$work/sessions/c"
prepare "$c"
out_c="$(runner_turn "$c" "Which marker did I give you?")"
echo "session C (unseeded): new_id=$([ "$(jget continuation <<< "$out_c")" != "$id_a" ] && echo true || echo false) request$(requests)_marker=$(has_marker "$(requests)")"

d="$work/sessions/d"
prepare "$d"
gone="eeeeeeee-0000-4000-8000-000000000000"
echo "session D seed (no transcript): $(seed "$d/outbound.db" "$gone")"
before="$(requests)"
out_d="$(runner_turn "$d" "Which marker did I give you?")"
echo "session D: message=$(jget message <<< "$out_d") continuation_after=$(jget continuation <<< "$out_d") cleared_or_fresh=$([ "$(jget continuation <<< "$out_d")" != "$gone" ] && echo true || echo false) stale_log=$(grep -c 'Stale session detected' "$d/runner.err") new_requests=$(( $(requests) - before ))"
