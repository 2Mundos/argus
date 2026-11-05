#!/usr/bin/env python3
"""Hook the harness's token usage and live log into a NanoClaw agent runner (install.sh).

usage: patch-harness-usage.py /path/to/nanoclaw

claude.ts and codex.ts hand each finished turn to overlays/harness-usage.ts (copied next to
them first), which appends a usage record for the NanoClaw host to post (account usage spec
3.9). Claude's SDK message loop also calls recordHarnessLog so the host can post the
harness stream as task-log source `worker` (task-live-log spec 3.8); codex.ts does the same for
the Codex app-server notifications `item/completed`, `error` and `turn/completed` through
recordHarnessCodexLog. Idempotent: every
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

CODEX_IMPORT_OLD = "import { createCodexUsageTracker, newQueryId, recordHarnessUsage } from './harness-usage.js';\n"
CODEX_IMPORT = (
    "import { createCodexUsageTracker, newQueryId, recordHarnessCodexLog, "
    "recordHarnessUsage } from './harness-usage.js';\n"
)
CODEX_FN = "async function* runOneTurn(\n"
CODEX_STATE = (
    "// Argus: token usage per thread and per process, for the NanoClaw host (harness-usage.ts).\n"
    "const codexUsage = new Map<string, ReturnType<typeof createCodexUsageTracker>>();\n"
    "const codexUsageQuery = newQueryId();\n"
    "let codexUsageSeq = 0;\n\n"
)
CODEX_COMPLETED = "      case 'turn/completed': {\n"
CODEX_UPDATED_HEAD = "      case 'thread/tokenUsage/updated': {\n"
CODEX_UPDATED = (
    CODEX_UPDATED_HEAD
    + "        const tracker = codexUsage.get(threadId) ?? createCodexUsageTracker();\n"
    "        codexUsage.set(threadId, tracker);\n"
    "        tracker.update(params.tokenUsage);\n"
    "        break;\n"
    "      }\n"
)


def codex_log(method: str) -> str:
    return f"        recordHarnessCodexLog('{method}', params);\n"


CODEX_LOG_TURN = codex_log("turn/completed")
CODEX_RECORD = (
    "        recordHarnessUsage(\n"
    "          'codex',\n"
    "          (codexUsage.get(threadId) ?? createCodexUsageTracker()).complete(model ?? null),\n"
    "          codexUsageQuery,\n"
    "          ++codexUsageSeq,\n"
    "        );\n"
)


def log_codex_notifications(root: Path) -> None:
    """Live-log hooks for `item/completed` and `error`: join the case when upstream has one
    (block form), add one when it has none, stop when the shape is anything else."""
    p = root / PROVIDERS / "codex.ts"
    if not p.is_file():
        return
    s = original = p.read_text()
    # New cases go before the usage case, so the usage hook text stays contiguous.
    for method in ("item/completed", "error"):
        head = f"      case '{method}': {{\n"
        if head in s:
            if s.count(head) != 1:
                raise SystemExit(f"{p}: {method}: upstream shape changed")
            if head + codex_log(method) not in s:
                s = s.replace(head, head + codex_log(method), 1)
        elif f"case '{method}'" in s:
            raise SystemExit(f"{p}: {method}: upstream shape changed")
        else:
            add = f"      case '{method}': {{\n{codex_log(method)}        break;\n      }}\n"
            if s.count(CODEX_UPDATED_HEAD) != 1:
                raise SystemExit(f"{p}: {method}: upstream shape changed")
            s = s.replace(CODEX_UPDATED_HEAD, add + CODEX_UPDATED_HEAD, 1)
    if CODEX_RECORD + CODEX_LOG_TURN not in s:
        if s.count(CODEX_RECORD) != 1:
            raise SystemExit(f"{p}: turn/completed: upstream shape changed")
        s = s.replace(CODEX_RECORD, CODEX_RECORD + CODEX_LOG_TURN, 1)
    if s != original:
        p.write_text(s)
        print(f"patched {p} (live log)")


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
            ("import upgrade", CODEX_IMPORT_OLD, CODEX_IMPORT),
            ("usage state", CODEX_FN, CODEX_STATE + CODEX_FN),
            (
                "usage update",
                CODEX_COMPLETED,
                CODEX_UPDATED + CODEX_COMPLETED + CODEX_RECORD,
            ),
        ],
    )
    log_codex_notifications(root)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
