#!/usr/bin/env python3
"""Pre-create session mount stubs before docker run (install.sh).

usage: patch-session-mount-stubs.py /path/to/nanoclaw

Hooks ensureSessionMountStubs into buildMounts so agent/ and group/ exist as the
host uid before the OCI runtime would create them as 0:0 (workspace-cleanup
spec 3.3). Idempotent; fails closed (non-zero) when an anchor is missing or
ambiguous.
"""

import sys
from pathlib import Path

p = Path(sys.argv[1]) / "src" / "container-runner.ts"
if not p.is_file():
    sys.exit(f"{p}: missing; NanoClaw layout changed upstream")
s = p.read_text()

IMPORT_ANCHOR = "} from './session-manager.js';\n"
IMPORT_LINE = "import { ensureSessionMountStubs } from './session-mount-stubs.js';\n"
IMPORT_FULL = IMPORT_ANCHOR + IMPORT_LINE

CALL_ANCHOR = "  const sessDir = sessionDir(agentGroup.id, session.id);\n"
CALL_BLOCK = (
    CALL_ANCHOR
    + "  // Pre-create the nested bind-mount points and the image WORKDIR as the host\n"
    + "  // user: the OCI runtime runs as root and would otherwise create them 0:0\n"
    + "  // inside the session directory, where the host user can never remove them\n"
    + "  // and workspace cleanup blocks with permission_denied (spec 3.3).\n"
    + "  ensureSessionMountStubs(sessDir);\n"
)

changed = False

if IMPORT_LINE not in s:
    if s.count(IMPORT_ANCHOR) != 1:
        sys.exit("container-runner.ts: session-manager import: upstream shape changed")
    s = s.replace(IMPORT_ANCHOR, IMPORT_FULL, 1)
    changed = True

if "ensureSessionMountStubs(sessDir);" not in s:
    if s.count(CALL_ANCHOR) != 1:
        sys.exit("container-runner.ts: sessDir: upstream shape changed")
    s = s.replace(CALL_ANCHOR, CALL_BLOCK, 1)
    changed = True

if changed:
    p.write_text(s)
    print(f"patched {p} (ensureSessionMountStubs)")
