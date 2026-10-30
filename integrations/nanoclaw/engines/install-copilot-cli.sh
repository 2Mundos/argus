#!/bin/sh
# Image build step: GitHub Copilot CLI at a pinned npm version (copilot accounts spec).
# The package ships a platform-specific native binary under @github/copilot-linux-*.
set -eu

VERSION="${COPILOT_CLI_VERSION:-1.0.105}"
prefix=/usr/local/lib/argus-copilot

case "$(uname -m)" in
  x86_64|amd64) pkg="@github/copilot-linux-x64" ;;
  *) echo "copilot: no pinned arch package for $(uname -m); add one before building here" >&2; exit 1 ;;
esac

npm install --prefix "$prefix" --omit=dev --no-audit --no-fund "@github/copilot@${VERSION}"
bin="$prefix/node_modules/$pkg/copilot"
[ -x "$bin" ] || { echo "copilot: expected binary at $bin" >&2; exit 1; }
ln -sf "$bin" /usr/local/bin/copilot
out="$(copilot --version 2>&1 | head -1)"
case "$out" in
  *"$VERSION"*) ;;
  *) echo "copilot: expected version $VERSION, got: ${out:-nothing}" >&2; exit 1 ;;
esac
