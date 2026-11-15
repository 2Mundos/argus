#!/usr/bin/env python3
"""Forward the per-attempt merge authorization into session env (install.sh).

usage: patch-merge-auth-env.py /path/to/nanoclaw

argus-merge-auth.ts mints ARGUS_MERGE_AUTH_KEY/TASK/ATTEMPT and ARGUS_MERGE_AUTHORIZED in
the host process env. A session is a separate container whose env is built from the
`contributedEnv` allowlist in container-runner.ts (the loop install.sh extends with the git
identity, command guard and forge review), so the four names must be on that list or
`argus-github-api merge` never sees them. A name unset on the host is skipped by the loop,
so an unauthorized attempt carries none. Extends a runner patched before these names
existed; idempotent; fails closed when the list is missing.
"""

import re
import sys
from pathlib import Path

NAMES = (
    "ARGUS_MERGE_AUTH_KEY",
    "ARGUS_MERGE_AUTH_TASK",
    "ARGUS_MERGE_AUTH_ATTEMPT",
    "ARGUS_MERGE_AUTHORIZED",
)

p = Path(sys.argv[1]) / "src" / "container-runner.ts"
if not p.is_file():
    sys.exit(f"{p}: missing; NanoClaw layout changed upstream")
s = p.read_text()
if "ARGUS_MERGE_AUTHORIZED" in s:
    sys.exit(0)
pattern = re.compile(r"(for \(const key of \[[^\]]*'ARGUS_COMMAND_GUARD'[^\]]*?)(,?\s*\]\))")
if len(pattern.findall(s)) != 1:
    sys.exit("container-runner.ts: forwarded-env list changed upstream")
extra = ", ".join(f"'{n}'" for n in NAMES)
p.write_text(pattern.sub(lambda m: f"{m.group(1)}, {extra}{m.group(2)}", s, count=1))
print(f"patched {p} (host merge authorization → agent contributedEnv)")
