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

cp "$here/argus.ts" "$target/src/channels/argus.ts"
cp "$here/argus.test.ts" "$target/src/channels/argus.test.ts"
cp "$here/init-argus-agent.ts" "$target/scripts/init-argus-agent.ts"

barrel="$target/src/channels/index.ts"
if ! grep -q "import './argus.js';" "$barrel"; then
  printf "\n// Argus fleet worker channel (integrations/nanoclaw in the Argus repo).\nimport './argus.js';\n" >> "$barrel"
  echo "added the import to $barrel"
fi
echo "installed. Next: (cd $target && pnpm run build && pnpm exec vitest run src/channels/argus.test.ts)"
