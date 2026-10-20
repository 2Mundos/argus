#!/usr/bin/env bash
# Runs INSIDE the disposable probe container (see RUNBOOK.md). Prints PASS/FAIL/SKIP per check.
# "allow" = must succeed, "deny" = must fail AFTER the command really started (it must print the
# marker first), so a sandbox that never started cannot make a negative pass.
# Exit: 0 all as expected; 1 a check misbehaved; 2 precondition missing; 3 the Codex sandbox did not
# bootstrap (every sandbox-dependent check is NOT_EXECUTED and nothing is proven).
set -uo pipefail
CODEX=${CODEX:-/pnpm/codex}
MARK=ARGUS-STARTED
bad=0
skipped=0
net_proof=unproven
[ -x "$CODEX" ] || { echo "FAIL codex binary not executable: $CODEX (set CODEX=)"; exit 2; }
check() { # name expect(allow|deny) cmd...   deny requires the marker in the output
  local name=$1 expect=$2; shift 2
  local out rc; out=$("$@" 2>&1); rc=$?
  if [ "$expect" = allow ] && [ $rc -eq 0 ]; then echo "PASS $name (rc=$rc)"
  elif [ "$expect" = deny ] && [ $rc -ne 0 ] && printf '%s' "$out" | grep -q "$MARK"; then echo "PASS $name (rc=$rc)"
  elif [ "$expect" = deny ] && [ $rc -ne 0 ]; then echo "FAIL $name (rc=$rc, failed BEFORE the command started: not a denial)"; bad=1
  else echo "FAIL $name (rc=$rc, wanted $expect)"; bad=1; fi
  printf '     | %s\n' "$(printf '%s' "$out" | head -3 | tr '\n' ' ' | cut -c1-200)"
}
skip() { echo "NOT_EXECUTED $1 ($2)"; skipped=$((skipped + 1)); }
sb() { "$CODEX" sandbox --permission-profile :workspace -- "$@"; }
sbm() { sb sh -c "echo $MARK; $*"; }                 # inside the codex sandbox, marker first
um() { unshare -Urm --propagation unchanged sh -c "echo $MARK; $*"; }  # own userns+mountns, no implicit mount of /

echo "== confinement of the probe itself"
cat /proc/self/attr/current; grep -E '^(Seccomp|NoNewPrivs|CapEff|CapBnd):' /proc/self/status
"$CODEX" --version; "$CODEX" sandbox --help 2>&1 | head -12

echo "== controls OUTSIDE the codex sandbox (writes must work, else the negatives prove nothing)"
check control-write-other   allow sh -c 'echo c > /workspace/other/control'
check control-write-home    allow sh -c 'echo c > /home/node/control'

echo "== bootstrap: the codex sandbox must start and write the workspace, or nothing below is evidence"
check sandbox-runs          allow sb /bin/true
check write-workspace       allow sb sh -c 'echo ok > /workspace/group/inside.txt'
check readback-workspace    allow grep -qx ok /workspace/group/inside.txt
if [ "$bad" -ne 0 ]; then
  for n in write-other-denied write-home-denied write-etc-denied net kcore-denied sysrq-denied lsm-sysfs-denied shadow-denied; do
    skip "$n" "codex sandbox did not bootstrap"; done
  for n in userns-tmpfs-mount userns-plain-tmpfs userns-proc-mount userns-sysfs-mount userns-cgroup2-mount userns-overlay-mount mask-bypass-umount; do
    skip "$n" "codex sandbox did not bootstrap"; done
  echo "== summary: BOOTSTRAP-FAILED (nothing proven; $skipped checks NOT_EXECUTED; kernel: journalctl -k | grep 'apparmor=\"DENIED\"')"
  exit 3
fi

echo "== negative: writes outside the workspace"
check write-other-denied    deny  sbm 'echo x > /workspace/other/escape'
check write-home-denied     deny  sbm 'echo x > /home/node/escape'
check write-etc-denied      deny  sbm 'echo x > /etc/argus-escape'
for f in /workspace/other/escape /home/node/escape /etc/argus-escape; do
  check "absent:$f" deny sh -c "echo $MARK; test -e $f"; done

echo "== network: the container is --network none, so a refusal alone proves nothing about the Codex sandbox"
# EPERM/EACCES come from a policy inside the sandbox; ENETUNREACH & co. are the empty Docker network and are
# only reported, never counted as a PASS.
net_code=$(sb node -e 'const s=require("net").connect(53,"1.1.1.1");s.on("error",e=>{console.log(e.code);process.exit(1)});s.on("connect",()=>{console.log("CONNECTED");process.exit(0)});setTimeout(()=>{console.log("TIMEOUT");process.exit(1)},3000)' 2>&1 | tail -1)
case "$net_code" in
  EPERM|EACCES) echo "PASS net-blocked-by-sandbox ($net_code)"; net_proof=sandbox ;;
  CONNECTED)    echo "FAIL net-connected (--network none was bypassed)"; bad=1 ;;
  ENETUNREACH|EHOSTUNREACH|ENETDOWN|EAI_AGAIN|ENOTFOUND) echo "INFO net-blocked-only-by-docker-network-none ($net_code): says nothing about the Codex sandbox" ;;
  *)            echo "FAIL net-unclassified (${net_code:-no output})"; bad=1 ;;
esac

echo "== negative: sensitive paths (host root is NOT mounted into the probe)"
check no-host-root          deny  sh -c "echo $MARK; test -e /host"
check kcore-denied          deny  sbm 'head -c 16 /proc/kcore'
check sysrq-denied          deny  sbm 'echo b > /proc/sysrq-trigger'
check lsm-sysfs-denied      deny  sbm 'cat /sys/kernel/security/apparmor/profiles'
check shadow-denied         deny  sbm 'cat /etc/shadow'

echo "== blast radius: what the profile lets ANY worker process do outside bwrap"
# unshare itself must work first (no implicit mount of /), else a later "denial" would be unshare failing.
if unshare -Urm --propagation unchanged true 2>/dev/null; then
  check userns-tmpfs-mount    allow um 'mount -t tmpfs -o nosuid,nodev t /mnt'       # accepted by the candidate
  check userns-plain-tmpfs    deny  um 'mount -t tmpfs t /mnt'                       # other flags: not allowlisted
  check userns-proc-mount     deny  um 'mount -t proc p /proc'
  check userns-sysfs-mount    deny  um 'mount -t sysfs s /sys'
  check userns-cgroup2-mount  deny  um 'mount -t cgroup2 c /sys/fs/cgroup'
  check userns-overlay-mount  deny  um 'mkdir -p /tmp/o/l /tmp/o/u /tmp/o/w /tmp/o/m; mount -t overlay o -o lowerdir=/tmp/o/l,upperdir=/tmp/o/u,workdir=/tmp/o/w /tmp/o/m'
  check mask-bypass-umount    deny  um 'umount /proc/kcore && head -c 16 /proc/kcore'
else
  for n in userns-tmpfs-mount userns-plain-tmpfs userns-proc-mount userns-sysfs-mount userns-cgroup2-mount userns-overlay-mount mask-bypass-umount; do
    skip "$n" "unshare -Urm --propagation unchanged does not start"; done
  bad=1
fi

if [ "$bad" -ne 0 ]; then echo "== summary: MISMATCHES ($skipped NOT_EXECUTED; network blocked by the Codex sandbox: $net_proof)"; exit 1; fi
echo "== summary: ALL-AS-EXPECTED (network blocked by the Codex sandbox: $net_proof)"
