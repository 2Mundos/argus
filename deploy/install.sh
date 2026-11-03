#!/bin/sh
# Install the argus launcher (docs/specs/launcher.spec.md 3.1):
#   curl -fsSL https://raw.githubusercontent.com/2Mundos/argus/main/deploy/install.sh | sh
# It copies /app/bin/argus out of the newest release image; it installs nothing else.
set -eu

REPO=${ARGUS_REPO:-2Mundos/argus}
IMAGE=${ARGUS_IMAGE:-ghcr.io/2mundos/argus}
BIN_DIR=${ARGUS_BIN_DIR:-$HOME/.local/bin}

die() { printf 'install: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "docker is required (https://docs.docker.com/get-docker/)"
command -v curl >/dev/null || die "curl is required"
docker info >/dev/null 2>&1 || die "cannot reach the Docker daemon; start Docker (or add yourself to the docker group)"

version=${ARGUS_VERSION:-$(curl -fsSI "https://github.com/$REPO/releases/latest" |
	tr -d '\r' | sed -n 's|^[Ll]ocation: .*/tag/\(v[0-9.]*\)$|\1|p')}
[ -n "$version" ] || die "could not find the newest release of $REPO"

docker pull "$IMAGE:$version" >/dev/null
mkdir -p "$BIN_DIR"
tmp=$BIN_DIR/argus.new.$$
docker run --rm --entrypoint cat "$IMAGE:$version" /app/bin/argus >"$tmp" 2>/dev/null || {
	rm -f "$tmp"
	die "$IMAGE:$version predates the launcher; use the docker run in the README until the next release"
}
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
