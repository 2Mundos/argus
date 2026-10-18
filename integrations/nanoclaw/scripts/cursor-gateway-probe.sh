#!/usr/bin/env bash
# Replayable live probe against the scratch OneCLI stack (fake tokens only).
# Prereq: argus-cursor-scratch-onecli (+ upstream) from ~/.hermes/cache/scratch/argus-cursor-work.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
NAME="${ARGUS_CURSOR_ONECLI_CONTAINER:-argus-cursor-scratch-onecli}"
IP="$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$NAME" 2>/dev/null || true)"
if [ -z "$IP" ]; then
  echo "container $NAME not running; start scratch stack first" >&2
  exit 2
fi
export ONECLI_URL="http://${IP}:10254"
export CURSOR_PROBE_AGENT_ID="${CURSOR_PROBE_AGENT_ID:-scratch-agent}"
cd "$ROOT"
chmod +x integrations/nanoclaw/scripts/cursor-gateway-native-smoke.sh
pnpm exec tsx integrations/nanoclaw/scripts/cursor-gateway-probe.ts
echo "probe ok: augment + native RO-stub smoke verified against scratch OneCLI"
