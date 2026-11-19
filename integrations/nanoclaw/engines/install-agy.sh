#!/bin/sh
# Image build step: Google Antigravity CLI (`agy`) at a pinned version, SHA-512 verified
# (google accounts spec). Uses the exact tarball URLs and SHA-512 digests published by the
# official release manifests for linux_amd64 and linux_arm64; fails closed before extraction
# on any checksum mismatch or unsupported architecture.
set -eu

VERSION="${AGY_VERSION:-1.5.12}"
BUILD="${AGY_BUILD:-1.5.12-5281398513336320}"
SHA512_X64="${AGY_SHA512_X64:-26de25fccb3fbf0b093e090ce14a14ac1df1ec3e90ef4ea6106887d3a81b0cc9edb0dadea7b9101d07f9606eba159062a6fb7b78e91b73f7520684a24db73428}"
SHA512_ARM64="${AGY_SHA512_ARM64:-1030d89ce9e5cb39509e272cb89b71fde0e0d5d6527b7392525d5ef2683da54fb49230449107486d91c02ded39a678faaa649b2041304a6cf82a3a332d5819d9}"
BASE_URL="${AGY_BASE_URL:-https://storage.googleapis.com/antigravity-public/antigravity-cli}"

case "$(uname -m)" in
  x86_64|amd64) subdir="linux-x64"; tarball="cli_linux_x64.tar.gz"; sha="$SHA512_X64" ;;
  aarch64|arm64) subdir="linux-arm"; tarball="cli_linux_arm64.tar.gz"; sha="$SHA512_ARM64" ;;
  *) echo "agy: no pinned checksum for $(uname -m); add one before building here" >&2; exit 1 ;;
esac

dest=/opt/antigravity-cli
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fSL --retry 3 -o "$tmp/agy.tar.gz" "${BASE_URL}/${BUILD}/${subdir}/${tarball}"
echo "${sha}  $tmp/agy.tar.gz" | sha512sum -c -
mkdir -p "$dest"
tar -xzf "$tmp/agy.tar.gz" -C "$dest" antigravity
chmod 0755 "$dest/antigravity"
ln -sf "$dest/antigravity" /usr/local/bin/agy
out="$(agy --version 2>&1 | head -1)"
case "$out" in
  *"$VERSION"*) ;;
  *) echo "agy: expected version $VERSION, got: ${out:-nothing}" >&2; exit 1 ;;
esac
