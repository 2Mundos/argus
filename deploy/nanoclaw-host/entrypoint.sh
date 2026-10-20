#!/bin/sh
# Start one NanoClaw host as an Argus instance.
#
# Inputs (set by Argus from the pool's `nanoclaw` options and the plain-runtime
# contract): ARGUS_NANOCLAW_INSTALL_DIR, ARGUS_NANOCLAW_INSTANCES_ROOT,
# ARGUS_INSTANCE_ID, ARGUS_URL, ARGUS_BOOTSTRAP_TOKENS, ARGUS_AGENT_NAME,
# CONTAINER_IMAGE, ONECLI_URL. Optional (the pool's `env`): ARGUS_CODEX_SANDBOX_PROFILE=codex-bwrap.
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

# Optional per-pool Codex sandbox profile (docs/specs/codex-sandbox-profile.spec.md):
# refuse to boot on a value the driver would refuse at spawn, instead of failing task by task.
if [ -n "${ARGUS_CODEX_SANDBOX_PROFILE:-}" ]; then
  [ "$ARGUS_CODEX_SANDBOX_PROFILE" = "codex-bwrap" ] || {
    echo "ARGUS_CODEX_SANDBOX_PROFILE must be unset or codex-bwrap, got '$ARGUS_CODEX_SANDBOX_PROFILE'" >&2
    exit 78
  }
  [ "${ARGUS_NANOCLAW_INNER_DOCKER:-}" != "1" ] || {
    echo "ARGUS_CODEX_SANDBOX_PROFILE is not supported with a private inner Docker daemon (nanoclaw-sandbox)" >&2
    exit 78
  }
  [ -f "$INSTALL/security/argus-codex-worker.seccomp.json" ] || {
    echo "no seccomp profile in $INSTALL/security (run integrations/nanoclaw/install.sh)" >&2
    exit 78
  }
fi

# NanoClaw roots everything at its working directory: state is per instance,
# code and the files it bind-mounts into agents are shared from the install.
mkdir -p "$WORK/data" "$WORK/groups" "$WORK/store"
for shared in container templates package.json; do
  ln -sfn "$INSTALL/$shared" "$WORK/$shared"
done
# Sandboxed mode (pool docker.runtime, e.g. sysbox): no host socket; run a
# private daemon and give it the agent image.
if [ "${ARGUS_NANOCLAW_INNER_DOCKER:-}" = "1" ]; then
  # Agent containers reach the OneCLI gateway as host.docker.internal:host-gateway.
  # Inside this sandbox that name would point at the instance itself, so make it
  # the address the instance resolves for the real Docker host (extra_hosts).
  gw="$(getent ahostsv4 host.docker.internal 2>/dev/null | awk 'NR==1{print $1}')"
  dockerd ${gw:+--host-gateway-ip="$gw"} >/var/log/dockerd.log 2>&1 &
  n=0
  until docker info >/dev/null 2>&1; do
    n=$((n + 1))
    [ "$n" -le 60 ] || { echo "inner dockerd did not start:" >&2; tail -20 /var/log/dockerd.log >&2; exit 70; }
    sleep 1
  done
  if ! docker image inspect "${CONTAINER_IMAGE:?}" >/dev/null 2>&1; then
    if [ -n "${ARGUS_NANOCLAW_AGENT_IMAGE_ARCHIVE:-}" ]; then
      docker load -i "$ARGUS_NANOCLAW_AGENT_IMAGE_ARCHIVE" >/dev/null
    else
      docker pull "$CONTAINER_IMAGE" >/dev/null
    fi
  fi
fi
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
