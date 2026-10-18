#!/usr/bin/env bash
# Native smoke: read-only staged Cursor stubs → argus-delegate → agent CLI → OneCLI → mock upstream.
# Fake credentials only. Requires scratch stack on argus-cursor-scratch-net.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
AFTER_JSON="${1:?usage: cursor-gateway-native-smoke.sh after-augment.json}"
WORK="${ARGUS_CURSOR_WORK:-$HOME/.hermes/cache/scratch/argus-cursor-work}"
STAGE="${ARGUS_CURSOR_NATIVE_STAGE:-$WORK/native-smoke-stage}"
ONECLI_NAME="${ARGUS_CURSOR_ONECLI_CONTAINER:-argus-cursor-scratch-onecli}"
NET="${ARGUS_CURSOR_DOCKER_NETWORK:-argus-cursor-scratch-net}"
IMAGE="${ARGUS_CURSOR_PROBE_IMAGE:-argus-nanoclaw-agent:99e6c88-gitea}"
UP_LOG="$WORK/logs/upstream.jsonl"
DELEGATE="$ROOT/integrations/nanoclaw/engines/argus-delegate.mjs"
POLICY="$ROOT/integrations/nanoclaw/engines/model-policy.mjs"
POLICY_DEFAULTS="$ROOT/integrations/nanoclaw/engines/model-policy.defaults.json"

if ! docker inspect "$ONECLI_NAME" >/dev/null 2>&1; then
  echo "native smoke: $ONECLI_NAME not running" >&2
  exit 2
fi

if [ -d "$STAGE/cursor" ]; then
  chmod -R u+w "$STAGE/cursor" 2>/dev/null || true
fi
rm -rf "$STAGE"
mkdir -p "$STAGE/cursor" "$STAGE/work"
STAGE="$STAGE" AFTER="$AFTER_JSON" node --input-type=module - <<'NODE'
import { readFileSync, writeFileSync, chmodSync } from "node:fs";
import { join } from "node:path";
const stage = process.env.STAGE;
const afterPath = process.env.AFTER;
const payload = JSON.parse(readFileSync(afterPath, "utf8"));
const cursor = join(stage, "cursor");
for (const stub of payload.credentialStubs ?? []) {
  const base = stub.containerPath.split("/cursor/").pop();
  if (!base) continue;
  writeFileSync(join(cursor, base), stub.content);
}
writeFileSync(join(stage, "gw-ca.pem"), payload.caCertificate);
chmodSync(join(cursor, "auth.json"), 0o444);
chmodSync(join(cursor, "cli-config.json"), 0o444);
chmodSync(cursor, 0o555);
NODE

# Parent EROFS repro (read-only credential tree, no network).
mkdir -p "$STAGE/probe-runtime"
docker run --rm --network none --cap-drop=ALL --security-opt=no-new-privileges \
  --user node \
  --entrypoint node \
  -v "$DELEGATE:/probe/argus-delegate.mjs:ro" \
  -v "$POLICY:/probe/model-policy.mjs:ro" \
  -v "$STAGE/cursor:/probe/cursor:ro" \
  -v "$STAGE/probe-runtime:/probe/runtime" \
  "$IMAGE" --input-type=module -e \
  'import fs from "node:fs";
   import {prepareExecutorEnv,cursorConfigDirAllowsCliWrites} from "/probe/argus-delegate.mjs";
   const out=prepareExecutorEnv("agent",{HOME:"/home/node",XDG_CONFIG_HOME:"/probe",CURSOR_CONFIG_DIR:"/probe/cursor"},"/probe/runtime");
   if(out.CURSOR_CONFIG_DIR==="/probe/cursor") process.exit(2);
   if(!cursorConfigDirAllowsCliWrites(out.CURSOR_CONFIG_DIR)) process.exit(3);
   fs.writeFileSync(out.CURSOR_CONFIG_DIR+"/cli-config.json.atomic-probe","");
   console.log("prepareExecutorEnv_private_writable_ok");' \
  | rg -q prepareExecutorEnv_private_writable_ok

# Fleet-like per-file RO binds (root-owned parent, UID 1000) + native atomic cli-config temp write.
docker run --rm --network none --user node --entrypoint node \
  -v "$DELEGATE:/opt/argus/argus-delegate.mjs:ro" \
  -v "$POLICY:/opt/argus/model-policy.mjs:ro" \
  -v "$STAGE/cursor/auth.json:/home/node/.config/cursor/auth.json:ro" \
  -v "$STAGE/cursor/cli-config.json:/home/node/.config/cursor/cli-config.json:ro" \
  -v "$STAGE/work:/tmp/work" \
  "$IMAGE" --input-type=module -e \
  'import fs from "node:fs";
   import {prepareExecutorEnv} from "/opt/argus/argus-delegate.mjs";
   const e=prepareExecutorEnv("agent",{HOME:"/home/node",XDG_CONFIG_HOME:"/home/node/.config",CURSOR_CONFIG_DIR:"/home/node/.config/cursor"},"/tmp/work/.argus-delegate-runtime");
   if(e.CURSOR_CONFIG_DIR.startsWith("/home/node/.config/cursor")) process.exit(2);
   const tmp=e.CURSOR_CONFIG_DIR+"/cli-config.json."+Date.now()+".tmp";
   fs.writeFileSync(tmp,"{}");
   fs.renameSync(tmp,e.CURSOR_CONFIG_DIR+"/cli-config.json");
   console.log("fleet_mount_topology_ok");' \
  | rg -q fleet_mount_topology_ok

docker exec argus-cursor-scratch-up sh -c ': > /w/logs/upstream.jsonl' 2>/dev/null || true

ENV_FILE="$STAGE/container.env"
node -e "
const payload = JSON.parse(require('fs').readFileSync(process.argv[1], 'utf8'));
const host = process.argv[2];
const lines = [];
for (const [k, v] of Object.entries(payload.env ?? {})) {
  lines.push(k + '=' + String(v).replace(/host\\.docker\\.internal/g, host));
}
require('fs').writeFileSync(process.argv[3], lines.join('\n') + '\n');
" "$AFTER_JSON" "$ONECLI_NAME" "$ENV_FILE"

chmod -R a+rwX "$STAGE/work" 2>/dev/null || true
printf '%s\n' "Reply with exactly the token SMOKE_OK and nothing else." >"$STAGE/prompt.txt"
set +e
docker run --rm --network "$NET" \
  --cap-drop=ALL --security-opt=no-new-privileges \
  --entrypoint node \
  -v "$DELEGATE:/opt/argus/argus-delegate.mjs:ro" \
  -v "$POLICY:/opt/argus/model-policy.mjs:ro" \
  -v "$POLICY_DEFAULTS:/opt/argus/model-policy.defaults.json:ro" \
  -v "$STAGE/cursor:/home/node/.config/cursor:ro" \
  -v "$STAGE/work:/tmp/work" \
  -v "$STAGE/gw-ca.pem:/tmp/onecli-gateway-ca.pem:ro" \
  -v "$STAGE/prompt.txt:/tmp/prompt.txt:ro" \
  --env-file "$ENV_FILE" \
  -e HOME=/home/node \
  -e XDG_CONFIG_HOME=/home/node/.config \
  -e CURSOR_CONFIG_DIR=/home/node/.config/cursor \
  -e ARGUS_DELEGATE_ROOTS=/tmp/work \
  -e ARGUS_ENGINE=cursor \
  -e NO_PROXY=localhost,127.0.0.1 \
  -e no_proxy=localhost,127.0.0.1 \
  "$IMAGE" /opt/argus/argus-delegate.mjs agent --read-only --model composer-2.5 --timeout 35 --cwd /tmp/work --prompt-file /tmp/prompt.txt \
  >"$STAGE/delegate.out" 2>"$STAGE/delegate.err"
DELEGATE_EXIT=$?
set -e

STAGE="$STAGE" UP_LOG="$UP_LOG" DELEGATE_EXIT="$DELEGATE_EXIT" node --input-type=module - <<'NODE'
import { readFileSync } from "node:fs";
const stage = process.env.STAGE;
const upLog = process.env.UP_LOG;
const delegateExit = Number(process.env.DELEGATE_EXIT);
const errTail = readFileSync(`${stage}/delegate.err`, "utf8").slice(-2000);
const outTail = readFileSync(`${stage}/delegate.out`, "utf8").slice(-2000);
const lines = readFileSync(upLog, "utf8").trim().split("\n").filter(Boolean);
const parsed = lines.map((l) => {
  try {
    return JSON.parse(l);
  } catch {
    return null;
  }
}).filter(Boolean);
const paths = parsed.map((r) => r.path).filter(Boolean);
const exchange = paths.some((p) => String(p).includes("exchange_user_api_key"));
const injected = parsed.some((r) => String(r.authorization ?? "").includes("REALFAKE-ACCESS-TOKEN"));
const scoped = parsed.some(
  (r) => r.host === "api2.cursor.sh" && /\/aiserver\.v1\.|\/agent\.v1\./.test(String(r.path ?? "")),
);
const authProbeOnly = !scoped && injected;
const workspaceTrustBlocked = /workspace trust required/i.test(errTail);
const configEacces = /EACCES.*cli-config\.json/i.test(errTail + outTail);
let reportTail = "";
try {
  const last = outTail.trim().split("\n").pop() ?? "";
  reportTail = JSON.parse(last).stderr_tail ?? "";
} catch {
  /* ignore */
}
const networkAttempted = /EPROTO|ECONNREFUSED|tls_get_more_records|fetch failed/i.test(
  errTail + outTail + reportTail,
);
const summary = {
  delegateExit,
  workspaceTrustBlocked,
  configEacces,
  networkAttempted,
  upstreamLines: parsed.length,
  exchangeFromWorker: exchange,
  gatewayInjectedFakeBearer: injected,
  scopedCursorPath: scoped,
  authProbeOnly,
  delegateErrTail: errTail.slice(0, 400),
  delegateOutTail: outTail.slice(0, 400),
};
console.log(JSON.stringify(summary, null, 2));
if (workspaceTrustBlocked) {
  console.error("agent blocked on workspace trust (expected --trust on authorized cwd)");
  process.exit(1);
}
if (configEacces) {
  console.error("native CLI could not atomically write cli-config (need private CURSOR_CONFIG_DIR)");
  process.exit(1);
}
if (exchange) {
  console.error("worker reached /auth/exchange_user_api_key");
  process.exit(1);
}
if (!injected && !networkAttempted) {
  console.error("delegate never reached network (config/auth still blocking?)");
  process.exit(1);
}
if (injected && !scoped && !authProbeOnly) {
  console.error("expected scoped api2 path or distinguishable auth probe with injection");
  process.exit(1);
}
NODE

echo "native smoke ok: private CURSOR_CONFIG_DIR; delegate reached network or upstream injection"
