#!/usr/bin/env python3
"""Prepend the worker git wrapper and engine dir to session PATH (install.sh).

docs/specs/worker-lifecycle.spec.md 3.8. Merges PATH into the existing
agent-resource-hygiene `agent.env` block (does not splice into the hygiene
comment). PATH is BIN, then PNPM_HOME (fallback /pnpm) unless the base PATH
already lists it, then the base. Idempotent; requires
patch-agent-resource-hygiene.py to have run first.
"""

import re
import sys
from pathlib import Path

MARKER = "// argus: git worker attribution"
SHIM = "/usr/local/lib/argus/argus-oom-priority.sh"
BIN = "/usr/local/lib/argus/bin"
HYGIENE_ENV = (
    f"  agent.env = {{ BASH_ENV: '{SHIM}', CLAUDE_ENV_FILE: '{SHIM}', ...agent.env }};"
)
EXPANDED = f"""  {MARKER} (worker-lifecycle 3.8): prepend worker git wrapper and engine dir on PATH.
  agent.env = {{
    ...agent.env,
    PATH: '{BIN}:' + ((e, b) => (b.split(':').includes(e) ? b : e + ':' + b))(agent.env.PNPM_HOME ?? process.env.PNPM_HOME ?? '/pnpm', agent.env.PATH ?? process.env.PATH ?? '/usr/local/bin:/usr/bin:/bin'),
    BASH_ENV: '{SHIM}',
    CLAUDE_ENV_FILE: '{SHIM}',
  }};"""
_ATTRIBUTION_BLOCK = re.compile(
    rf"  {re.escape(MARKER)}[^\n]*\n  agent\.env = \{{.*?\}};",
    re.DOTALL,
)

p = Path(sys.argv[1]) / "src" / "container-runner.ts"
if not p.is_file():
    sys.exit(f"{p}: missing; NanoClaw layout changed upstream")
text = p.read_text()
if MARKER in text:
    normalized = _ATTRIBUTION_BLOCK.sub(EXPANDED, text, count=1)
    if normalized != text:
        p.write_text(normalized)
        print(f"patched {p} (git worker attribution PATH)")
    sys.exit(0)
if HYGIENE_ENV not in text:
    sys.exit(
        f"{p}: missing hygiene agent.env block; run patch-agent-resource-hygiene.py first"
    )
if text.count(HYGIENE_ENV) != 1:
    sys.exit(f"{p}: ambiguous hygiene agent.env block")
p.write_text(text.replace(HYGIENE_ENV, EXPANDED, 1))
print(f"patched {p} (git worker attribution PATH)")
