#!/usr/bin/env bash
# Runs INSIDE the disposable probe container (see RUNBOOK.md). Prints PASS/FAIL per check.
# Expectation column: "allow" = must succeed, "deny" = must fail. Exit 1 if any check misbehaves.
set -uo pipefail
CODEX=${CODEX:-/pnpm/codex}
bad=0
check() { # name expect(allow|deny) cmd...
  local name=$1 expect=$2; shift 2
  local out rc; out=$("$@" 2>&1); rc=$?
  if { [ "$expect" = allow ] && [ $rc -eq 0 ]; } || { [ "$expect" = deny ] && [ $rc -ne 0 ]; }; then
    echo "PASS $name (rc=$rc)"
  else echo "FAIL $name (rc=$rc, wanted $expect)"; bad=1; fi
  printf '     | %s\n' "$(printf '%s' "$out" | head -3 | tr '\n' ' ' | cut -c1-200)"
}
sb() { "$CODEX" sandbox --permission-profile :workspace -- "$@"; }

echo "== confinement of the probe itself"
cat /proc/self/attr/current; grep -E '^(Seccomp|NoNewPrivs|CapEff|CapBnd):' /proc/self/status
"$CODEX" --version; "$CODEX" sandbox --help 2>&1 | head -12

echo "== controls OUTSIDE the codex sandbox (writes must work, else the negatives prove nothing)"
check control-write-other   allow sh -c 'echo c > /workspace/other/control'
check control-write-home    allow sh -c 'echo c > /home/node/control'

echo "== positive: codex sandbox workspace-write"
check sandbox-runs          allow sb /bin/true
check write-workspace       allow sb sh -c 'echo ok > /workspace/group/inside.txt'
check readback-workspace    allow grep -qx ok /workspace/group/inside.txt

echo "== negative: writes outside the workspace"
check write-other-denied    deny  sb sh -c 'echo x > /workspace/other/escape'
check write-home-denied     deny  sb sh -c 'echo x > /home/node/escape'
check write-etc-denied      deny  sb sh -c 'echo x > /etc/argus-escape'
for f in /workspace/other/escape /home/node/escape /etc/argus-escape; do
  check "absent:$f" deny test -e "$f"; done

echo "== negative: network (container is --network none; the error code tells who blocked)"
check net-denied deny sb node -e 'const s=require("net").connect(53,"1.1.1.1");s.on("error",e=>{console.log(e.code);process.exit(1)});s.on("connect",()=>process.exit(0));setTimeout(()=>process.exit(1),3000)'

echo "== negative: sensitive paths (host root is NOT mounted into the probe)"
check no-host-root          deny  test -e /host
check kcore-denied          deny  sb head -c 16 /proc/kcore
check sysrq-denied          deny  sb sh -c 'echo b > /proc/sysrq-trigger'
check lsm-sysfs-denied      deny  sb cat /sys/kernel/security/apparmor/profiles
check shadow-denied         deny  sb cat /etc/shadow

echo "== blast radius: what the profile lets ANY worker process do outside bwrap"
check userns-tmpfs-mount    allow unshare -Urm sh -c 'mount -t tmpfs t /mnt'      # accepted by the candidate
check userns-proc-mount     deny  unshare -Urmpf sh -c 'mount -t proc p /proc'    # not allowlisted
check userns-sysfs-mount    deny  unshare -Urm sh -c 'mount -t sysfs s /sys'
check userns-cgroup2-mount  deny  unshare -Urm sh -c 'mount -t cgroup2 c /sys/fs/cgroup'
check userns-overlay-mount  deny  unshare -Urm sh -c 'mkdir -p /tmp/o/{l,u,w,m}; mount -t overlay o -o lowerdir=/tmp/o/l,upperdir=/tmp/o/u,workdir=/tmp/o/w /tmp/o/m'
check mask-bypass-umount    deny  unshare -Urm sh -c 'umount /proc/kcore && head -c 16 /proc/kcore'

echo "== summary: $([ $bad -eq 0 ] && echo ALL-AS-EXPECTED || echo MISMATCHES)"
exit $bad
