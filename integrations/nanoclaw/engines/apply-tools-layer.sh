#!/usr/bin/env bash
# Put the tools layer (Dockerfile.tools-layer) on an agent image:
#   apply-tools-layer.sh BASE TAG     BASE: image ref or id; TAG: the result
# TAG may equal BASE's tag: the layer is added on top of BASE as it is now, so
# running it twice stacks a harmless no-op layer. provision.sh calls it after
# tagging the fresh build, so a rebuild from scratch reproduces the result.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
base="${1:?usage: apply-tools-layer.sh BASE TAG}"
tag="${2:?usage: apply-tools-layer.sh BASE TAG}"
docker build --build-arg "BASE=$base" -f "$here/Dockerfile.tools-layer" -t "$tag" "$here"
