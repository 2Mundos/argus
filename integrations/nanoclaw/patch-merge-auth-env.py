#!/usr/bin/env python3
"""Forward per-attempt merge authorization and fix scope into session env (install.sh).

usage: patch-merge-auth-env.py /path/to/nanoclaw

argus-merge-auth.ts sets ARGUS_MERGE_AUTH_KEY/TASK/ATTEMPT, ARGUS_MERGE_AUTHORIZED,
ARGUS_MERGE_APPROVED_PR/SHA, and the reviewer fix scope ARGUS_FIX_PR/ARGUS_FIX_BASE_SHA
in the host process env. A session is a separate container whose env is built from the
`contributedEnv` allowlist in container-runner.ts (the loop install.sh extends with the git
identity, command guard and forge review), so these names must be on that list or
`argus-github-api merge` and `argus-task deliver` never see them. A name unset on the host
is skipped by the loop, so the script first deletes all of them from any prepopulated
contribution/gateway env: an unauthorized or non-fix attempt must not inherit earlier
values. Extends a runner patched before these names existed; idempotent; fails closed when
the list is missing.
"""

import re
import sys
from pathlib import Path

NAMES = (
    "ARGUS_MERGE_AUTH_KEY",
    "ARGUS_MERGE_AUTH_TASK",
    "ARGUS_MERGE_AUTH_ATTEMPT",
    "ARGUS_MERGE_AUTHORIZED",
    "ARGUS_MERGE_APPROVED_PR",
    "ARGUS_MERGE_APPROVED_SHA",
    "ARGUS_FIX_PR",
    "ARGUS_FIX_BASE_SHA",
)
DROP = "// argus: drop stale merge authorization"

p = Path(sys.argv[1]) / "src" / "container-runner.ts"
if not p.is_file():
    sys.exit(f"{p}: missing; NanoClaw layout changed upstream")
s = p.read_text()
drop_pattern = re.compile(
    rf"{re.escape(DROP)}\n"
    r"(?P<indent>[ \t]*)for \(const key of \[(?P<list>[^\]]*)\]\)"
    r" \{\n[ \t]*delete contributedEnv\[key\];\n[ \t]*\}\n"
)
drop_match = drop_pattern.search(s)
if drop_match:
    drop_names = drop_match.group("list")
    drop_missing = [n for n in NAMES if f"'{n}'" not in drop_names]
    if drop_missing:
        drop_names += "".join(f", '{n}'" for n in drop_missing)
        d_indent = drop_match.group("indent")
        drop_replacement = (
            f"{DROP}\n"
            f"{d_indent}for (const key of [{drop_names}]) {{\n"
            f"{d_indent}  delete contributedEnv[key];\n"
            f"{d_indent}}}\n"
        )
        s = s[: drop_match.start()] + drop_replacement + s[drop_match.end() :]
pattern = re.compile(
    r"(?P<indent>[ \t]*)for \(const key of \["
    r"(?P<list>[^\]]*'ARGUS_COMMAND_GUARD'[^\]]*?)(?P<tail>,?\s*\]\))"
)
matches = pattern.findall(s)
if len(matches) != 1:
    sys.exit("container-runner.ts: forwarded-env list changed upstream")
match = pattern.search(s)
indent = match.group("indent")
names = match.group("list")
missing = [n for n in NAMES if f"'{n}'" not in names]
if missing:
    names += "".join(f", '{n}'" for n in missing)
clear = ""
if DROP not in s:
    cleared = ", ".join(f"'{n}'" for n in NAMES)
    clear = (
        f"{indent}{DROP}\n"
        f"{indent}for (const key of [{cleared}]) {{\n"
        f"{indent}  delete contributedEnv[key];\n"
        f"{indent}}}\n"
    )
replacement = f"{clear}{indent}for (const key of [{names}{match.group('tail')}"
p.write_text(s[: match.start()] + replacement + s[match.end() :])
print(f"patched {p} (host merge authorization & fix scope → agent contributedEnv)")
