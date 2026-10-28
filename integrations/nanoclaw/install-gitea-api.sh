#!/usr/bin/env bash
# Place argus-gitea-api and argus-github-api from integrations/nanoclaw/{gitea,github}-api.mjs.
# github-api.mjs imports ./gitea-api.mjs, so both modules always land side by side.
# Shared by install.sh, engines/install-engines.sh, apply-tools-layer.sh, and offline tests.
#   install-gitea-api.sh copy-nanoclaw TARGET        → TARGET/container + TARGET/src/channels
#   install-gitea-api.sh patch-nanoclaw-dockerfile T → patch T/container/Dockerfile (install -m)
#   install-gitea-api.sh stage-engines TARGET        → TARGET/container/{gitea,github}-api.mjs only
#   install-gitea-api.sh stage-tools-ctx CTX         → CTX/{gitea,github}-api.mjs (tools-layer context)
#   install-gitea-api.sh install-bin PREFIX          → PREFIX/usr/local/bin/{argus-gitea-api,argus-github-api,gitea-api.mjs,github-api.mjs}
# Idempotent. Does not need Docker, network, or secrets.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/gitea-api.mjs"
types="$here/gitea-api.d.mts"
gh_src="$here/github-api.mjs"
gh_types="$here/github-api.d.mts"
for f in "$src" "$gh_src"; do [[ -f "$f" ]] || { echo "missing $f" >&2; exit 2; }; done

cmd="${1:?usage: install-gitea-api.sh copy-nanoclaw|patch-nanoclaw-dockerfile|stage-engines|stage-tools-ctx|install-bin DEST}"
dest="${2:?usage: install-gitea-api.sh $cmd DEST}"

case "$cmd" in
  copy-nanoclaw)
    mkdir -p "$dest/container" "$dest/src/channels"
    cp -p "$src" "$dest/container/gitea-api.mjs"
    cp -p "$gh_src" "$dest/container/github-api.mjs"
    cp -p "$src" "$types" "$gh_src" "$gh_types" "$dest/src/channels/"
    ;;
  patch-nanoclaw-dockerfile)
    # Same block install.sh used to inline; keeps Dockerfile install -m in one place.
    dockerfile="$dest/container/Dockerfile"
    [[ -f "$dockerfile" ]] || { echo "missing $dockerfile" >&2; exit 2; }
    python3 - "$dockerfile" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
github = '''# ---- Argus GitHub API helper ----------------------------------------------
# Same model as argus-gitea-api; github-api.mjs imports gitea-api.mjs beside it.
COPY gitea-api.mjs github-api.mjs /tmp/
RUN install -m 0644 /tmp/gitea-api.mjs /tmp/github-api.mjs /usr/local/bin/ \\
 && install -m 0755 /tmp/github-api.mjs /usr/local/bin/argus-github-api \\
 && rm /tmp/gitea-api.mjs /tmp/github-api.mjs

'''
marker = "# ---- ncl CLI wrapper"
if "Argus Gitea API helper" not in s:
    block = '''# ---- Argus Gitea API helper -----------------------------------------------
# The token stays in OneCLI; this client sends HTTPS through the injected proxy.
COPY gitea-api.mjs /tmp/argus-gitea-api.mjs
RUN npm install --prefix /usr/local/lib/argus-gitea --omit=dev --no-audit --no-fund undici@8.12.2 \\
 && install -m 0755 /tmp/argus-gitea-api.mjs /usr/local/bin/argus-gitea-api \\
 && rm /tmp/argus-gitea-api.mjs

'''
    assert marker in s, "NanoClaw container Dockerfile has no ncl CLI insertion marker"
    s = s.replace(marker, block + marker, 1)
if "Argus GitHub API helper" not in s:
    assert marker in s, "NanoClaw container Dockerfile has no ncl CLI insertion marker"
    s = s.replace(marker, github + marker, 1)
open(p, "w").write(s)
PY
    ;;
  stage-engines)
    mkdir -p "$dest/container"
    cp -p "$src" "$dest/container/gitea-api.mjs"
    cp -p "$gh_src" "$dest/container/github-api.mjs"
    ;;
  stage-tools-ctx)
    mkdir -p "$dest"
    cp -p "$src" "$dest/gitea-api.mjs"
    cp -p "$gh_src" "$dest/github-api.mjs"
    ;;
  install-bin)
    # Same semantics as Dockerfile / install -m / COPY --chmod for the helper binary.
    bindir="$dest/usr/local/bin"
    mkdir -p "$bindir"
    install -m 0644 "$src" "$bindir/gitea-api.mjs"
    install -m 0755 "$src" "$bindir/argus-gitea-api"
    install -m 0644 "$gh_src" "$bindir/github-api.mjs"
    install -m 0755 "$gh_src" "$bindir/argus-github-api"
    ;;
  *)
    echo "unknown command: $cmd" >&2
    exit 2
    ;;
esac
