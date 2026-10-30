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
cp "$here/argus-local-forward.ts" "$target/src/channels/argus-local-forward.ts"
cp "$here/argus-local-forward.test.ts" "$target/src/channels/argus-local-forward.test.ts"
cp "$here/argus-accounts.ts" "$target/src/channels/argus-accounts.ts"
cp "$here/argus-usage-relay.ts" "$target/src/channels/argus-usage-relay.ts"
cp "$here/argus-usage-relay.test.ts" "$target/src/channels/argus-usage-relay.test.ts"
cp "$here/argus-changes-relay.ts" "$target/src/channels/argus-changes-relay.ts"
cp "$here/argus-changes-relay.test.ts" "$target/src/channels/argus-changes-relay.test.ts"
cp "$here/argus-workspace.ts" "$target/src/channels/argus-workspace.ts"
cp "$here/argus-workspace.test.ts" "$target/src/channels/argus-workspace.test.ts"
cp "$here/argus-checkpoint.ts" "$target/src/channels/argus-checkpoint.ts"
cp "$here/argus-checkpoint.test.ts" "$target/src/channels/argus-checkpoint.test.ts"
cp "$here/argus-repo-preflight.ts" "$target/src/channels/argus-repo-preflight.ts"
cp "$here/argus-repo-preflight.test.ts" "$target/src/channels/argus-repo-preflight.test.ts"
cp "$here/argus-repo-preflight.docker.test.ts" "$target/src/channels/argus-repo-preflight.docker.test.ts"
cp "$here/argus.test.ts" "$target/src/channels/argus.test.ts"
cp "$here/argus-accounts.test.ts" "$target/src/channels/argus-accounts.test.ts"
cp "$here/argus-runtime.test.ts" "$target/src/channels/argus-runtime.test.ts"
# The quota patterns' shared fixture (Argus's packages/core), read by argus-accounts.test.ts.
cp "$here/../../packages/core/tests/fixtures/quota-notices.json" "$target/src/channels/argus-quota-notices.json"
cp "$here/argus-gitea-lease.test.ts" "$target/src/channels/argus-gitea-lease.test.ts"
# The continuation contract tests and the real-API claims they render (task continuation spec).
cp "$here/argus-continuation.test.ts" "$target/src/channels/argus-continuation.test.ts"
cp "$here/argus-continuation-observe.ts" "$target/src/channels/argus-continuation-observe.ts"
cp "$here/argus-continuation-observe.test.ts" "$target/src/channels/argus-continuation-observe.test.ts"
# Reply fencing: output of an earlier task never applies to the task claimed next.
cp "$here/argus-reply-fence.test.ts" "$target/src/channels/argus-reply-fence.test.ts"
cp "$here/argus-stall-causes.test.ts" "$target/src/channels/argus-stall-causes.test.ts"
cp "$here/argus-claim-isolation.test.ts" "$target/src/channels/argus-claim-isolation.test.ts"
mkdir -p "$target/src/channels/fixtures"
rm -rf "$target/src/channels/fixtures/continuation"
cp -r "$here/fixtures/continuation" "$target/src/channels/fixtures/continuation"
cp "$here/argus-codex-stub.ts" "$target/src/gateway-providers/argus-codex-stub.ts"
cp "$here/argus-codex-stub.test.ts" "$target/src/gateway-providers/argus-codex-stub.test.ts"
cp "$here/argus-cursor-stub.ts" "$target/src/gateway-providers/argus-cursor-stub.ts"
cp "$here/argus-cursor-stub.test.ts" "$target/src/gateway-providers/argus-cursor-stub.test.ts"
cp "$here/argus-claude-stub.ts" "$target/src/gateway-providers/argus-claude-stub.ts"
cp "$here/argus-claude-stub.test.ts" "$target/src/gateway-providers/argus-claude-stub.test.ts"
cp "$here/engines/anthropic-redirect.mjs" "$target/src/gateway-providers/anthropic-redirect.mjs"
mkdir -p "$target/src/gateway-providers/engines" "$target/dist/gateway-providers/engines"
# cp takes one destination: copy into each directory separately.
for dir in "$target/src/gateway-providers/engines" "$target/dist/gateway-providers/engines"; do
  cp "$here/engines/account-settings.mjs" "$here/engines/anthropic-openai-bridge.mjs" "$dir/"
done
cp "$here/engines/anthropic-openai-bridge.d.mts" "$target/src/gateway-providers/engines/"
cp "$here/argus-local-harness.ts" "$target/src/gateway-providers/argus-local-harness.ts"
cp "$here/argus-local-harness.test.ts" "$target/src/gateway-providers/argus-local-harness.test.ts"
cp "$here/argus-custom-harness.ts" "$target/src/gateway-providers/argus-custom-harness.ts"
cp "$here/argus-custom-harness.test.ts" "$target/src/gateway-providers/argus-custom-harness.test.ts"
cp "$here/argus-custom-bridge.ts" "$target/src/gateway-providers/argus-custom-bridge.ts"
cp "$here/argus-custom-bridge.test.ts" "$target/src/gateway-providers/argus-custom-bridge.test.ts"
# The harness and forwarder import "./engines/anthropic-redirect.mjs" (repo layout); give both
# installed directories that path, with types for tsc and the runtime copy in dist/.
for dir in channels gateway-providers; do
  mkdir -p "$target/src/$dir/engines" "$target/dist/$dir/engines"
  cp "$here/engines/anthropic-redirect.mjs" "$target/src/$dir/engines/anthropic-redirect.mjs"
  cp "$here/engines/anthropic-redirect.mjs" "$target/dist/$dir/engines/anthropic-redirect.mjs"
  cp "$here/engines/anthropic-redirect.d.mts" "$target/src/$dir/engines/anthropic-redirect.d.mts"
done
# The channel imports the harness helpers from its own directory.
printf '%s\n' "export * from '../gateway-providers/argus-local-harness.js';" > "$target/src/channels/argus-local-harness.ts"
for mod in argus-custom-harness argus-custom-bridge; do
  printf '%s\n' "export * from '../gateway-providers/$mod.js';" > "$target/src/channels/$mod.ts"
done
# ...and the custom bridge reaches the channel's Strata forwarder from its own.
printf '%s\n' "export * from '../channels/argus-local-forward.js';" > "$target/src/gateway-providers/argus-local-forward.ts"
cp "$here/argus-onecli-git-ca.ts" "$target/src/gateway-providers/argus-onecli-git-ca.ts"
cp "$here/argus-onecli-git-ca.test.ts" "$target/src/gateway-providers/argus-onecli-git-ca.test.ts"
cp "$here/argus-startup-order.test.ts" "$target/src/channels/argus-startup-order.test.ts"
cp "$here/argus-onecli-preflight-lease.test.ts" "$target/src/gateway-providers/argus-onecli-preflight-lease.test.ts"
cp "$here/onecli-path-match.ts" "$target/src/gateway-providers/onecli-path-match.ts"
mkdir -p "$target/src/gateway-providers/fixtures"
cp -r "$here/fixtures/cursor-gateway" "$target/src/gateway-providers/fixtures/"
cp "$here/init-argus-agent.ts" "$target/scripts/init-argus-agent.ts"
# Shared helper (also exercised by offline installer tests).
bash "$here/install-gitea-api.sh" copy-nanoclaw "$target"
bash "$here/install-gitea-api.sh" patch-nanoclaw-dockerfile "$target"

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
# Claude Code strips CLAUDE_CODE_OAUTH_TOKEN from its tool subprocesses: hand the gateway's
# sentinel (never a real token) to argus-delegate through a read-only stub (argus-claude-stub.ts).
if [ -f "$onecli_ts" ] && ! grep -q "withClaudeDelegateRuntime" "$onecli_ts"; then
  python3 - "$onecli_ts" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
needle = "import { cursorExecutorAssigned, withCursorDelegateRuntime } from './argus-cursor-stub.js';"
assert needle in s, "run install after the cursor delegate hook is present"
s = s.replace(needle, needle + "\nimport { withClaudeDelegateRuntime } from './argus-claude-stub.js';", 1)
old_head = "  const config = await withCursorDelegateRuntime("
old_tail = "        : false,\n  );\n  log.info('OneCLI gateway applied'"
assert old_head in s and old_tail in s, "onecli.ts ensureSession shape changed upstream"
s = s.replace(old_head, "  const config = withClaudeDelegateRuntime(await withCursorDelegateRuntime(", 1)
s = s.replace(old_tail, "        : false,\n  ));\n  log.info('OneCLI gateway applied'", 1)
open(p, "w").write(s)
PY
  echo "patched $onecli_ts (Claude delegate sentinel stub)"
fi
if [ -f "$onecli_ts" ] && ! grep -q "withLocalHarnessRuntimeForGroup" "$onecli_ts"; then
  python3 - "$onecli_ts" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
needle = "import { withClaudeDelegateRuntime } from './argus-claude-stub.js';"
assert needle in s, "run install after the Claude delegate hook is present"
s = s.replace(
    needle,
    needle + "\nimport { withLocalHarnessRuntimeForGroup } from './argus-local-harness.js';",
    1,
)
old = "  const config = withClaudeDelegateRuntime(await withCursorDelegateRuntime("
new = "  const config = await withLocalHarnessRuntimeForGroup(withClaudeDelegateRuntime(await withCursorDelegateRuntime("
assert old in s, "onecli.ts ensureSession shape changed upstream"
s = s.replace(old, new, 1)
old_tail = "        : false,\n  ));\n  log.info('OneCLI gateway applied'"
new_tail = "        : false,\n  )), DATA_DIR, input.key.agentGroupId);\n  log.info('OneCLI gateway applied'"
assert old_tail in s, "onecli.ts ensureSession tail changed upstream"
open(p, "w").write(s.replace(old_tail, new_tail, 1))
PY
  echo "patched $onecli_ts (local Strata harness env)"
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
if [ -f "$onecli_ts" ] && ! grep -q "argus-onecli-git-ca" "$onecli_ts"; then
  python3 - "$onecli_ts" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
needle = "import { cursorExecutorAssigned, withCursorDelegateRuntime } from './argus-cursor-stub.js';"
assert needle in s, "run install after the cursor delegate hook is present"
s = s.replace(
    needle,
    needle + "\nimport { applyGitSslCaInfo } from './argus-onecli-git-ca.js';",
    1,
)
old = "  for (const stub of config.credentialStubs ?? []) mount('stub', stub.content, stub.containerPath);\n  return { env, mounts };"
new = """  for (const stub of config.credentialStubs ?? []) mount('stub', stub.content, stub.containerPath);
  applyGitSslCaInfo(env, config.caCertificateContainerPath);
  return { env, mounts };"""
assert old in s, "onecli.ts contributionFromConfig shape changed upstream"
open(p, "w").write(s.replace(old, new, 1))
PY
  echo "patched $onecli_ts (GIT_SSL_CAINFO for git smart HTTP)"
fi
if [ -f "$onecli_test" ]; then
  patch_result="$(
    python3 - "$onecli_test" <<'PY'
import sys
p = sys.argv[1]
original = open(p).read()
s = original
canonical = """    const contributionEnv = lease.contribution?.env;
    if (!contributionEnv) throw new Error('missing contribution env');
    // GIT_SSL_CAINFO tracks the combined CA bundle when the host trust store is available.
    expect(contributionEnv.GIT_SSL_CAINFO).toBe(
      contributionEnv.SSL_CERT_FILE ?? '/tmp/onecli-ca.pem',
    );"""
legacy = """    // GIT_SSL_CAINFO tracks the combined CA bundle when the host trust store is available.
    expect(lease.contribution.env.GIT_SSL_CAINFO).toBe(
      lease.contribution.env.SSL_CERT_FILE ?? '/tmp/onecli-ca.pem',
    );"""
for bad in (
    "        GIT_SSL_CAINFO: '/tmp/onecli-ca.pem',\n",
    "        GIT_SSL_CAINFO: '/tmp/onecli-combined-ca.pem',\n",
):
    s = s.replace(bad, "")
if legacy in s:
    s = s.replace(legacy, canonical)
elif canonical not in s:
    anchor = """    });
    expect(withProviderEnv({}, '')).toEqual({});"""
    insert = f"""    }});
{canonical}
    expect(withProviderEnv({{}}, '')).toEqual({{}});"""
    assert anchor in s, "onecli session contribution test shape changed upstream"
    s = s.replace(anchor, insert, 1)
if s != original:
    open(p, "w").write(s)
    print("patched")
PY
  )"
  if [ "$patch_result" = patched ]; then
    echo "patched $onecli_test (expects GIT_SSL_CAINFO on gateway contribution)"
  fi
fi
# Upstream's onecli.test.ts never opens NanoClaw's database, but the local-harness
# hook patched into ensureSession above looks the agent group up there, so the
# lookup threw "Database not initialized". The test now stubs only getAgentGroup
# to find no group, which takes the hook's no-group path (config unchanged); the
# group-found path is covered by argus-local-harness.test.ts. The mock goes right
# after the test's vitest import (vi.mock is hoisted; an import list split over
# lines matches too, checked in the script); no such import stops the install,
# like every other anchor here.
if [ -f "$onecli_test" ] && grep -q "withLocalHarnessRuntimeForGroup" "$onecli_ts" \
    && ! grep -q "vi.mock('../db/agent-groups.js'" "$onecli_test"; then
  patch_result="$(
    python3 - "$onecli_test" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
import re
mock = """
// Argus: the local-harness hook in ensureSession looks the agent group up in
// NanoClaw's database, which this test never opens; stub the lookup (install.sh).
// The rest of the module stays real: other modules in the graph import it.
vi.mock('../db/agent-groups.js', async (original) => ({
  ...(await original<typeof import('../db/agent-groups.js')>()),
  getAgentGroup: async () => undefined,
}));
"""
# [^;] also matches newlines, so an import list split over lines still matches.
vitest_import = re.compile(r"^import [^;]*\bfrom ['\"]vitest['\"];?[ \t]*\n", re.M)
assert vitest_import.search("import {\n  describe,\n  it,\n} from 'vitest';\n")
first = vitest_import.search(s)
assert first, "onecli test has no vitest import to anchor the mock on"
at = first.end()
open(p, "w").write(s[:at] + mock + s[at:])
print("patched")
PY
  )"
  if [ "$patch_result" = patched ]; then
    echo "patched $onecli_test (agent group lookup for the local-harness hook)"
  fi
fi

codex_ts="$target/container/agent-runner/src/providers/codex-app-server.ts"
if [ -f "$codex_ts" ]; then
  patch_result="$(
    python3 - "$codex_ts" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
original = s
if "GIT_HTTP_PROXY_AUTHMETHOD" not in s:
    old = "  'GIT_SSL_CAINFO',\n  'HOME',"
    new = "  'GIT_SSL_CAINFO',\n  'GIT_HTTP_PROXY_AUTHMETHOD',\n  'GIT_TERMINAL_PROMPT',\n  'HOME',"
    assert old in s, "codex-app-server CODEX_ENV_ALLOWLIST shape changed upstream"
    s = s.replace(old, new, 1)
runtime_keys = ["CURSOR_CONFIG_DIR", "XDG_CONFIG_HOME", "ARGUS_REPOSITORIES_JSON", "ARGUS_CODEX_TURN_TIMEOUT_MS"]
missing = [k for k in runtime_keys if f"  '{k}',\n" not in s]
if missing:
    insert = "".join(f"  '{k}',\n" for k in missing)
    anchor = "  'CODEX_HOME',\n"
    assert anchor in s, "codex-app-server CODEX_ENV_ALLOWLIST missing CODEX_HOME anchor"
    s = s.replace(anchor, insert + anchor, 1)
if s != original:
    open(p, "w").write(s)
    print("patched")
PY
  )"
  if [ "$patch_result" = patched ]; then
    echo "patched $codex_ts (Codex env allowlist: git, Cursor delegate, Gitea map)"
  fi
fi

container_runner="$target/src/container-runner.ts"
if [ -f "$container_runner" ] && ! grep -q "argusRepositoriesJson" "$container_runner"; then
  python3 - "$container_runner" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
old = """  const contributedEnv: Record<string, string> = {
    ...(contribution.env ?? {}),
    ...(gateway.env ?? {}),
  };"""
new = """  const contributedEnv: Record<string, string> = {
    ...(contribution.env ?? {}),
    ...(gateway.env ?? {}),
  };
  const argusRepositoriesJson = process.env.ARGUS_REPOSITORIES_JSON?.trim();
  if (argusRepositoriesJson) {
    contributedEnv.ARGUS_REPOSITORIES_JSON = argusRepositoriesJson;
  }"""
assert old in s, "container-runner composeSessionSpec contributedEnv block changed upstream"
open(p, "w").write(s.replace(old, new, 1))
PY
  echo "patched $container_runner (host ARGUS_REPOSITORIES_JSON → agent contributedEnv)"
fi

codex_test="$target/container/agent-runner/src/providers/codex-app-server.test.ts"
if [ -f "$codex_test" ]; then
  patch_result="$(
    python3 - "$codex_test" <<'PY'
import sys
p = sys.argv[1]
original = open(p).read()
s = original
marker = "GIT_HTTP_PROXY_AUTHMETHOD for OneCLI git smart HTTP"
if marker not in s:
    s = s.replace(
        """      HTTPS_PROXY: 'http://proxy',
      OPENAI_API_KEY: 'sk-test',""",
        """      HTTPS_PROXY: 'http://proxy',
      GIT_SSL_CAINFO: '/tmp/onecli-ca.pem',
      GIT_HTTP_PROXY_AUTHMETHOD: 'basic',
      GIT_TERMINAL_PROMPT: '0',
      OPENAI_API_KEY: 'sk-test',""",
        1,
    )
    s = s.replace(
        """    expect(env.HTTPS_PROXY).toBe('http://proxy');
    expect(env.OPENAI_API_KEY).toBeUndefined();""",
        """    expect(env.HTTPS_PROXY).toBe('http://proxy');
    // GIT_HTTP_PROXY_AUTHMETHOD for OneCLI git smart HTTP through the SDK proxy tunnel.
    expect(env.GIT_SSL_CAINFO).toBe('/tmp/onecli-ca.pem');
    expect(env.GIT_HTTP_PROXY_AUTHMETHOD).toBe('basic');
    expect(env.GIT_TERMINAL_PROMPT).toBe('0');
    expect(env.OPENAI_API_KEY).toBeUndefined();""",
        1,
    )
    assert marker in s, "codex-app-server env test shape changed upstream"
runtime_marker = "forwards Cursor delegate and Gitea allowlist env"
if runtime_marker not in s:
    s = s.replace(
        """      GIT_TERMINAL_PROMPT: '0',
      OPENAI_API_KEY: 'sk-test',""",
        """      GIT_TERMINAL_PROMPT: '0',
      CURSOR_CONFIG_DIR: '/home/node/.config/cursor',
      XDG_CONFIG_HOME: '/home/node/.config',
      ARGUS_REPOSITORIES_JSON: '{"radar":"https://git.example.test/radar/radar.git"}',
      OPENAI_API_KEY: 'sk-test',""",
        1,
    )
    s = s.replace(
        """    expect(env.GIT_TERMINAL_PROMPT).toBe('0');
    expect(env.OPENAI_API_KEY).toBeUndefined();""",
        """    expect(env.GIT_TERMINAL_PROMPT).toBe('0');
    // forwards Cursor delegate and Gitea allowlist env
    expect(env.CURSOR_CONFIG_DIR).toBe('/home/node/.config/cursor');
    expect(env.XDG_CONFIG_HOME).toBe('/home/node/.config');
    expect(env.ARGUS_REPOSITORIES_JSON).toBe(
      '{"radar":"https://git.example.test/radar/radar.git"}',
    );
    expect(env.OPENAI_API_KEY).toBeUndefined();""",
        1,
    )
    assert "expect(env.CURSOR_CONFIG_DIR)" in s, "codex-app-server env test runtime keys missing"
if s != original:
    open(p, "w").write(s)
    print("patched")
PY
  )"
  if [ "$patch_result" = patched ]; then
    echo "patched $codex_test (Codex env forwards git, Cursor delegate, Gitea map)"
  fi
fi

providers_dir="$target/container/agent-runner/src/providers"
mkdir -p "$providers_dir"
cp "$here/overlays/codex-turn-timeout.ts" "$here/overlays/codex-turn-timeout.test.ts" "$providers_dir/"
# Claude Code's quota block ("You've hit your session limit · resets 2am (UTC)") reaches the
# channel as a structured providerFailure, never as upstream text.
cp "$here/overlays/claude-rate-limit.ts" "$here/overlays/claude-rate-limit.test.ts" \
  "$here/overlays/claude.rate-limit.test.ts" "$providers_dir/"
# Codex's usage limit ("You've hit your usage limit … try again at …") likewise.
cp "$here/overlays/codex-usage-limit.ts" "$here/overlays/codex-usage-limit.test.ts" "$providers_dir/"
python3 "$here/patch-claude-rate-limit.py" "$target"
# The harness's own token usage (claude.ts, codex.ts) for the host to post (account usage spec 3.9).
cp "$here/overlays/harness-usage.ts" "$here/overlays/harness-usage.test.ts" "$providers_dir/"
python3 "$here/patch-harness-usage.py" "$target"

codex_provider="$providers_dir/codex.ts"
if [ -f "$codex_provider" ]; then
  patch_result="$(
    python3 - "$codex_provider" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
original = s
marker = "from './codex-turn-timeout.js'"
if marker not in s:
    anchor = "} from './codex-app-server.js';"
    assert anchor in s, "codex.ts import block changed upstream"
    s = s.replace(
        anchor,
        "} from './codex-app-server.js';\nimport { resolveCodexTurnTimeoutMs } from './codex-turn-timeout.js';",
        1,
    )
s = s.replace("const TURN_TIMEOUT_MS = 10 * 60 * 1000;\n", "")
turn_anchor = "  let turnId: string | null = null;\n"
turn_insert = (
    "  let turnId: string | null = null;\n\n"
    "  const turnTimeoutMs = resolveCodexTurnTimeoutMs();\n"
)
if "const turnTimeoutMs = resolveCodexTurnTimeoutMs();" not in s:
    assert turn_anchor in s, "runOneTurn turnId declaration changed upstream"
    s = s.replace(turn_anchor, turn_insert, 1)
old_timer = """  const timer = setTimeout(() => {
    state.error = new Error(`Turn timed out after ${TURN_TIMEOUT_MS}ms`);
    finishTurn();
    kick();
  }, TURN_TIMEOUT_MS);"""
new_timer = """  const timer = setTimeout(() => {
    if (!turnDone && turnId) {
      void interruptCodexTurn(server, threadId, turnId).catch(() => {});
    }
    state.error = new Error(`Turn timed out after ${turnTimeoutMs}ms`);
    finishTurn();
    kick();
  }, turnTimeoutMs);"""
if old_timer in s:
    s = s.replace(old_timer, new_timer, 1)
elif "interruptCodexTurn(server, threadId, turnId)" not in s:
    partial = """  const timer = setTimeout(() => {
    state.error = new Error(`Turn timed out after ${turnTimeoutMs}ms`);
    finishTurn();
    kick();
  }, turnTimeoutMs);"""
    assert partial in s, "codex.ts turn timer block changed upstream"
    s = s.replace(
        partial,
        new_timer,
        1,
    )
if s != original:
    open(p, "w").write(s)
    print("patched")
PY
  )"
  if [ "$patch_result" = patched ]; then
    echo "patched $codex_provider (configurable Codex turn timeout + interrupt on expiry)"
  fi
fi

# Codex sandbox profile, opt-in per worker (docs/specs/codex-sandbox-profile.spec.md).
# Files only: nothing here loads AppArmor, needs root, or turns the profile on. The
# seccomp JSON lands at <checkout>/security/, the one fixed path the driver reads.
cp "$here/argus-codex-sandbox.ts" "$target/src/drivers/argus-codex-sandbox.ts"
cp "$here/argus-codex-sandbox.test.ts" "$target/src/drivers/argus-codex-sandbox.test.ts"
cp "$here/argus-codex-sandbox-composition.test.ts" "$target/src/argus-codex-sandbox-composition.test.ts"
mkdir -p "$target/security"
cp "$here/security/argus-codex-worker.seccomp.json" "$target/security/argus-codex-worker.seccomp.json"

# Idempotent and upgrading: also rewrites an earlier version of these hooks (patch-codex-sandbox.py).
python3 "$here/patch-codex-sandbox.py" "$target"

# Bounded docker rm/ps removal confirmation (session teardown race).
cp "$here/overlays/docker-stop-removal.ts" "$here/overlays/docker-stop-removal.test.ts" \
  "$here/overlays/docker-driver-teardown.test.ts" "$target/src/drivers/"
python3 "$here/patch-docker-teardown.py" "$target"

# Pre-create session bind-mount stubs as the host uid (workspace-cleanup.spec.md 3.3).
cp "$here/overlays/session-mount-stubs.ts" "$here/overlays/session-mount-stubs.test.ts" \
  "$target/src/"
python3 "$here/patch-session-mount-stubs.py" "$target"

# The repository preflight session takes its gateway lease through the worker path's admission gate.
python3 "$here/patch-gateway-session-export.py" "$target"
# Claims and preflight sessions wait for startup adoption, which stops any session without a database row.
python3 "$here/patch-startup-adoption-gate.py" "$target"

# No blanket running-session ceiling (reconcile-session.ts): only a stale message claim kills.
python3 "$here/patch-no-session-ceiling.py" "$target"

echo "installed. Next: (cd $target && pnpm run build && pnpm exec vitest run src/channels/argus.test.ts)"
