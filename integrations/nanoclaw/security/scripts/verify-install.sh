#!/usr/bin/env bash
# Prove the per-worker opt-in survives provisioning, against a DISPOSABLE checkout.
#   scripts/verify-install.sh /path/to/nanoclaw-git-checkout [fixture-dir]
#   UPGRADE_FROM=<dir>      start from a copy of an already-installed checkout (an older install) instead of
#                           a pristine archive, to prove the patches upgrade in place
#   CONVERGE_WITH=<dir>     require the result to be byte-identical to another fixture (pristine vs upgraded)
# The checkout is only read (`git archive` of the pinned commit; its node_modules is
# symlinked, untracked, into the fixture). Runs install.sh twice and requires the
# second run to change nothing, then tsc --noEmit and the real driver/composition
# tests. No network, no Docker, no root. Nothing is installed into the checkout.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
nc="$(cd "$here/../.." && pwd)"
src="${1:?usage: verify-install.sh /path/to/nanoclaw-git-checkout [fixture-dir]}"
commit="$(sed -n 's/^NANOCLAW_COMMIT="${NANOCLAW_COMMIT:-\([0-9a-f]*\)}"$/\1/p' "$nc/provision.sh")"
[ -n "$commit" ] || { echo "FAIL: pinned NANOCLAW_COMMIT not found in provision.sh" >&2; exit 1; }
[ -d "$src/node_modules" ] || { echo "FAIL: $src has no node_modules to link" >&2; exit 1; }
fx="${2:-$(mktemp -d "${TMPDIR:-$HOME/.hermes/cache/scratch}/argus-nc-fixture.XXXXXX")}"
mkdir -p "$fx"
[ -z "$(ls -A "$fx")" ] || { echo "FAIL: fixture dir $fx is not empty" >&2; exit 1; }

if [ -n "${UPGRADE_FROM:-}" ]; then
  cp -a "$UPGRADE_FROM/." "$fx/"
  echo "fixture: $fx (upgrade of $UPGRADE_FROM)"
else
  git -C "$src" archive "$commit" | tar -x -C "$fx"
  ln -s "$src/node_modules" "$fx/node_modules"
  echo "fixture: $fx (pristine $commit)"
fi

# The gateway adapter provision.sh applies from the checkout's own skill (nc:copy and
# the barrel import only; the pnpm dependency comes from the linked node_modules).
skill=".claude/skills/add-onecli"
[ -n "${UPGRADE_FROM:-}" ] || while read -r from _ to; do
  mkdir -p "$fx/$(dirname "$to")"
  cp "$fx/$skill/$from" "$fx/$to"
done < <(awk '/^```nc:copy/{f=1;next} /^```/{f=0} f && /->/' "$fx/$skill/SKILL.md")
grep -q "import './onecli.js';" "$fx/src/gateway-providers/installed.ts" \
  || printf "import './onecli.js';\n" >> "$fx/src/gateway-providers/installed.ts"

tree_hash() { (cd "$fx" && find . -path ./node_modules -prune -o -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum); }

"$nc/install.sh" "$fx" >/dev/null
first="$(tree_hash)"
"$nc/install.sh" "$fx" >/dev/null
second="$(tree_hash)"
[ "$first" = "$second" ] || { echo "FAIL: second install.sh changed the checkout" >&2; exit 1; }
echo "install.sh x2: second run changed nothing"
for needle in securityProfileForProvider assertDriverSupportsSecurityProfiles; do
  [ "$(grep -c "$needle" "$fx/src/container-runner.ts")" = 2 ] || { echo "FAIL: container-runner.ts $needle not applied exactly once" >&2; exit 1; }
done
[ "$(grep -c "securityProfile?: 'codex-bwrap'" "$fx/src/drivers/types.ts")" = 1 ] || { echo "FAIL: types.ts field" >&2; exit 1; }
[ "$(grep -c "securityProfiles" "$fx/src/drivers/types.ts")" -ge 2 ] || { echo "FAIL: types.ts capability" >&2; exit 1; }
! grep -q "assertDaemonSupportsProfile" "$fx/src/drivers/docker-driver.ts" || { echo "FAIL: stale v1 hook left in docker-driver.ts" >&2; exit 1; }
[ "$(grep -c "argus-codex-sandbox" "$fx/src/drivers/docker-driver.ts")" = 1 ] || { echo "FAIL: docker-driver.ts patch not applied exactly once" >&2; exit 1; }
cmp "$nc/security/argus-codex-worker.seccomp.json" "$fx/security/argus-codex-worker.seccomp.json"

if [ -n "${CONVERGE_WITH:-}" ]; then
  other="$(cd "$CONVERGE_WITH" && find . -path ./node_modules -prune -o -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum)"
  [ "$second" = "$other" ] || { echo "FAIL: result differs from $CONVERGE_WITH" >&2; exit 1; }
  echo "converges with $CONVERGE_WITH (byte-identical tree)"
fi

cd "$fx"
node_modules/.bin/tsc --noEmit
echo "tsc --noEmit: clean"
node_modules/.bin/vitest run \
  src/drivers/argus-codex-sandbox.test.ts src/argus-codex-sandbox-composition.test.ts \
  src/drivers/docker-driver.test.ts src/drivers/conformance.test.ts src/drivers/driver-selection.test.ts \
  src/container-runner.test.ts src/channels/argus.test.ts src/channels/argus-accounts.test.ts \
  src/channels/argus-gitea-lease.test.ts src/gateway-providers/argus-cursor-stub.test.ts \
  src/gateway-providers/argus-onecli-git-ca.test.ts src/gateway-providers/onecli.test.ts \
  src/gateway-providers/argus-local-harness.test.ts
echo "fixture kept at $fx"
