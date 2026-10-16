#!/bin/sh
# Image build step: the Cursor agent CLI at a pinned version, checksum verified.
# The vendor's installer (https://cursor.com/install) only downloads a versioned
# tarball from downloads.cursor.com and symlinks it; this does the same with a
# pinned SHA-256 instead of trusting whatever the URL serves tomorrow. The hash was
# computed on first download (the vendor publishes none): bump version and hash
# together, after a fresh download and a look at the vendor installer.
set -eu

VERSION="${CURSOR_AGENT_VERSION:-2026.10.16-e3cb207}"
SHA256_X64="${CURSOR_AGENT_SHA256_X64:-3aa3f48cd7a3909a98c9b3f2a4085f0c021088bf826690b22c61f424c843e552}"

case "$(uname -m)" in
  x86_64|amd64) arch=x64; sha="$SHA256_X64" ;;
  *) echo "cursor agent: no pinned checksum for $(uname -m); add one before building here" >&2; exit 1 ;;
esac

dest=/opt/cursor-agent
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fSL --retry 3 -o "$tmp/agent.tgz" "https://downloads.cursor.com/lab/${VERSION}/linux/${arch}/agent-cli-package.tar.gz"
echo "${sha}  $tmp/agent.tgz" | sha256sum -c -
mkdir -p "$dest"
tar --strip-components=1 -xzf "$tmp/agent.tgz" -C "$dest"
ln -sf "$dest/cursor-agent" /usr/local/bin/agent
ln -sf "$dest/cursor-agent" /usr/local/bin/cursor-agent
agent --version
