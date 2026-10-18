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
cp "$here/argus-gitea-lease.test.ts" "$target/src/channels/argus-gitea-lease.test.ts"
cp "$here/argus-codex-stub.ts" "$target/src/gateway-providers/argus-codex-stub.ts"
cp "$here/argus-codex-stub.test.ts" "$target/src/gateway-providers/argus-codex-stub.test.ts"
cp "$here/argus-cursor-stub.ts" "$target/src/gateway-providers/argus-cursor-stub.ts"
cp "$here/argus-cursor-stub.test.ts" "$target/src/gateway-providers/argus-cursor-stub.test.ts"
mkdir -p "$target/src/gateway-providers/fixtures"
cp -r "$here/fixtures/cursor-gateway" "$target/src/gateway-providers/fixtures/"
cp "$here/init-argus-agent.ts" "$target/scripts/init-argus-agent.ts"
cp "$here/gitea-api.mjs" "$target/container/gitea-api.mjs"

# The helper runs inside the agent container, not on the NanoClaw host.
python3 - "$target/container/Dockerfile" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
if "Argus Gitea API helper" not in s:
    marker = "# ---- ncl CLI wrapper"
    block = '''# ---- Argus Gitea API helper -----------------------------------------------
# The token stays in OneCLI; this client sends HTTPS through the injected proxy.
COPY gitea-api.mjs /tmp/argus-gitea-api.mjs
RUN npm install --prefix /usr/local/lib/argus-gitea --omit=dev --no-audit --no-fund undici@8.12.2 \\
 && install -m 0755 /tmp/argus-gitea-api.mjs /usr/local/bin/argus-gitea-api \\
 && rm /tmp/argus-gitea-api.mjs

'''
    assert marker in s, "NanoClaw container Dockerfile has no ncl CLI insertion marker"
    s = s.replace(marker, block + marker, 1)
open(p, "w").write(s)
PY

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
  echo "patched $onecli_ts (codex stub)"
fi
if [ -f "$onecli_ts" ] && ! grep -q "withCursorDelegateRuntime" "$onecli_ts"; then
  python3 - "$onecli_ts" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
needle = "import { codexAccountId, withMirroredCodexAccount } from './argus-codex-stub.js';"
assert needle in s, "run install after the codex stub hook is present"
if "argus-cursor-stub" not in s:
    s = s.replace(
        needle,
        needle
        + "\nimport { cursorExecutorAssigned, withCursorDelegateRuntime } from './argus-cursor-stub.js';",
        1,
    )
candidates = [
    """  const config = augmentCursorContainerConfig(await withMirroredCodexAccount(
    await onecli.getContainerConfig({ agent: input.key.agentGroupId }),
    async () => (onecliUrl ? codexAccountId(onecliUrl, onecliApiKey, input.key.agentGroupId) : null),
  ));""",
    """  const config = await withMirroredCodexAccount(
    await onecli.getContainerConfig({ agent: input.key.agentGroupId }),
    async () => (onecliUrl ? codexAccountId(onecliUrl, onecliApiKey, input.key.agentGroupId) : null),
  );""",
]
new = """  const config = await withCursorDelegateRuntime(
    await withMirroredCodexAccount(
      await onecli.getContainerConfig({ agent: input.key.agentGroupId }),
      async () => (onecliUrl ? codexAccountId(onecliUrl, onecliApiKey, input.key.agentGroupId) : null),
    ),
    async () =>
      onecliUrl
        ? cursorExecutorAssigned(onecliUrl, onecliApiKey, input.key.agentGroupId)
        : false,
  );"""
for old in candidates:
    if old in s:
        s = s.replace(old, new, 1)
        break
else:
    raise SystemExit("onecli.ts: no codex container-config hook found to wrap")
assert "withCursorDelegateRuntime" in s
open(p, "w").write(s)
PY
  echo "patched $onecli_ts (cursor delegate runtime)"
fi
onecli_test="$target/src/gateway-providers/onecli.test.ts"
if [ -f "$onecli_test" ] && grep -q 'expect(fetchMock).toHaveBeenCalledTimes(1);' "$onecli_test"; then
  python3 - "$onecli_test" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
old = "    expect(fetchMock).toHaveBeenCalledTimes(1);"
new = """    const healthFetches = fetchMock.mock.calls.filter(([url]) =>
      String(url).endsWith('/v1/health'),
    );
    expect(healthFetches).toHaveLength(1);"""
assert old in s, "onecli health-monitor test shape changed upstream"
open(p, "w").write(s.replace(old, new, 1))
PY
  echo "patched $onecli_test (health monitor counts /v1/health only)"
fi
echo "installed. Next: (cd $target && pnpm run build && pnpm exec vitest run src/channels/argus.test.ts)"
