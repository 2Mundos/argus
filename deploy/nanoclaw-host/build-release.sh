#!/usr/bin/env bash
# Build the published worker images (docs/specs/local-workers.spec.md 3.1):
#   deploy/nanoclaw-host/build-release.sh HOST_REF [AGENT_REF]
# HOST_REF: the NanoClaw host image with the built checkout at /opt/nanoclaw.
# AGENT_REF: the agent image, built from that same checkout the way provision.sh
# builds it (NanoClaw's container/build.sh, then the Argus tools layer). Without
# AGENT_REF only the host image is built (what quality CI checks on a PR).
# IMAGE_LABELS (optional, "key=value" per line) are added to both images, e.g. the
# OCI source label that links a GHCR package to this repository.
set -euo pipefail

host_ref="${1:?usage: build-release.sh HOST_REF [AGENT_REF]}"
agent_ref="${2:-}"
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
commit="$(sed -n 's/^NANOCLAW_COMMIT="${NANOCLAW_COMMIT:-\([0-9a-f]\{40\}\)}"$/\1/p' \
  "$repo/integrations/nanoclaw/provision.sh")"
[ -n "$commit" ] || { echo "no pinned NANOCLAW_COMMIT in provision.sh" >&2; exit 1; }

base="argus-nanoclaw-host:release-base-$$"
work="$(mktemp -d)"
cleanup() {
  docker rmi -f "$base" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

docker build -t "$base" "$repo/deploy/nanoclaw-host"
docker build -f "$repo/deploy/nanoclaw-host/Dockerfile.release" \
  --build-arg HOST_IMAGE="$base" --build-arg NANOCLAW_COMMIT="$commit" \
  -t "$host_ref" "$repo"
docker run --rm --entrypoint sh "$host_ref" -c \
  'test -f /opt/nanoclaw/dist/index.js && test -f /opt/nanoclaw/dist/channels/argus.js'

[ -n "$agent_ref" ] || exit 0

cid="$(docker create "$host_ref")"
docker cp "$cid:/opt/nanoclaw" "$work/src"
docker rm "$cid" >/dev/null
(cd "$work/src" && ./container/build.sh build)
built="$(cd "$work/src" && PROJECT_ROOT="$work/src" bash -c \
  'source setup/lib/install-slug.sh && container_image_base'):latest"
"$repo/integrations/nanoclaw/engines/apply-tools-layer.sh" "$built" "$agent_ref"
docker rmi "$built" >/dev/null
docker image inspect -f '{{.Size}}' "$agent_ref" >/dev/null

# Labels go on as one metadata-only layer, so both images carry the same ones.
label() {
  [ -n "${IMAGE_LABELS:-}" ] || return 0
  { echo "FROM $1"; printf '%s\n' "$IMAGE_LABELS" | sed '/^[[:space:]]*$/d; s/^[[:space:]]*/LABEL /'; } |
    docker build -q -t "$1" - >/dev/null
}
label "$host_ref"
label "$agent_ref"
echo "built $host_ref and $agent_ref (NanoClaw $commit)"
