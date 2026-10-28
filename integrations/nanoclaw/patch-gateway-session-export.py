#!/usr/bin/env python3
"""Export NanoClaw's ensureGatewaySession (install.sh).

usage: patch-gateway-session-export.py /path/to/nanoclaw

The repository preflight session (argus-repo-preflight.ts) must take its gateway
lease through the same admission gate as a worker session (it refuses while the
gateway is unavailable and closes with the admission generation). That function is
module-private upstream; this only adds `export`. Idempotent; fails closed (non-zero)
when the anchor is missing and the export is not already there.
"""

import sys
from pathlib import Path

p = Path(sys.argv[1]) / "src" / "container-runner.ts"
if not p.is_file():
    sys.exit(f"{p}: missing; NanoClaw layout changed upstream")
s = p.read_text()
OLD = "async function ensureGatewaySession(input: GatewaySessionInput)"
NEW = "export " + OLD
if NEW in s:
    sys.exit(0)
if s.count(OLD) != 1:
    sys.exit("container-runner.ts: ensureGatewaySession: upstream shape changed")
p.write_text(s.replace(OLD, NEW, 1))
print(f"patched {p} (export ensureGatewaySession)")
