#!/bin/sh
# Start one NanoClaw host as an Argus instance.
#
# Inputs (set by Argus from the pool's `nanoclaw` options and the plain-runtime
# contract): ARGUS_NANOCLAW_INSTALL_DIR, ARGUS_NANOCLAW_INSTANCES_ROOT,
# ARGUS_INSTANCE_ID, ARGUS_URL, ARGUS_BOOTSTRAP_TOKENS, ARGUS_AGENT_NAME,
# CONTAINER_IMAGE, ONECLI_URL.
set -eu

INSTALL="${ARGUS_NANOCLAW_INSTALL_DIR:?}"
WORK="${ARGUS_NANOCLAW_INSTANCES_ROOT:?}/${ARGUS_INSTANCE_ID:?}"

[ -f "$INSTALL/dist/index.js" ] || {
  echo "no built NanoClaw at $INSTALL (run: pnpm install && pnpm run build there)" >&2
  exit 78
}
[ -f "$INSTALL/dist/channels/argus.js" ] || {
  echo "the Argus channel is not installed/built in $INSTALL (integrations/nanoclaw/install.sh)" >&2
  exit 78
}

# NanoClaw roots everything at its working directory: state is per instance,
# code and the files it bind-mounts into agents are shared from the install.
mkdir -p "$WORK/data" "$WORK/groups" "$WORK/store"
for shared in container templates package.json; do
  ln -sfn "$INSTALL/$shared" "$WORK/$shared"
done
cd "$WORK"

# Upgrade tripwire: stamp a marker only for a fresh working directory, never
# overwrite one, so a later code upgrade still trips it as NanoClaw intends.
# No .git here, so commit/tree are 'unknown' (the documented no-git case).
if [ ! -e data/upgrade-state.json ]; then
  version="$(node -p "require('$INSTALL/package.json').version")"
  printf '{"version":"%s","commit":"unknown","tree":"unknown","updatedAt":"%s","via":"argus-runtime"}\n' \
    "$version" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > data/upgrade-state.json
fi

# Agent group + wiring to the Argus channel (idempotent).
"$INSTALL/node_modules/.bin/tsx" "$INSTALL/scripts/init-argus-agent.ts" \
  --agent-name "${ARGUS_AGENT_NAME:-argus-worker}"

exec node "$INSTALL/dist/index.js"
