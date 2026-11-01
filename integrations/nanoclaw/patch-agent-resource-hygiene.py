#!/usr/bin/env python3
"""Wire the agent resource-hygiene shims into session containers (install.sh).

usage: patch-agent-resource-hygiene.py /path/to/nanoclaw

docs/specs/agent-resource-hygiene.spec.md 3.3. Sets BASH_ENV and CLAUDE_ENV_FILE on
the agent container to argus-oom-priority.sh in the tools layer
(/usr/local/lib/argus). That shim raises oom_score_adj for the harness's command
shells and sources argus-cpu-count.sh, which adds the Node preload (NODE_OPTIONS)
and the sitecustomize directory (PYTHONPATH) only when the container has a CPU
quota. A value already in the agent environment wins.

Idempotent ("ensure this exact text"). Runs after patch-codex-sandbox.py, whose
`agent.securityProfile` hook it anchors on. Fails closed (non-zero exit, nothing
written) when that anchor is missing or ambiguous.
"""

import sys
from pathlib import Path

SHIM = "/usr/local/lib/argus/argus-oom-priority.sh"
MARKER = "// argus: agent resource hygiene"
ANCHOR = "  if (securityProfile) agent.securityProfile = securityProfile;\n"
HOOK = f"""
  {MARKER} (agent-resource-hygiene.spec.md): raise-only OOM priority for the
  // harness's command shells, and CPU-count shims when the container has a quota.
  agent.env = {{ BASH_ENV: '{SHIM}', CLAUDE_ENV_FILE: '{SHIM}', ...agent.env }};
"""

p = Path(sys.argv[1]) / "src" / "container-runner.ts"
if not p.is_file():
    sys.exit(f"{p}: missing; NanoClaw layout changed upstream")
text = p.read_text()
if MARKER not in text:
    assert text.count(ANCHOR) == 1, (
        "container-runner.ts: composeSessionSpec securityProfile hook: "
        "upstream shape changed"
    )
    p.write_text(text.replace(ANCHOR, ANCHOR + HOOK, 1))
    print(f"patched {p} (agent resource hygiene)")
