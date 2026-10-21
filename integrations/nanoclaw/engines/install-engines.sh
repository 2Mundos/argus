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
cp "$here/install-cursor-agent.sh" "$here/argus-delegate.mjs" "$here/account-settings.mjs" "$here/anthropic-redirect.mjs" "$here/model-policy.mjs" "$here/model-policy.defaults.json" "$here/milestones.mjs" "$here/livelog.mjs" "$here/usage.mjs" "$target/container/"
cp "$here/../gitea-api.mjs" "$target/container/"
python3 "$here/patch-engines-dockerfile.py" "$target/container/Dockerfile"
echo "engines overlay applied to $target"
