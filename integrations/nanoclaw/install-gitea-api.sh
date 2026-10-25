#!/usr/bin/env bash
# Place argus-gitea-api from integrations/nanoclaw/gitea-api.mjs.
# Shared by install.sh, engines/install-engines.sh, apply-tools-layer.sh, and offline tests.
#   install-gitea-api.sh copy-nanoclaw TARGET        → TARGET/container + TARGET/src/channels
#   install-gitea-api.sh patch-nanoclaw-dockerfile T → patch T/container/Dockerfile (install -m)
#   install-gitea-api.sh stage-engines TARGET        → TARGET/container/gitea-api.mjs only
#   install-gitea-api.sh stage-tools-ctx CTX         → CTX/gitea-api.mjs (tools-layer context)
#   install-gitea-api.sh install-bin PREFIX          → PREFIX/usr/local/bin/{argus-gitea-api,gitea-api.mjs}
# Idempotent. Does not need Docker, network, or secrets.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/gitea-api.mjs"
types="$here/gitea-api.d.mts"
[[ -f "$src" ]] || { echo "missing $src" >&2; exit 2; }

cmd="${1:?usage: install-gitea-api.sh copy-nanoclaw|patch-nanoclaw-dockerfile|stage-engines|stage-tools-ctx|install-bin DEST}"
dest="${2:?usage: install-gitea-api.sh $cmd DEST}"

case "$cmd" in
  copy-nanoclaw)
    mkdir -p "$dest/container" "$dest/src/channels"
    cp -p "$src" "$dest/container/gitea-api.mjs"
    cp -p "$src" "$types" "$dest/src/channels/"
    ;;
  patch-nanoclaw-dockerfile)
    # Same block install.sh used to inline; keeps Dockerfile install -m in one place.
    dockerfile="$dest/container/Dockerfile"
    [[ -f "$dockerfile" ]] || { echo "missing $dockerfile" >&2; exit 2; }
    python3 - "$dockerfile" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
if "Argus Gitea API helper" not in s:
    marker = "# ---- ncl CLI wrapper"
    block = '''# ---- Argus Gitea API helper -----------------------------------------------
# The token stays in OneCLI; this client sends HTTPS through the injected proxy.
COPY gitea-api.mjs /tmp/argus-gitea-api.mjs
RUN npm install --prefix /usr/local/lib/argus-gitea --omit=dev --no-audit --no-fund undici@8.12.2 \\
 && install -m 0755 /tmp/argus-gitea-api.mjs /usr/local/bin/argus-gitea-api \\
 && rm /tmp/argus-gitea-api.mjs

'''
    assert marker in s, "NanoClaw container Dockerfile has no ncl CLI insertion marker"
    s = s.replace(marker, block + marker, 1)
open(p, "w").write(s)
PY
    ;;
  stage-engines)
    mkdir -p "$dest/container"
    cp -p "$src" "$dest/container/gitea-api.mjs"
    ;;
  stage-tools-ctx)
    mkdir -p "$dest"
    cp -p "$src" "$dest/gitea-api.mjs"
    ;;
  install-bin)
    # Same semantics as Dockerfile / install -m / COPY --chmod for the helper binary.
    bindir="$dest/usr/local/bin"
    mkdir -p "$bindir"
    install -m 0644 "$src" "$bindir/gitea-api.mjs"
    install -m 0755 "$src" "$bindir/argus-gitea-api"
    ;;
  *)
    echo "unknown command: $cmd" >&2
    exit 2
    ;;
esac
