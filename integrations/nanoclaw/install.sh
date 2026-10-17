#!/usr/bin/env bash
# Install the Argus channel into a NanoClaw checkout.
#   integrations/nanoclaw/install.sh /path/to/nanoclaw
# Idempotent: copies the channel, its tests and the wiring script, and appends
# one import to the channels barrel if it is not there yet. NanoClaw has no
# plugin loader, so this file copy is the installation.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="${1:?usage: install.sh /path/to/nanoclaw}"
[ -f "$target/src/channels/index.ts" ] || { echo "not a NanoClaw checkout: $target" >&2; exit 1; }

mkdir -p "$target/dist/channels"
for dir in "$target/src/channels" "$target/dist/channels"; do
  cp "$here/engines/model-policy.mjs" "$here/engines/model-policy.defaults.json" "$dir/"
done
cp "$here/engines/model-policy.d.mts" "$target/src/channels/"
cp "$here/argus.ts" "$target/src/channels/argus.ts"
cp "$here/argus-accounts.ts" "$target/src/channels/argus-accounts.ts"
cp "$here/argus.test.ts" "$target/src/channels/argus.test.ts"
cp "$here/argus-accounts.test.ts" "$target/src/channels/argus-accounts.test.ts"
cp "$here/argus-codex-stub.ts" "$target/src/gateway-providers/argus-codex-stub.ts"
cp "$here/argus-codex-stub.test.ts" "$target/src/gateway-providers/argus-codex-stub.test.ts"
cp "$here/init-argus-agent.ts" "$target/scripts/init-argus-agent.ts"

barrel="$target/src/channels/index.ts"
if ! grep -q "import './argus.js';" "$barrel"; then
  printf "\n// Argus fleet worker channel (integrations/nanoclaw in the Argus repo).\nimport './argus.js';\n" >> "$barrel"
  echo "added the import to $barrel"
fi
# The Codex sentinel auth.json must name the real workspace (argus-codex-stub.ts).
onecli_ts="$target/src/gateway-providers/onecli.ts"
if [ -f "$onecli_ts" ] && ! grep -q "argus-codex-stub" "$onecli_ts"; then
  python3 - "$onecli_ts" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
s = s.replace("import { DATA_DIR } from '../config.js';", "import { DATA_DIR } from '../config.js';\nimport { codexAccountId, withMirroredCodexAccount } from './argus-codex-stub.js';", 1)
old = "  const config = await onecli.getContainerConfig({ agent: input.key.agentGroupId });"
new = "  const config = await withMirroredCodexAccount(\n    await onecli.getContainerConfig({ agent: input.key.agentGroupId }),\n    async () => (onecliUrl ? codexAccountId(onecliUrl, onecliApiKey, input.key.agentGroupId) : null),\n  );"
assert old in s and "argus-codex-stub" in s
open(p, "w").write(s.replace(old, new, 1))
PY
  echo "patched $onecli_ts"
fi
echo "installed. Next: (cd $target && pnpm run build && pnpm exec vitest run src/channels/argus.test.ts)"
