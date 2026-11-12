#!/bin/sh
# Image build step: kubectl at a pinned version, checksum verified
# (docs/specs/integrations.spec.md 3.6). The hashes are the ones dl.k8s.io
# publishes beside each binary (`kubectl.sha256`); bump version and hashes
# together. The binary is only a client: it reads the attempt's KUBECONFIG, and a
# task without a Kubernetes integration has none.
set -eu

VERSION="${KUBECTL_VERSION:-v1.37.2}"
SHA256_AMD64="${KUBECTL_SHA256_AMD64:-c04487523e7aa7d1493f7808174c899db93d7a9cf57531420cd76b9bd415f8f4}"
SHA256_ARM64="${KUBECTL_SHA256_ARM64:-fcc16f63186659d9722014b7e02d5fdd2d4e669cb8c0ab66365d7d3b8ba03509}"
DEST="${KUBECTL_DEST:-/usr/local/bin/kubectl}"

case "$(uname -m)" in
  x86_64|amd64) arch=amd64; sha="$SHA256_AMD64" ;;
  aarch64|arm64) arch=arm64; sha="$SHA256_ARM64" ;;
  *) echo "kubectl: no pinned checksum for $(uname -m); add one before building here" >&2; exit 1 ;;
esac

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fSL --retry 3 -o "$tmp/kubectl" "https://dl.k8s.io/release/${VERSION}/bin/linux/${arch}/kubectl"
echo "${sha}  $tmp/kubectl" | sha256sum -c -
install -m 0755 "$tmp/kubectl" "$DEST"
"$DEST" version --client
