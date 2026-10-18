#!/usr/bin/env bash
# Build a project layer (projects/<project>.Dockerfile) on top of an agent image:
#   build-project-image.sh PROJECT BASE_TAG
# BASE_TAG: the image to extend, e.g. argus-nanoclaw-agent:<hash>; it must already
# carry the tools layer (apply-tools-layer.sh). The result is tagged
# argus-nanoclaw-agent:<project>-<suffix of BASE_TAG> and that tag is the last line
# printed, so `tag="$(build-project-image.sh radar argus-nanoclaw-agent:abc | tail -1)"` works.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project="${1:?usage: build-project-image.sh PROJECT BASE_TAG}"
base="${2:?usage: build-project-image.sh PROJECT BASE_TAG}"
dockerfile="$here/projects/$project.Dockerfile"
[[ "$project" =~ ^[a-z0-9][a-z0-9_.-]*$ ]] || { echo "invalid project name: $project" >&2; exit 2; }
[[ -f "$dockerfile" ]] || { echo "no Dockerfile for project '$project': $dockerfile not found" >&2; exit 2; }
suffix="${base##*:}"   # no ':' -> the whole ref
suffix="${suffix//[^A-Za-z0-9_.-]/-}"
tag="argus-nanoclaw-agent:$project-$suffix"
gitea_src="$here/../gitea-api.mjs"
[[ -f "$gitea_src" ]] || { echo "missing Gitea helper: $gitea_src" >&2; exit 2; }
cp "$gitea_src" "$here/gitea-api.mjs"
cleanup() { rm -f "$here/gitea-api.mjs"; rm -rf "$here/.graft-stage"; }
trap cleanup EXIT
if [ "$project" = radar ]; then
  graft_src="${GRAFT_COPY_FROM:-$HOME/.hermes/node/lib/node_modules/@nanonets/graft}"
  [[ -f "$graft_src/dist/cli.js" ]] || {
    echo "prebuilt graft 0.21.1 not found at $graft_src (set GRAFT_COPY_FROM)" >&2
    exit 2
  }
  graft_ver="$(node "$graft_src/dist/cli.js" --version 2>/dev/null || true)"
  [[ "$graft_ver" = 0.21.1 ]] || {
    echo "graft at $graft_src is $graft_ver, expected 0.21.1" >&2
    exit 2
  }
  rm -rf "$here/.graft-stage"
  mkdir -p "$here/.graft-stage"
  cp -a "$graft_src" "$here/.graft-stage/graft"
fi
docker build --build-arg "BASE=$base" -f "$dockerfile" -t "$tag" "$here" >&2
echo "$tag"
