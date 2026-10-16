#!/usr/bin/env bash
# Version smoke of the four CLIs in an agent image, and the credential state of a
# FRESH container of it; writes the facts the Argus engine catalogue reads.
#   smoke.sh IMAGE [LEDGER]     LEDGER default: ${ARGUS_DATA_DIR:-data}/engines-ledger.json
# Facts, never secrets. `authenticated=false` here means "a fresh container of this
# image has no credential" (argus-delegate exits 78 and runs nothing); a worker that
# logged in with the vendor client is a different, later fact. `validated` is left
# alone: only a real checked task sets it. Runs offline (--network none).
set -euo pipefail

image="${1:?usage: smoke.sh IMAGE [LEDGER]}"
ledger="${2:-${ARGUS_DATA_DIR:-data}/engines-ledger.json}"
expect() { # id binary pinned-substring
  local id="$1" bin="$2" want="$3" out code
  out="$(docker run --rm --network none --entrypoint "$bin" "$image" --version 2>&1 | head -1)" || true
  if [[ "$out" == *"$want"* ]]; then echo "$id|true|$out"; else echo "$id|false|expected $want, got: ${out:-nothing}"; fi
}
auth() { # id executor
  local id="$1" ex="$2" out code=0
  out="$(docker run --rm --network none -e ARGUS_ENGINE=smoke \
    --entrypoint argus-delegate "$image" "$ex" --cwd /workspace/group --timeout 5 </dev/null 2>/dev/null | tail -1)" || code=$?
  if [[ "$out" == *'"blocked":"auth_missing"'* ]]; then
    echo "$id|false|fresh container has no credential (argus-delegate exit $code, blocked=auth_missing)"
  else
    echo "$id|null|unexpected delegate result: ${out:0:160}"
  fi
}
rows=(
  "$(expect claude claude '2.1.296 (Claude Code)')"
  "$(expect codex codex 'codex-cli 0.162.1')"
  "$(expect opencode opencode '1.18.27')"
  "$(expect cursor-agent agent '2026.10.16-e3cb207')"
)
arows=(
  "$(auth claude claude)"
  "$(auth codex codex)"
  "$(auth opencode opencode)"
  "$(auth cursor-agent agent)"
)
mkdir -p "$(dirname "$ledger")"
node - "$ledger" "$image" "${rows[@]}" -- "${arows[@]}" <<'JS'
const fs = require("fs");
const [file, image, ...rest] = process.argv.slice(2);
const cut = rest.indexOf("--");
const parse = (r) => { const [id, ok, ...d] = r.split("|"); return [id, { ok: ok === "null" ? null : ok === "true", detail: d.join("|") }]; };
const installed = Object.fromEntries(rest.slice(0, cut).map(parse));
const authed = Object.fromEntries(rest.slice(cut + 1).map(parse));
let doc = { engines: {} };
try { doc = JSON.parse(fs.readFileSync(file, "utf8")); } catch {}
doc.engines ||= {};
const at = new Date().toISOString();
for (const id of Object.keys(installed)) {
  const e = (doc.engines[id] ||= {});
  e.installed = { ...installed[id], at };
  e.authenticated = { ...authed[id], at };
  e.image = image;
  const m = installed[id].detail.match(/(\d+\.\d+\.\d+|\d{4}\.\d+\.\d+-[0-9a-f]+)/);
  if (installed[id].ok && m) e.version = m[1];
}
fs.writeFileSync(file, `${JSON.stringify(doc, null, 2)}\n`);
for (const id of Object.keys(installed)) console.log(id.padEnd(13), "installed=" + installed[id].ok, "authenticated=" + authed[id].ok, "|", installed[id].detail);
JS
