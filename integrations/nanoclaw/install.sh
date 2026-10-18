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
cp "$here/argus-onecli-git-ca.ts" "$target/src/gateway-providers/argus-onecli-git-ca.ts"
cp "$here/argus-onecli-git-ca.test.ts" "$target/src/gateway-providers/argus-onecli-git-ca.test.ts"
cp "$here/onecli-path-match.ts" "$target/src/gateway-providers/onecli-path-match.ts"
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
runtime_keys = ["CURSOR_CONFIG_DIR", "XDG_CONFIG_HOME", "ARGUS_REPOSITORIES_JSON"]
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

echo "installed. Next: (cd $target && pnpm run build && pnpm exec vitest run src/channels/argus.test.ts)"
