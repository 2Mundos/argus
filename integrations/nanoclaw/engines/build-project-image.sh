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
docker build --build-arg "BASE=$base" -f "$dockerfile" -t "$tag" "$here/projects" >&2
echo "$tag"
