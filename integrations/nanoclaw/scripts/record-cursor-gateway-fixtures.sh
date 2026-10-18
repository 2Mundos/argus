#!/usr/bin/env bash
# Record replayable Cursor gateway fixtures from a disposable scratch OneCLI (fake tokens only).
# Never writes agent accessToken fields or secret values — metadata and container-config shape only.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
FIX="$ROOT/integrations/nanoclaw/fixtures/cursor-gateway"
ONECLI_URL="${ONECLI_URL:?set ONECLI_URL e.g. http://127.0.0.1:10254}"
AGENT="${CURSOR_PROBE_AGENT_ID:-scratch-agent}"

mkdir -p "$FIX"
export FIX ONECLI_URL AGENT
node --input-type=module <<'NODE'
import { writeFileSync } from "node:fs";
import { join } from "node:path";

const fix = process.env.FIX;
const base = process.env.ONECLI_URL.replace(/\/$/, "");
const agentId = process.env.AGENT;

const get = async (path) => {
  const res = await fetch(`${base}${path}`);
  if (!res.ok) throw new Error(`${path} ${res.status}`);
  return res.json();
};

const maskProxyEnv = (env) =>
  Object.fromEntries(
    Object.entries(env ?? {}).map(([k, v]) => [
      k,
      String(v).replace(/\/\/[^@/]*@/g, "//<proxy-token>@"),
    ]),
  );

const agents = (await get("/api/agents")).map(({ accessToken: _drop, ...rest }) => rest);
writeFileSync(join(fix, "agents.json"), `${JSON.stringify(agents, null, 2)}\n`);

const secrets = await get("/api/secrets");
for (const s of secrets) {
  if ("value" in s) delete s.value;
}
writeFileSync(join(fix, "secrets-metadata.json"), `${JSON.stringify(secrets, null, 2)}\n`);

const scratch = agents.find((a) => a.identifier === agentId);
if (!scratch?.id) throw new Error(`agent ${agentId} not found`);
writeFileSync(
  join(fix, "scratch-agent-secret-ids.json"),
  `${JSON.stringify(await get(`/api/agents/${scratch.id}/secrets`), null, 2)}\n`,
);

const cfg = await get(`/v1/container-config?agent=${agentId}`);
writeFileSync(
  join(fix, "container-config-before-augment.json"),
  `${JSON.stringify({ ...cfg, env: maskProxyEnv(cfg.env) }, null, 2)}\n`,
);

const defaultCfg = await get("/v1/container-config?agent=default");
writeFileSync(
  join(fix, "container-config-default-agent.json"),
  `${JSON.stringify({ ...defaultCfg, env: maskProxyEnv(defaultCfg.env) }, null, 2)}\n`,
);

console.log(`recorded fixtures under ${fix} (sanitized)`);
NODE
