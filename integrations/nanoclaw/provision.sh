#!/usr/bin/env bash
# Prepare everything the Argus `nanoclaw` worker template needs on a Docker host,
# in user-owned directories, from a pinned NanoClaw commit. Needs no root and
# touches no daemon configuration. It does NOT install the isolation runtime
# (sysbox) or the OneCLI credential gateway: those are host-level steps, see
# docs/runbooks/2026-10-16.nanoclaw-worker-setup.ref.md.
#
#   integrations/nanoclaw/provision.sh          # build everything
#   integrations/nanoclaw/provision.sh env      # only print the ARGUS_* lines
#   integrations/nanoclaw/provision.sh env-file [path]   # write them (0600)
#   integrations/nanoclaw/provision.sh check    # checkout, install, build and the
#                                               # vitest suite only: no Docker, no image
#
# Optional engines overlay (docs/specs/engines.spec.md, codex-accounts.spec.md):
#   ARGUS_NANOCLAW_ENGINES=1 integrations/nanoclaw/provision.sh
# adds the native Codex provider, the pinned codex/opencode/Cursor CLIs and
# argus-delegate to the checkout and the agent image (tagged "-engines"), plus
# the Codex sentinel fix. Off by default; `env` prints the matching tag.
#
# Overrides: ARGUS_NANOCLAW_STATE (default ${XDG_DATA_HOME:-~/.local/share}/argus-nanoclaw),
# NANOCLAW_COMMIT, NANOCLAW_REPO, and the image tags ARGUS_NANOCLAW_AGENT_TAG (the
# full ref; no "-engines" suffix is added to it) and ARGUS_NANOCLAW_HOST_IMAGE. A
# full run builds, tags and saves whatever those name, so a verification run on a
# host that already serves workers sets both to refs the fleet does not use.
# Idempotent: rerunning reuses what is built.
set -euo pipefail

# Pinned: the commit the integration was developed and tested against.
NANOCLAW_COMMIT="${NANOCLAW_COMMIT:-8fbacc91b6e2671d53c17fd0c866c0e2eef21715}"
NANOCLAW_REPO="${NANOCLAW_REPO:-https://github.com/qwibitai/nanoclaw.git}"
STATE="${ARGUS_NANOCLAW_STATE:-${XDG_DATA_HOME:-$HOME/.local/share}/argus-nanoclaw}"
HOST_IMAGE="${ARGUS_NANOCLAW_HOST_IMAGE:-argus-nanoclaw-host:dev}"
SRC="$STATE/src"
ROOT="$STATE/i"            # instances_root: must stay short (<= 56 chars)
ARCHIVE="$STATE/agent-image.tar"
ENGINES="${ARGUS_NANOCLAW_ENGINES:-0}"
TAG_SUFFIX=""
if [ "$ENGINES" = 1 ]; then TAG_SUFFIX="-engines"; fi
AGENT_TAG="${ARGUS_NANOCLAW_AGENT_TAG:-argus-nanoclaw-agent:${NANOCLAW_COMMIT:0:12}$TAG_SUFFIX}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"

DOCKER_SOCKET="${ARGUS_NANOCLAW_DOCKER_SOCKET:-/var/run/docker.sock}"

# Inputs for both templates; nothing here chooses one for you. The socket
# template's user/group are derived from the host (the account that owns this
# build and the Docker socket's group), never hard-coded. The socket opt-in line
# is part of the output only because the operator asked for that template on this
# node: delete it to keep nanoclaw-socket unavailable.
print_env() {
  local gid=""
  [ -S "$DOCKER_SOCKET" ] && gid="$(stat -c %g "$DOCKER_SOCKET")"
  cat <<ENV
# -- shared by both NanoClaw templates
ARGUS_NANOCLAW_INSTALL_DIR=$SRC
ARGUS_NANOCLAW_INSTANCES_ROOT=$ROOT
ARGUS_NANOCLAW_AGENT_IMAGE=$AGENT_TAG
ARGUS_NANOCLAW_HOST_IMAGE=$HOST_IMAGE
# -- optional, for guided workers to join your gateway (accounts are chosen
#    globally by priority; there is no provider list to set)
# ARGUS_NANOCLAW_ONECLI_URL=http://onecli:10254
# ARGUS_NANOCLAW_NETWORK=<docker network that reaches the gateway>
# -- nanoclaw-socket (host Docker socket: ROOT-EQUIVALENT on this Docker host)
ARGUS_NANOCLAW_ALLOW_DOCKER_SOCKET=true
ARGUS_NANOCLAW_DOCKER_SOCKET=$DOCKER_SOCKET
ARGUS_NANOCLAW_USER=$(id -u):$(id -g)
ARGUS_NANOCLAW_GROUP_ADD=$gid
# -- nanoclaw-sandbox (needs the sysbox-runc runtime on the Docker host)
ARGUS_NANOCLAW_DOCKER_RUNTIME=sysbox-runc
ARGUS_NANOCLAW_AGENT_IMAGE_ARCHIVE=$ARCHIVE
ENV
}
if [ "${1:-}" = env ]; then print_env; exit 0; fi
CHECK_ONLY=0
if [ "${1:-}" = check ]; then CHECK_ONLY=1; fi
if [ "${1:-}" = env-file ]; then
  # A non-secret file for a service manager's EnvironmentFile=; mode 0600.
  out="${2:-${XDG_CONFIG_HOME:-$HOME/.config}/argus/nanoclaw.env}"
  mkdir -p "$(dirname "$out")"
  ( umask 077; print_env > "$out" )
  echo "wrote $out"; exit 0
fi
[ "${#ROOT}" -le 56 ] || { echo "instances root too long (${#ROOT} > 56): set ARGUS_NANOCLAW_STATE shorter" >&2; exit 1; }

mkdir -p "$STATE" "$ROOT"
if [ ! -d "$SRC/.git" ]; then
  git clone --filter=blob:none "$NANOCLAW_REPO" "$SRC"
fi
git -C "$SRC" cat-file -e "$NANOCLAW_COMMIT^{commit}" 2>/dev/null || git -C "$SRC" fetch origin "$NANOCLAW_COMMIT"
git -C "$SRC" checkout --quiet --detach "$NANOCLAW_COMMIT"

"$here/install.sh" "$SRC"
# The gateway provider NanoClaw's host needs to boot (it exits at startup with
# "no gateway provider is registered" otherwise). This applies ONLY the native
# adapter of upstream's `add-onecli` skill, exactly what its nc:copy / nc:append /
# nc:dep directives declare, from the checkout's own pinned skill. It does NOT run
# the skill's setup script: no OneCLI service, Postgres, shell-rc edits or
# credentials. The gateway service is still needed when an agent session starts.
install_gateway_adapter() {
  local skill=".claude/skills/add-onecli" line from to dep
  [ -f "$SRC/$skill/SKILL.md" ] || { echo "no add-onecli skill in the checkout" >&2; exit 1; }
  while read -r from _ to; do
    mkdir -p "$SRC/$(dirname "$to")"
    cp "$SRC/$skill/$from" "$SRC/$to"
  done < <(awk '/^```nc:copy/{f=1;next} /^```/{f=0} f && /->/' "$SRC/$skill/SKILL.md")
  grep -q "import './onecli.js';" "$SRC/src/gateway-providers/installed.ts" \
    || printf "import './onecli.js';\n" >> "$SRC/src/gateway-providers/installed.ts"
  dep="$(awk '/^```nc:dep manager:pnpm/{f=1;next} /^```/{f=0} f && NF' "$SRC/$skill/SKILL.md" | head -1)"
  [ -n "$dep" ] || { echo "no pinned gateway SDK in the skill" >&2; exit 1; }
  ( cd "$SRC" && pnpm add "$dep" )
}

(
  cd "$SRC"
  pnpm install --frozen-lockfile
)
install_gateway_adapter
# The adapter above replaces onecli.ts: apply the channel's gateway hook again,
# then (optionally) the engines overlay, before building.
"$here/install.sh" "$SRC" >/dev/null
if [ "$ENGINES" = 1 ]; then "$here/engines/install-engines.sh" "$SRC"; fi
(
  cd "$SRC"
  pnpm rebuild better-sqlite3   # pnpm skips native builds by default
  pnpm run build
  # tsc does not emit imported JS assets; restore them even if build cleaned dist.
  "$here/install.sh" "$SRC" >/dev/null
  pnpm exec vitest run src/channels/argus.test.ts src/channels/argus-chat.test.ts src/channels/argus-accounts.test.ts src/channels/argus-continuation.test.ts src/channels/argus-continuation-observe.test.ts src/channels/argus-reply-fence.test.ts src/channels/argus-stall-causes.test.ts src/channels/argus-stderr-tail.test.ts src/channels/argus-claim-isolation.test.ts src/channels/argus-gitea-lease.test.ts src/channels/argus-workspace.test.ts src/channels/argus-checkpoint.test.ts src/channels/argus-inputs.test.ts src/channels/argus-changes-relay.test.ts src/gateway-providers/argus-cursor-stub.test.ts src/gateway-providers/argus-copilot-stub.test.ts src/gateway-providers/argus-local-harness.test.ts src/gateway-providers/argus-onecli-git-ca.test.ts src/gateway-providers/onecli.test.ts src/gateway-providers/onecli-files.test.ts src/gateway-providers/gateway-provider-registry.test.ts src/drivers/argus-codex-sandbox.test.ts src/argus-codex-sandbox-composition.test.ts src/drivers/docker-driver.test.ts src/session-mount-stubs.test.ts
)

if [ "$CHECK_ONLY" = 1 ]; then
  echo "Checked: $SRC installs, builds and passes the Argus vitest suite (no image built)."
  exit 0
fi

# Agent image: NanoClaw's own build script, then a stable tag and a `docker save`
# archive (the sandboxed host's private daemon starts without images).
(cd "$SRC" && ./container/build.sh)
built="$(cd "$SRC" && PROJECT_ROOT="$SRC" bash -c 'source setup/lib/install-slug.sh && container_image_base'):latest"
# ssh and rsync are not in NanoClaw's image: the Argus tools layer goes on top of
# the fresh build, so AGENT_TAG (what the fleet runs and what is saved) has them.
"$here/engines/apply-tools-layer.sh" "$built" "$AGENT_TAG"
docker save -o "$ARCHIVE" "$AGENT_TAG"

docker build -t "$HOST_IMAGE" "$repo/deploy/nanoclaw-host"

echo
echo "Prepared. Environment for the Argus process (no secrets):"
print_env
echo "Both templates are listed by Argus; each is ready only when its own prerequisites verify."
echo "Hosts boot and register with Argus now. Still required to run tasks: the OneCLI gateway service and credential (NanoClaw --step gateway, runbook), and for nanoclaw-sandbox the sysbox-runc runtime."
