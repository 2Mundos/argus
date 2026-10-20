#!/usr/bin/env bash
# Derive argus-codex-worker.seccomp.json from the upstream Docker default profile.
# usage: derive-seccomp.sh <upstream default.json> | biome format --stdin-file-path=argus-codex-worker.seccomp.json > argus-codex-worker.seccomp.json
# (raw jq output is semantically equal; Biome formatting is what the repo lint requires)
# Upstream sha256 expected: 6416b47770785a41ac59073cdc77d9fe98517df2799dc83ef207e622de3053f6
set -euo pipefail
# 0x7e020000 = CLONE_NEWNS|NEWCGROUP|NEWUTS|NEWIPC|NEWUSER|NEWPID|NEWNET (namespace flags)
# 0x81fdffff = ~0x7e020000 (every non-namespace bit); 0x10000000 = CLONE_NEWUSER
jq '.syscalls += [
  {"names":["unshare"],"action":"SCMP_ACT_ALLOW",
   "args":[{"index":0,"value":2180907007,"valueTwo":0,"op":"SCMP_CMP_MASKED_EQ"}],
   "comment":"argus: unshare only with namespace flags (bwrap: NEWUSER, NEWPID); no-op for CAP_SYS_ADMIN holders (already allowed upstream)",
   "excludes":{"caps":["CAP_SYS_ADMIN"]}},
  {"names":["clone"],"action":"SCMP_ACT_ALLOW",
   "args":[{"index":0,"value":268435456,"valueTwo":268435456,"op":"SCMP_CMP_MASKED_EQ"}],
   "comment":"argus: clone with namespace flags only when CLONE_NEWUSER is set in the same call (bwrap raw_clone); other ns flags then belong to the new userns",
   "excludes":{"caps":["CAP_SYS_ADMIN"],"arches":["s390","s390x"]}},
  {"names":["clone"],"action":"SCMP_ACT_ALLOW",
   "args":[{"index":1,"value":268435456,"valueTwo":268435456,"op":"SCMP_CMP_MASKED_EQ"}],
   "comment":"argus: s390 argument order variant of the clone rule above",
   "includes":{"arches":["s390","s390x"]},"excludes":{"caps":["CAP_SYS_ADMIN"]}},
  {"names":["mount","umount2","pivot_root"],"action":"SCMP_ACT_ALLOW",
   "comment":"argus: needed by bwrap inside its own userns/mountns; unprivileged in the container userns (no CAP_SYS_ADMIN) and further gated by the AppArmor mount allow-list",
   "excludes":{"caps":["CAP_SYS_ADMIN"]}}
]' "$1"
