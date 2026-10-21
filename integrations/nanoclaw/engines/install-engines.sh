#!/usr/bin/env bash
# Apply the engines overlay to a NanoClaw checkout (docs/specs/engines.spec.md):
#   integrations/nanoclaw/engines/install-engines.sh /path/to/nanoclaw
# 1. the upstream Codex provider payload, unmodified, through the seams of the
#    checkout's own `add-codex` skill (copy list, barrels);
# 2. the pinned CLIs: codex and opencode in container/cli-tools.json, the Cursor
#    agent through its checksummed install step;
# 3. argus-delegate in the agent image.
# Idempotent. It builds nothing and touches no running container.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="${1:?usage: install-engines.sh /path/to/nanoclaw}"
skill="$target/.claude/skills/add-codex/SKILL.md"
[ -f "$skill" ] || { echo "no add-codex skill in $target" >&2; exit 1; }

# The commit of upstream's `providers` branch the payload was reviewed at.
PROVIDERS_COMMIT="${PROVIDERS_COMMIT:-3959d1f055cba2320cf843b30834a278250346b8}"
CODEX_VERSION="0.162.1"
OPENCODE_VERSION="1.18.27"

git -C "$target" cat-file -e "$PROVIDERS_COMMIT^{commit}" 2>/dev/null \
  || git -C "$target" fetch --quiet origin "$PROVIDERS_COMMIT"

# 1. payload, as the skill's nc:copy directives declare
while read -r f; do
  mkdir -p "$target/$(dirname "$f")"
  git -C "$target" show "$PROVIDERS_COMMIT:$f" > "$target/$f"
done < <(awk '/^```nc:copy from-branch:providers/{f=1;next} /^```/{f=0} f&&NF' "$skill")
while read -r from _ to; do
  mkdir -p "$target/$(dirname "$to")"
  cp "$target/.claude/skills/add-codex/$from" "$target/$to"
done < <(awk '/^```nc:copy$/{f=1;next} /^```/{f=0} f&&/->/' "$skill")
for barrel in src/providers/index.ts src/provider-contracts/index.ts \
    container/agent-runner/src/provider-contracts/index.ts \
    container/agent-runner/src/providers/index.ts setup/providers/index.ts; do
  grep -q "import './codex.js';" "$target/$barrel" || printf "import './codex.js';\n" >> "$target/$barrel"
done

# 2. CLI manifest (exact versions; claude-code stays as the checkout pins it)
node - "$target/container/cli-tools.json" "$CODEX_VERSION" "$OPENCODE_VERSION" <<'JS'
const fs = require("fs");
const [file, codex, opencode] = process.argv.slice(2);
const tools = JSON.parse(fs.readFileSync(file, "utf8"));
const want = [
  { name: "@openai/codex", version: codex },
  { name: "opencode-ai", version: opencode, onlyBuilt: true },
];
for (const w of want) {
  const i = tools.findIndex((t) => t.name === w.name);
  if (i >= 0) tools[i] = w; else tools.push(w);
}
fs.writeFileSync(file, `${JSON.stringify(tools, null, 2)}\n`);
JS

# 3. image: the Cursor agent, argus-delegate, and a version smoke that fails the build
cp "$here/install-cursor-agent.sh" "$here/argus-delegate.mjs" "$here/anthropic-redirect.mjs" "$here/model-policy.mjs" "$here/model-policy.defaults.json" "$here/milestones.mjs" "$here/livelog.mjs" "$here/usage.mjs" "$target/container/"
cp "$here/../gitea-api.mjs" "$target/container/"
if ! grep -q "Argus specialist CLIs" "$target/container/Dockerfile"; then
  python3 - "$target/container/Dockerfile" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
marker = "# ---- ncl CLI wrapper"
block = '''# ---- Argus specialist CLIs ---------------------------------------------------
# claude, codex and opencode come from cli-tools.json above (pinned). The Cursor
# agent is a checksummed tarball; argus-delegate is the non-interactive entry the
# worker uses to hand work to any of the four. The smoke fails the build if a CLI
# does not run, so "in the image" is proven, not assumed. Authentication is NOT
# part of the image: each worker logs in with the vendor client.
COPY install-cursor-agent.sh argus-delegate.mjs anthropic-redirect.mjs model-policy.mjs model-policy.defaults.json milestones.mjs livelog.mjs usage.mjs gitea-api.mjs /tmp/argus-engines/
RUN sh /tmp/argus-engines/install-cursor-agent.sh \\
 && install -m 0644 /tmp/argus-engines/anthropic-redirect.mjs /usr/local/bin/anthropic-redirect.mjs \\
 && install -m 0755 /tmp/argus-engines/argus-delegate.mjs /usr/local/bin/argus-delegate \\
 && install -m 0644 /tmp/argus-engines/model-policy.mjs /tmp/argus-engines/model-policy.defaults.json /tmp/argus-engines/milestones.mjs /tmp/argus-engines/livelog.mjs /tmp/argus-engines/usage.mjs /usr/local/bin/ \\
 && install -m 0644 /tmp/argus-engines/gitea-api.mjs /usr/local/bin/gitea-api.mjs \\
 && npm install --prefix /usr/local/lib/argus-gitea --omit=dev --no-audit --no-fund undici@8.12.2 \\
 && install -m 0755 /tmp/argus-engines/gitea-api.mjs /usr/local/bin/argus-gitea-api \\
 && rm -rf /tmp/argus-engines \\
 && claude --version && codex --version && opencode --version && agent --version

'''
assert marker in s
open(p, "w").write(s.replace(marker, block + marker, 1))
PY
fi
# Also upgrade checkouts where the original Argus CLI block already exists.
if ! grep -q '^COPY model-policy.mjs model-policy.defaults.json /usr/local/bin/' "$target/container/Dockerfile"; then
  printf '\nCOPY model-policy.mjs model-policy.defaults.json /usr/local/bin/\n' >> "$target/container/Dockerfile"
fi
# Older already-patched NanoClaw checkouts need the same additions, too.
python3 - "$target/container/Dockerfile" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
import re
s = re.sub(
    r"^(COPY install-cursor-agent\.sh argus-delegate\.mjs(?: model-policy\.mjs model-policy\.defaults\.json)?) /tmp/argus-engines/$",
    r"\1 gitea-api.mjs /tmp/argus-engines/",
    s,
    flags=re.M,
)
needle = " && install -m 0755 /tmp/argus-engines/argus-delegate.mjs /usr/local/bin/argus-delegate \\\n"
if needle in s and "argus-gitea-api" not in s:
    s = s.replace(needle, needle + " && npm install --prefix /usr/local/lib/argus-gitea --omit=dev --no-audit --no-fund undici@8.12.2 \\\n && install -m 0755 /tmp/argus-engines/gitea-api.mjs /usr/local/bin/argus-gitea-api \\\n", 1)
# milestones.mjs and the sibling gitea-api.mjs (CI milestones) for already-patched checkouts.
if "milestones.mjs" not in s:
    s = s.replace("model-policy.defaults.json gitea-api.mjs /tmp/argus-engines/", "model-policy.defaults.json milestones.mjs gitea-api.mjs /tmp/argus-engines/", 1)
    s = s.replace(" && rm -rf /tmp/argus-engines \\\n", " && install -m 0644 /tmp/argus-engines/milestones.mjs /tmp/argus-engines/gitea-api.mjs /usr/local/bin/ \\\n && rm -rf /tmp/argus-engines \\\n", 1)
# livelog.mjs (the live task log) for checkouts patched before it existed.
if "livelog.mjs" not in s:
    s = s.replace("milestones.mjs gitea-api.mjs /tmp/argus-engines/", "milestones.mjs livelog.mjs gitea-api.mjs /tmp/argus-engines/", 1)
    s = s.replace(" && rm -rf /tmp/argus-engines \\\n", " && install -m 0644 /tmp/argus-engines/livelog.mjs /usr/local/bin/ \\\n && rm -rf /tmp/argus-engines \\\n", 1)
# usage.mjs (argus-delegate imports it) for checkouts patched before it was listed.
if "usage.mjs" not in s:
    s = s.replace("livelog.mjs gitea-api.mjs /tmp/argus-engines/", "livelog.mjs usage.mjs gitea-api.mjs /tmp/argus-engines/", 1)
    s = s.replace(" && rm -rf /tmp/argus-engines \\\n", " && install -m 0644 /tmp/argus-engines/usage.mjs /usr/local/bin/ \\\n && rm -rf /tmp/argus-engines \\\n", 1)
# anthropic-redirect.mjs (the delegate's redirect guard) for checkouts patched before it existed.
if "anthropic-redirect.mjs" not in s:
    s = s.replace("COPY install-cursor-agent.sh argus-delegate.mjs ", "COPY install-cursor-agent.sh argus-delegate.mjs anthropic-redirect.mjs ", 1)
    s = s.replace(" && rm -rf /tmp/argus-engines \\\n", " && install -m 0644 /tmp/argus-engines/anthropic-redirect.mjs /usr/local/bin/anthropic-redirect.mjs \\\n && rm -rf /tmp/argus-engines \\\n", 1)
assert "gitea-api.mjs /tmp/argus-engines/" in s
assert "argus-gitea-api" in s
open(p, "w").write(s)
PY
echo "engines overlay applied to $target"
