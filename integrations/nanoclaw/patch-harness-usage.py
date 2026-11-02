#!/usr/bin/env python3
"""Hook the harness's token usage and live log into a NanoClaw agent runner (install.sh).

usage: patch-harness-usage.py /path/to/nanoclaw

claude.ts and codex.ts hand each finished turn to overlays/harness-usage.ts (copied next to
them first), which appends a usage record for the NanoClaw host to post (account usage spec
3.9). Claude's SDK message loop also calls recordHarnessLog so the host can post the
harness stream as task-log source `worker` (task-live-log spec 3.8). Idempotent: every
hook is "ensure this exact text"; an anchor that is missing or ambiguous stops the install
(the upstream shape changed).
"""

import sys
from pathlib import Path

PROVIDERS = Path("container/agent-runner/src/providers")


def edit(root: Path, name: str, hooks: list[tuple[str, str, str]]) -> None:
    p = root / PROVIDERS / name
    if not p.is_file():
        return
    s = p.read_text()
    original = s
    for what, anchor, new in hooks:
        if what == "import" and "from './harness-usage.js'" in s:
            continue  # present (possibly the pre-log form); "import upgrade" rewrites it
        if new in s:
            continue
        if what == "import upgrade" and anchor not in s:
            continue  # never had the usage-only import
        if s.count(anchor) != 1:
            raise SystemExit(f"{p}: {what}: upstream shape changed")
        s = s.replace(anchor, new, 1)
    if s != original:
        p.write_text(s)
        print(f"patched {p}")


REGISTER = "import { registerProvider } from './provider-registry.js';\n"
CLAUDE_IMPORT_OLD = "import { createClaudeUsageTracker, newQueryId, recordHarnessUsage } from './harness-usage.js';\n"
CLAUDE_IMPORT = (
    "import { createClaudeUsageTracker, newQueryId, recordHarnessLog, "
    "recordHarnessUsage } from './harness-usage.js';\n"
)
# Before `lastStreamActivityAt`: patch-claude-rate-limit.py anchors on the lines that follow it.
CLAUDE_LOOP = "      let messageCount = 0;\n"
CLAUDE_STATE = (
    "      // Argus: this query's token usage, for the NanoClaw host (harness-usage.ts).\n"
    "      const usageTracker = createClaudeUsageTracker();\n"
    "      const usageQuery = newQueryId();\n"
    "      let usageSeq = 0;\n"
)
# The rate-limit patch owns `for await (const message of sdkResult)`; hook the body.
CLAUDE_FOR = "      for await (const message of sdkResult) {\n"
CLAUDE_LOG = (
    "      for await (const message of sdkResult) {\n"
    "        // Argus: harness live log for the NanoClaw host (task-live-log spec 3.8).\n"
    "        recordHarnessLog(message);\n"
)
CLAUDE_RESULT = (
    "          const resultAsError = hint ? `${candidate}\\n${hint}` : '';\n"
)
CLAUDE_RECORD = "          recordHarnessUsage('claude', usageTracker.observe(message), usageQuery, ++usageSeq);\n"

CODEX_IMPORT = "import { createCodexUsageTracker, newQueryId, recordHarnessUsage } from './harness-usage.js';\n"
CODEX_FN = "async function* runOneTurn(\n"
CODEX_STATE = (
    "// Argus: token usage per thread and per process, for the NanoClaw host (harness-usage.ts).\n"
    "const codexUsage = new Map<string, ReturnType<typeof createCodexUsageTracker>>();\n"
    "const codexUsageQuery = newQueryId();\n"
    "let codexUsageSeq = 0;\n\n"
)
CODEX_COMPLETED = "      case 'turn/completed': {\n"
CODEX_UPDATED = (
    "      case 'thread/tokenUsage/updated': {\n"
    "        const tracker = codexUsage.get(threadId) ?? createCodexUsageTracker();\n"
    "        codexUsage.set(threadId, tracker);\n"
    "        tracker.update(params.tokenUsage);\n"
    "        break;\n"
    "      }\n"
)
CODEX_RECORD = (
    "        recordHarnessUsage(\n"
    "          'codex',\n"
    "          (codexUsage.get(threadId) ?? createCodexUsageTracker()).complete(model ?? null),\n"
    "          codexUsageQuery,\n"
    "          ++codexUsageSeq,\n"
    "        );\n"
)


def main(root: Path) -> None:
    edit(
        root,
        "claude.ts",
        [
            ("import", REGISTER, REGISTER + CLAUDE_IMPORT),
            # Upgrade a checkout that already had the usage-only import.
            ("import upgrade", CLAUDE_IMPORT_OLD, CLAUDE_IMPORT),
            ("query state", CLAUDE_LOOP, CLAUDE_LOOP + CLAUDE_STATE),
            ("live log", CLAUDE_FOR, CLAUDE_LOG),
            ("result usage", CLAUDE_RESULT, CLAUDE_RESULT + CLAUDE_RECORD),
        ],
    )
    edit(
        root,
        "codex.ts",
        [
            ("import", REGISTER, REGISTER + CODEX_IMPORT),
            ("usage state", CODEX_FN, CODEX_STATE + CODEX_FN),
            (
                "usage update",
                CODEX_COMPLETED,
                CODEX_UPDATED + CODEX_COMPLETED + CODEX_RECORD,
            ),
        ],
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
