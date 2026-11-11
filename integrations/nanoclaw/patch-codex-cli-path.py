#!/usr/bin/env python3
"""Make the Codex app-server child resolve the image-installed codex CLI.

Runs from install-engines.sh, after the upstream Codex payload copy: the patched
file only exists once the payload is in place.

usage: patch-codex-cli-path.py <runner-root>

docs/specs/worker-lifecycle.spec.md 3.8. buildCodexProcessEnv forwards PATH unchanged, so
a `codex` installed under PNPM_HOME (/pnpm) is not found. After the allowlist loop, PATH
is set through overlays/codex-cli-path.ts (install.sh copies it). Idempotent.
"""

import sys
from pathlib import Path

IMPORT = "import { codexCliPath } from './codex-cli-path.js';\n"
IMPORT_ANCHOR = "import type { McpServerConfig } from './types.js';\n"
CALL = """  const cliPath = codexCliPath(next.PATH, env.PNPM_HOME);
  if (cliPath !== undefined) next.PATH = cliPath;
"""
FUNC = (
    "export function buildCodexProcessEnv(env: NodeJS.ProcessEnv): NodeJS.ProcessEnv {"
)
RETURN = "  return next;\n}"

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
if "codex-cli-path.js" in text:
    sys.exit(0)
if text.count(IMPORT_ANCHOR) != 1:
    sys.exit(f"{p}: import anchor not found; upstream shape changed")
start = text.find(FUNC)
if start < 0:
    sys.exit(f"{p}: buildCodexProcessEnv not found; upstream shape changed")
end = text.find(RETURN, start)
if end < 0:
    sys.exit(
        f"{p}: buildCodexProcessEnv `return next;` not found; upstream shape changed"
    )
text = text[:end] + CALL + text[end:]
text = text.replace(IMPORT_ANCHOR, IMPORT_ANCHOR + IMPORT, 1)
p.write_text(text)
print(f"patched {p} (Codex CLI PATH)")
