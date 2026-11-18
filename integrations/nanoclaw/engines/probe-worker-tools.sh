#!/usr/bin/env bash
# Offline probe of the everyday worker tools in an agent image, run as uid 1000
# with no network: presence of rg, jq, wget and patch, plus one representative
# operation each. Works on any tools-layer image, a project image derived from it
# included.
#   probe-worker-tools.sh IMAGE          (docker run --network none --user 1000:1000)
#   probe-worker-tools.sh --inside       (the checks, in the current container)
set -euo pipefail
inside='
set -eu
test "$(id -u)" = 1000
for b in rg jq wget patch; do command -v "$b" >/dev/null || { echo "missing $b"; exit 1; }; done
d=$(mktemp -d); cd "$d"
printf "alpha\nbeta\n" > a.txt
rg -q beta a.txt
test "$(printf "{\"k\":[1,2]}" | jq -c ".k|add")" = 3
cp a.txt b.txt; printf "gamma\n" >> b.txt
diff -u a.txt b.txt > c.diff || true
patch -s a.txt < c.diff
cmp a.txt b.txt
wget --version >/dev/null
if wget -q -T 2 -t 1 -O /dev/null http://127.0.0.1:9/ 2>/dev/null; then echo "wget unexpectedly succeeded"; exit 1; fi
echo "worker tools ok"
'
if [[ "${1:-}" == --inside ]]; then exec bash -c "$inside"; fi
image="${1:?usage: probe-worker-tools.sh IMAGE | --inside}"
exec docker run --rm --network none --user 1000:1000 --entrypoint bash "$image" -c "$inside"
