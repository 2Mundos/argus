#!/bin/sh
# Image build step: DeepSeek Harness (dsh) at a pinned npm version, with its own Node
# runtime (docs/specs/engines.spec.md). dsh needs Node >= 24.9.0 and the agent image runs
# Node 22, so Node 24 is unpacked into a dedicated prefix and /usr/local/bin/dsh is a wrapper
# that runs dsh on it; the image's node and npm are never touched. Every download is checked
# against a pin stored here (sha256 for Node, the registry's sha512 integrity for dsh) before
# it is extracted or installed. Bump the versions and hashes together, from
# https://nodejs.org/dist/vX.Y.Z/SHASUMS256.txt and `npm view @deepseek-ai/dsh dist`.
# ARGUS_DSH_PREFIX and ARGUS_DSH_BIN_DIR move the install roots (a sandbox without root);
# they do not touch the checks.
set -eu

DSH_VERSION="${DSH_VERSION:-0.2.2-alpha.3}"
DSH_INTEGRITY="${DSH_INTEGRITY:-sha512-ffV06fOgiOeIJOmA3EgPj+U6azJrsLACiscLj9t+AfM7p9p14bMzErWRhgeC8nizBTyQ2ckxkPQtj4Pqon8mGw==}"
DSH_TARBALL_URL="${DSH_TARBALL_URL:-https://registry.npmjs.org/@deepseek-ai/dsh/-/dsh-${DSH_VERSION}.tgz}"
NODE_VERSION="v24.22.2"
NODE_SHA256_X64="${DSH_NODE_SHA256_X64:-48cc0e21e048790e54c0cf5749c2725005313d652651c40b7a21feb9697aaf75}"
NODE_SHA256_ARM64="${DSH_NODE_SHA256_ARM64:-eb187837a2230d4047161ac0be008584e16b46f726e9c9490ee35dc6b17d9ba8}"

root="${ARGUS_DSH_PREFIX:-/opt}"
bin_dir="${ARGUS_DSH_BIN_DIR:-/usr/local/bin}"
node_dir="$root/argus-dsh-node"
dsh_dir="$root/argus-dsh"

fail() { echo "install-dsh: $*" >&2; exit 1; }
pinned() { # name value pattern
  printf '%s' "$2" | grep -Eq "$3" || fail "$1 is not a pinned value; set it before building here"
}
pinned DSH_VERSION "$DSH_VERSION" '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$'
pinned DSH_INTEGRITY "$DSH_INTEGRITY" '^sha512-[A-Za-z0-9+/]{86}==$'

case "$(uname -m)" in
  x86_64|amd64) arch=x64; node_sha="$NODE_SHA256_X64" ;;
  aarch64|arm64) arch=arm64; node_sha="$NODE_SHA256_ARM64" ;;
  *) fail "no pinned Node runtime for $(uname -m); add one before building here" ;;
esac
pinned "the Node sha256 for $arch" "$node_sha" '^[0-9a-f]{64}$'

command -v xz >/dev/null || fail "xz is required to unpack the Node runtime (apt install xz-utils)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fSL --retry 3 -o "$tmp/dsh.tgz" "$DSH_TARBALL_URL"
sum="sha512-$(openssl dgst -sha512 -binary "$tmp/dsh.tgz" | openssl base64 -A)"
[ "$sum" = "$DSH_INTEGRITY" ] || fail "dsh $DSH_VERSION integrity mismatch (got $sum)"
curl -fSL --retry 3 -o "$tmp/node.tar.xz" "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-${arch}.tar.xz"
echo "${node_sha}  $tmp/node.tar.xz" | sha256sum -c - >/dev/null || fail "Node $NODE_VERSION checksum mismatch"

rm -rf "$node_dir" "$dsh_dir"
mkdir -p "$node_dir" "$dsh_dir" "$bin_dir"
tar --strip-components=1 -xJf "$tmp/node.tar.xz" -C "$node_dir"
PATH="$node_dir/bin:$PATH" "$node_dir/bin/npm" install --prefix "$dsh_dir" --omit=dev --no-audit --no-fund "$tmp/dsh.tgz"

entry="$dsh_dir/node_modules/@deepseek-ai/dsh/lib/bin.js"
[ -f "$entry" ] || fail "expected $entry after install"
cat > "$bin_dir/dsh" <<WRAP
#!/bin/sh
exec "$node_dir/bin/node" "$entry" "\$@"
WRAP
chmod 0755 "$bin_dir/dsh"

out="$("$bin_dir/dsh" --version 2>&1 | head -1)"
[ "$out" = "$DSH_VERSION" ] || fail "expected version $DSH_VERSION, got: ${out:-nothing}"
echo "dsh $out"
