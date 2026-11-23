#!/bin/sh
# Image build step: Muse CLI (`muse`) at a pinned version, SHA-256 verified
# (muse accounts spec). Uses the exact artifact URLs and SHA-256 digests from
# Meta's public release manifest for x86_64 and aarch64 Linux; fails closed
# before installing on any checksum mismatch or unsupported architecture.
set -eu

VERSION="${MUSE_VERSION:-1.4.25-R9361.1}"
SHA256_X64="${MUSE_SHA256_X64:-c1b728fb9732503c5e1869e3ad2f2c9bdb5b2638fb4cf398d6e2c46aa6d8f94a}"
SHA256_ARM64="${MUSE_SHA256_ARM64:-8fe089115ef137879fca46986d55edf18d5d3ab9cfdb6441c67e9d1807b4108a}"
BASE_URL="${MUSE_BASE_URL:-https://lookaside.facebook.com/lookaside/muse/download}"

case "$(uname -m)" in
  x86_64|amd64) file="muse-x86-linux"; sha="$SHA256_X64" ;;
  aarch64|arm64) file="muse-aarch64-linux"; sha="$SHA256_ARM64" ;;
  *) echo "muse: no pinned checksum for $(uname -m); add one before building here" >&2; exit 1 ;;
esac

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fSL --retry 3 -o "$tmp/muse" "${BASE_URL}/?channel=muse&version=${VERSION}&file=${file}"
echo "${sha}  $tmp/muse" | sha256sum -c -
chmod 0755 "$tmp/muse"
mv "$tmp/muse" /usr/local/bin/muse
out="$(muse --version 2>&1 | head -1)"
case "$out" in
  *"$VERSION"*) ;;
  *) echo "muse: expected version $VERSION, got: ${out:-nothing}" >&2; exit 1 ;;
esac
