#!/bin/sh
# Install the argus launcher (docs/specs/launcher.spec.md).
#   curl -fsSL https://github.com/2Mundos/argus/releases/latest/download/install.sh | sh
# Set ARGUS_VERSION=vX.Y.Z (for sh, after the pipe) to pin a release.
# It fetches the launcher of the newest (or the pinned) release; it installs nothing else.
# No Docker needed: remote-only use (`argus tray --url https://host`) never touches images.
set -eu

REPO=${ARGUS_REPO:-2Mundos/argus}
IMAGE=${ARGUS_IMAGE:-ghcr.io/2mundos/argus}
BIN_DIR=${ARGUS_BIN_DIR:-$HOME/.local/bin}

die() { printf 'install: %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null || die "curl is required"
# docker is only the fallback source for the launcher; container commands need it later.

version=${ARGUS_VERSION:-$(curl -fsSI "https://github.com/$REPO/releases/latest" |
	tr -d '\r' | sed -n 's|^[Ll]ocation: .*/tag/\(v[0-9.]*\)$|\1|p')}
[ -n "$version" ] || die "could not find the newest release of $REPO"

mkdir -p "$BIN_DIR"
tmp=$BIN_DIR/argus.new.$$
if curl -fsSL "https://raw.githubusercontent.com/$REPO/$version/deploy/argus" >"$tmp" 2>/dev/null; then
	:
elif command -v docker >/dev/null 2>&1; then
	if ! docker pull "$IMAGE:$version" >/dev/null 2>&1; then
		rm -f "$tmp"
		die "could not pull $IMAGE:$version; check the network and retry"
	fi
	docker run --rm --entrypoint cat "$IMAGE:$version" /app/bin/argus >"$tmp" 2>/dev/null || {
		rm -f "$tmp"
		die "$IMAGE:$version predates the launcher; use the docker run in the README until the next release"
	}
else
	rm -f "$tmp"
	die "could not fetch the launcher for $version; check the network and retry"
fi
sh -n "$tmp" || {
	rm -f "$tmp"
	die "the launcher in $IMAGE:$version does not parse"
}
chmod 755 "$tmp"
mv "$tmp" "$BIN_DIR/argus"

printf 'Installed argus (%s) to %s/argus\n' "$version" "$BIN_DIR"
case :$PATH: in
*:"$BIN_DIR":*) ;;
*) printf 'Add %s to your PATH, then run: argus up\n' "$BIN_DIR" && exit 0 ;;
esac
printf 'Next: argus up    (add --docker to run workers on this machine)\n'
printf 'Or watch a remote Argus with no local install: argus tray --url https://host\n'
