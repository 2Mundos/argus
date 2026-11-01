#!/usr/bin/env bash
# Put the tools layer (Dockerfile.tools-layer) on an agent image:
#   apply-tools-layer.sh BASE TAG     BASE: image ref or id; TAG: the result
# TAG may equal BASE's tag: the layer is added on top of BASE as it is now, so
# running it twice stacks a harmless no-op layer. provision.sh calls it after
# tagging the fresh build, so a rebuild from scratch reproduces the result.
# The build context is a staging copy of this directory plus ../gitea-api.mjs and
# ../github-api.mjs, which the layer installs but which live one level up (shared
# with install.sh).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
base="${1:?usage: apply-tools-layer.sh BASE TAG}"
tag="${2:?usage: apply-tools-layer.sh BASE TAG}"
gitea_src="$here/../gitea-api.mjs"
[[ -f "$gitea_src" ]] || { echo "missing Gitea helper: $gitea_src" >&2; exit 2; }
[[ -f "$here/../github-api.mjs" ]] || { echo "missing GitHub helper: $here/../github-api.mjs" >&2; exit 2; }
ctx="$(mktemp -d)"
trap 'rm -rf "$ctx"' EXIT
shopt -s nullglob
cp -p "$here"/*.mjs "$here"/*.json "$here/command_guard.py" "$ctx/"
# Shared helper (also exercised by offline installer tests).
bash "$here/../install-gitea-api.sh" stage-tools-ctx "$ctx"
docker build --build-arg "BASE=$base" -f "$here/Dockerfile.tools-layer" -t "$tag" "$ctx"
