#!/usr/bin/env python3
"""Replace the Codex provider's hook-trust bypass with Codex's normal hook trust.

Runs from install-engines.sh, after patch-codex-cli-path.py: the patched files only
exist once the upstream Codex payload is in place.

usage: patch-codex-hook-trust.py <runner-root>

docs/specs/codex-hook-trust.spec.md. Upstream spawns `codex
--dangerously-bypass-hook-trust` and sends `config.bypass_hook_trust` on every thread.
This removes both, adds the command guard to the hooks.json the provider writes, and
trusts the registered hooks through overlays/codex-hook-trust.ts right after
`initialize`, so a refusal there stops the query before any thread or model turn.
Idempotent; any upstream shape it does not recognise fails the install.
"""

import sys
from pathlib import Path

FORBIDDEN = ("--dangerously-bypass-hook-trust", "bypass_hook_trust")

IMPORT = (
    "import { establishCodexHookTrust, registerExpectedCodexHook, withCodexGuardHook }"
    " from './codex-hook-trust.js';\n"
)
IMPORT_ANCHOR = "import type { McpServerConfig } from './types.js';\n"

ARGS_OLD = "export const CODEX_APP_SERVER_ARGS = ['--dangerously-bypass-hook-trust', 'app-server', '--listen', 'stdio://'] as const;"
ARGS_NEW = "export const CODEX_APP_SERVER_ARGS = ['app-server', '--listen', 'stdio://'] as const;"

SPAWN_COMMENT_OLD = """  // NanoClaw generates the hook config and has no interactive user available
  // to approve hook trust inside the container.
"""
SPAWN_COMMENT_NEW = """  // No hook-trust bypass: initializeCodexAppServer trusts the hooks NanoClaw wrote.
"""

THREAD_CONFIG_OLD = """    // App-server ignores the CLI hook-trust flag; apply it to every thread.
    config: { bypass_hook_trust: true },
"""

INIT_ANCHOR = "  sendCodexNotification(server, 'initialized');\n"
INIT_CALL = """  // Before any thread: an inactive guard or memory hook refuses the query (codex-hook-trust spec).
  const trust = await establishCodexHookTrust((method, params) => sendCodexRequest(server, method, params), {
    cwd: process.cwd(),
  });
  log(`hook trust: ${trust.trusted.length} trusted, ${trust.skipped.length} other hook(s) left untrusted`);
"""

RECONCILE_ANCHOR = """  hooks.SessionStart = nextSessionStart;
  return JSON.stringify(hooksConfig, null, 2) + '\\n';
"""
RECONCILE_NEW = """  hooks.SessionStart = nextSessionStart;
  withCodexGuardHook(hooksConfig);
  registerExpectedCodexHook({ eventName: 'sessionStart', command: memorySessionHook.command });
  return JSON.stringify(hooksConfig, null, 2) + '\\n';
"""

TEST_EDITS = (
    (
        "    expect(CODEX_APP_SERVER_ARGS).toContain('--dangerously-bypass-hook-trust');\n",
        "    expect(CODEX_APP_SERVER_ARGS).toEqual(['app-server', '--listen', 'stdio://']);\n",
        1,
    ),
    (
        "    expect(requests[0].params.config).toEqual({ bypass_hook_trust: true });\n",
        "    expect(requests[0].params.config).toBeUndefined();\n",
        2,  # new thread and resume
    ),
)


def fail(path: Path, what: str) -> None:
    sys.exit(f"{path}: {what}; upstream shape changed")


def replace_once(
    text: str, path: Path, old: str, new: str, what: str, count: int = 1
) -> str:
    """Replace the `count` copies of old, or accept text that already has new."""
    if old in text:
        if text.count(old) != count:
            fail(path, f"{what} found {text.count(old)} times, expected {count}")
        return text.replace(old, new)
    if new and new in text:
        return text
    if not new:
        return text  # a removal already applied; the final check proves it
    fail(path, f"{what} not found")
    return text


def insert_after(text: str, path: Path, anchor: str, insert: str, what: str) -> str:
    if anchor + insert in text:
        return text
    if insert in text:
        fail(path, f"{what} present but not after its anchor")
    if text.count(anchor) != 1:
        fail(path, f"{what} anchor not found exactly once")
    return text.replace(anchor, anchor + insert, 1)


providers = Path(sys.argv[1]) / "container" / "agent-runner" / "src" / "providers"
src = providers / "codex-app-server.ts"
test = providers / "codex-app-server.test.ts"
for p in (src, test):
    if not p.is_file():
        sys.exit(f"{p}: missing; NanoClaw layout changed upstream")

text = original = src.read_text()
if IMPORT not in text:  # other overlays insert after the same anchor
    text = insert_after(text, src, IMPORT_ANCHOR, IMPORT, "codex-hook-trust import")
text = replace_once(text, src, ARGS_OLD, ARGS_NEW, "CODEX_APP_SERVER_ARGS")
text = replace_once(
    text, src, SPAWN_COMMENT_OLD, SPAWN_COMMENT_NEW, "spawn hook-trust comment"
)
text = replace_once(text, src, THREAD_CONFIG_OLD, "", "thread bypass_hook_trust")
text = insert_after(text, src, INIT_ANCHOR, INIT_CALL, "initialize trust call")
text = replace_once(
    text, src, RECONCILE_ANCHOR, RECONCILE_NEW, "reconcileCodexHooksJson return"
)

found = [s for s in FORBIDDEN if s in text]
if found:
    fail(src, f"{', '.join(found)} still present")
for needed in (IMPORT, ARGS_NEW, INIT_CALL, RECONCILE_NEW):
    if text.count(needed) != 1:
        fail(src, f"expected exactly one {needed.strip().splitlines()[0]!r}")
if text.count("establishCodexHookTrust(") != 1:
    fail(src, "establishCodexHookTrust must be called exactly once")
thread = text.find("export async function startOrResumeCodexThread(")
if thread < 0 or text.index(INIT_CALL) > thread:
    fail(src, "hook trust is not established before the first thread")

test_text = test_original = test.read_text()
for old, new, count in TEST_EDITS:
    test_text = replace_once(test_text, test, old, new, "bypass assertion", count)
found = [s for s in FORBIDDEN if s in test_text]
if found:
    fail(test, f"{', '.join(found)} still expected")

if text != original:
    src.write_text(text)
    print(f"patched {src} (Codex hook trust, no bypass)")
if test_text != test_original:
    test.write_text(test_text)
    print(f"patched {test} (no hook-trust bypass)")
