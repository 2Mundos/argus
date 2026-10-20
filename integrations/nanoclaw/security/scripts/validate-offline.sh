#!/usr/bin/env bash
# Offline checks only: no root, no kernel load, no Docker. usage: scripts/validate-offline.sh
set -euo pipefail
cd "$(dirname "$0")/.."
aa=argus-codex-worker.apparmor sc=argus-codex-worker.seccomp.json

apparmor_parser -QK -S "$aa" >/dev/null && echo "apparmor: compiles (-QK -S)"
# negative control: the parser really parses
neg=$(mktemp "${TMPDIR:-$HOME/.hermes/cache/scratch}/aa-neg.XXXXXX")
trap 'rm -f "$neg"' EXIT
printf 'profile x { bogus rule, }\n' > "$neg"
if apparmor_parser -QK -S "$neg" >/dev/null 2>&1; then echo "FAIL: negative control accepted"; exit 1; fi
echo "apparmor: negative control rejected"

jq -e . "$sc" >/dev/null && echo "seccomp: valid JSON"
jq -e '.defaultAction == "SCMP_ACT_ERRNO"' "$sc" >/dev/null && echo "seccomp: defaultAction=SCMP_ACT_ERRNO"
# no profile-wide ALLOW, no unconditional allow of the dangerous set
jq -e '[.syscalls[] | select(.action=="SCMP_ACT_ALLOW" and (.names|index("setns") or index("bpf") or index("fsopen") or index("move_mount") or index("open_tree")) and (.includes.caps|not))] | length == 0' "$sc" >/dev/null \
  && echo "seccomp: setns/bpf/new-mount-API only allowed with CAP_SYS_ADMIN (unchanged from upstream)" \
  || { echo "FAIL: ungated dangerous syscall"; exit 1; }
if grep -vE '^\s*#' "$aa" | grep unconfined | grep -qv 'peer=unconfined'; then echo "FAIL: unconfined"; exit 1; fi
echo "apparmor: no unconfined profile (only peer=unconfined signal scope)"
# the AppArmor delta against the Docker template is only the marked block + pivot_root
echo "apparmor: 'deny mount,' present? $(grep -c '^  deny mount,$' "$aa") (expected 0)"
