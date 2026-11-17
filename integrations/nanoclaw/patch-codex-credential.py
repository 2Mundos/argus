#!/usr/bin/env python3
"""Refuse to start the Codex app-server child without the gateway's credential.

Runs from install-engines.sh, after patch-codex-cli-path.py: the patched file only
exists once the upstream Codex payload is in place.

usage: patch-codex-credential.py <runner-root>

docs/specs/codex-accounts.spec.md. The CLI authenticates from CODEX_HOME/auth.json, the
sentinel OneCLI serves for an `openai` lease; without it the CLI exits 0 right after
start. spawnCodexAppServer now builds the child env once, makes sure CODEX_HOME exists
and throws `codex_credential_missing` (overlays/codex-credential.ts) when the sentinel is
absent. Idempotent.
"""

import sys
from pathlib import Path

IMPORT = "import { requireCodexCredential } from './codex-credential.js';\n"
IMPORT_ANCHOR = "import type { McpServerConfig } from './types.js';\n"
OLD = """  const proc = spawn('codex', args, {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: buildCodexProcessEnv(process.env),
  });
"""
NEW = """  const childEnv = buildCodexProcessEnv(process.env);
  requireCodexCredential(childEnv);
  const proc = spawn('codex', args, {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: childEnv,
  });
"""

p = (
    Path(sys.argv[1])
    / "container"
    / "agent-runner"
    / "src"
    / "providers"
    / "codex-app-server.ts"
)
if not p.is_file():
    sys.exit(f"{p}: missing; NanoClaw layout changed upstream")
text = p.read_text()
if "codex-credential.js" in text:
    sys.exit(0)
if text.count(IMPORT_ANCHOR) != 1:
    sys.exit(f"{p}: import anchor not found; upstream shape changed")
if text.count(OLD) != 1:
    sys.exit(f"{p}: spawn call not found; upstream shape changed")
text = text.replace(OLD, NEW, 1).replace(IMPORT_ANCHOR, IMPORT_ANCHOR + IMPORT, 1)
p.write_text(text)
print(f"patched {p} (Codex credential refusal)")
