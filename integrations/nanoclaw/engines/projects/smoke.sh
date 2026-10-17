#!/usr/bin/env bash
# Smoke of a project image: the toolchain a worker needs to run a project's gates.
#   smoke.sh IMAGE        (offline: --network none; the interpreter must be pre-installed)
# One line per item; exit 1 if any is missing.
set -uo pipefail
image="${1:?usage: smoke.sh IMAGE}"
fail=0
check() { # label command...
  local label="$1" out
  shift
  if out="$(docker run --rm --network none --entrypoint "$1" "$image" "${@:2}" 2>&1 </dev/null | head -1)" && [[ -n "$out" ]]; then
    printf '%-14s ok    %s\n' "$label" "$out"
  else
    printf '%-14s FAIL  %s\n' "$label" "${out:-not found}"
    fail=1
  fi
}
check uv uv --version
check python3 python3 --version
check project-python uv python find 3.13
check git git --version
check ssh ssh -V
check make make --version
exit "$fail"
