#!/usr/bin/env bash
# Installed-runtime test of task inputs (docs/specs/task-inputs.spec.md, verification
# boundary). Everything is disposable: a real Argus API on a fresh database under
# mktemp -d, a synthetic patch on a throwaway repository, and the NanoClaw channel in a
# disposable checkout built by `integrations/nanoclaw/provision.sh check`. Runs no model,
# no Docker, and nothing that touches a running Argus or NanoClaw.
#
#   scripts/task-input-e2e.sh
#
# ARGUS_NANOCLAW_STATE reuses a disposable checkout that `provision.sh check` already
# built there (the channel is reinstalled into it); unset, one is provisioned under the
# run's temporary directory and removed with it. ARGUS_E2E_KEEP=1 keeps the directory.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d "${TMPDIR:-/tmp}/argus-ti-e2e.XXXXXX")"
api_pid=""
cleanup() {
  [ -n "$api_pid" ] && kill "$api_pid" 2>/dev/null && wait "$api_pid" 2>/dev/null || true
  if [ "${ARGUS_E2E_KEEP:-0}" = 1 ]; then echo "kept $tmp"; else
    chmod -R u+w "$tmp" 2>/dev/null || true
    rm -rf "$tmp"
  fi
}
trap cleanup EXIT

# The API is local; a proxy from the environment must not intercept it.
export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}" no_proxy="127.0.0.1,localhost${no_proxy:+,$no_proxy}"

# -- API on a disposable database ------------------------------------------------
port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
admin="e2e-admin-$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
url="http://127.0.0.1:$port"
mkdir -p "$tmp/data"
(
  cd "$repo/apps/api"
  ARGUS_DATA_DIR="$tmp/data" ARGUS_ADMIN_TOKEN="$admin" \
    exec uv run --quiet uvicorn argus_api.main:app --host 127.0.0.1 --port "$port" --log-level warning
) >"$tmp/api.log" 2>&1 &
api_pid=$!
for _ in $(seq 1 150); do
  curl -fsS "$url/healthz" >/dev/null 2>&1 && break
  kill -0 "$api_pid" 2>/dev/null || { cat "$tmp/api.log" >&2; exit 1; }
  sleep 0.2
done
curl -fsS "$url/healthz" >/dev/null || { echo "API did not start" >&2; cat "$tmp/api.log" >&2; exit 1; }

call() { # method path token [json]
  local out data=()
  [ $# -ge 4 ] && data=(-d "$4")
  out="$(curl -sS -X "$1" -H "Authorization: Bearer $3" -H 'Content-Type: application/json' \
    "${data[@]}" -w '\n%{http_code}' "$url/api$2")"
  local code="${out##*$'\n'}"
  case "$code" in 2??) printf '%s' "${out%$'\n'*}" ;; *)
    echo "$1 $2: HTTP $code: ${out%$'\n'*}" >&2; return 1 ;; esac
}
field() { python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }
bootstrap() { call POST /agents/bootstrap-tokens "$admin" "{\"role\":\"$1\"}" | field token; }

orch="$(call POST /agents/register "" "{\"bootstrap_token\":\"$(bootstrap orchestrator)\",\"name\":\"e2e-orchestrator\"}" | field token)"
task="$(call POST /tasks "$orch" '{"project":"global","title":"task-inputs e2e","required_capabilities":["task-inputs"]}' | field id)"
other="$(call POST /tasks "$orch" '{"project":"global","title":"task-inputs e2e: another task","required_capabilities":["e2e-other"]}' | field id)"

# -- A synthetic patch of multi-byte UTF-8 on an exact base ------------------------
base="$tmp/base"
git init -q "$base"
git -C "$base" config user.email e2e@argus.invalid
git -C "$base" config user.name "argus e2e"
python3 - "$base/notes.txt" <<'PY'
import sys
with open(sys.argv[1], "w", encoding="utf-8") as f:
    for i in range(1500):
        f.write(f"line {i:04d}: base text\n")
PY
git -C "$base" add notes.txt
git -C "$base" commit -q -m base
python3 - "$base/notes.txt" <<'PY'
import sys
with open(sys.argv[1], "w", encoding="utf-8") as f:
    for i in range(1500):
        f.write(f"line {i:04d}: café naïve résumé ✓ 日本語 — {i}\n")
PY
patch="$tmp/fix.patch"
git -C "$base" diff >"$patch"
git -C "$base" checkout -q -- notes.txt
size="$(wc -c <"$patch")"
[ "$size" -ge 62300 ] || { echo "fixture is $size bytes, below 62300" >&2; exit 1; }
python3 -c 'import sys; b=open(sys.argv[1],"rb").read(); b.decode("utf-8"); assert any(c > 127 for c in b)' "$patch"

attached="$(ARGUS_TOKEN="$orch" python3 "$repo/scripts/attach_artifact.py" \
  --url "$url" --task "$task" --file "$patch" --name fix.patch --share-with-workers)"
artifact="$(field id <<<"$attached")"
sha256="$(field sha256 <<<"$attached")"
ref="$(field ref <<<"$attached")"
printf 'unshared\n' >"$tmp/private.txt"
unshared="$(curl -fsS -X POST -H "Authorization: Bearer $orch" -H 'Content-Type: text/plain' \
  --data-binary @"$tmp/private.txt" "$url/api/tasks/$task/attachments?name=private.txt" | field id)"
revision="$(call GET "/tasks/$task/continuation" "$orch" | field revision)"
call PATCH "/tasks/$task/continuation" "$orch" \
  "{\"expected_revision\":$revision,\"reason\":\"e2e: the recovered patch\",\"wip\":{\"summary\":\"recovered patch\",\"ref\":\"$ref\"}}" >/dev/null
echo "API: $url, task $task, input $artifact ($size bytes, sha256 $sha256)"

# -- The channel in a disposable NanoClaw checkout --------------------------------
state="${ARGUS_NANOCLAW_STATE:-$tmp/nc}"
src="$state/src"
if [ -d "$src/node_modules" ] && [ -d "$src/dist" ]; then
  "$repo/integrations/nanoclaw/install.sh" "$src" >/dev/null
  echo "reusing the disposable checkout $src"
else
  ARGUS_NANOCLAW_STATE="$state" "$repo/integrations/nanoclaw/provision.sh" check >"$tmp/provision.log" 2>&1 \
    || { tail -50 "$tmp/provision.log" >&2; exit 1; }
  echo "provisioned $src (provision.sh check passed)"
fi
(
  cd "$src"
  ARGUS_E2E_API_URL="$url" ARGUS_E2E_ADMIN_TOKEN="$admin" \
    ARGUS_E2E_TASK_ID="$task" ARGUS_E2E_OTHER_TASK_ID="$other" \
    ARGUS_E2E_ARTIFACT_ID="$artifact" ARGUS_E2E_UNSHARED_ID="$unshared" \
    ARGUS_E2E_INPUT_FILE="$patch" ARGUS_E2E_INPUT_NAME=fix.patch ARGUS_E2E_INPUT_SHA256="$sha256" \
    ARGUS_E2E_BASE_REPO="$base" ARGUS_E2E_EVIDENCE="$tmp/evidence.txt" \
    ARGUS_E2E_BOOTSTRAP_A="$(bootstrap worker)" ARGUS_E2E_BOOTSTRAP_B="$(bootstrap worker)" \
    ARGUS_E2E_BOOTSTRAP_C="$(bootstrap worker)" \
    pnpm exec vitest run src/channels/argus-task-inputs.e2e.test.ts
)
echo "checkout: $src at $(git -C "$src" rev-parse --short=12 HEAD)"
cat "$tmp/evidence.txt"
