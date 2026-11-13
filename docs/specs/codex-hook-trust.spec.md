---
status: partial
created: 2026-11-13
updated: 2026-11-13
---

# Codex hook trust

# 1. Purpose and Scope

The upstream NanoClaw Codex provider starts `codex --dangerously-bypass-hook-trust
app-server` and sends `config: { bypass_hook_trust: true }` on every thread, so Codex
runs any hook without checking it. Argus removes the bypass and uses Codex's own hook
trust instead: the provider trusts exactly the hooks it wrote, by the hash Codex
computed, and refuses the query when a protection hook is not active.

In scope: the native Codex app-server provider in the NanoClaw agent runner
(`container/agent-runner/src/providers/codex-app-server.ts`), as installed by
`engines/install-engines.sh`, and the [command guard](command-guard.spec.md) and
memory `SessionStart` hooks it registers.

# 2. Non-Goals

- Codex run as a delegated executor (`argus-delegate`): it reads its own config and
  hooks, and is not covered.
- Trusting hooks a project or a user adds: they stay untrusted, so Codex skips them.
- Changing the provider's sandbox mode or approval policy.

# 3. Decisions Made

- **No bypass.** `patch-codex-hook-trust.py` removes `--dangerously-bypass-hook-trust`
  from `CODEX_APP_SERVER_ARGS` and `bypass_hook_trust` from the thread parameters, and
  updates the upstream test assertions that expected them. `verify-install.sh` fails an
  installed provider that still contains either string.
- **Hooks written.** `reconcileCodexHooksJson` adds the guard
  (`python3 /usr/local/lib/argus/command_guard.py`, `PreToolUse`, matcher `Bash`,
  timeout 10 s) once, keeping other entries, and registers the memory `SessionStart`
  hook it wrote as expected.
- **Trust.** Right after `initialize`, before any thread start or resume,
  `establishCodexHookTrust` (`overlays/codex-hook-trust.ts`) calls `hooks/list` for the
  working directory, finds each expected hook by event, command, matcher, `source:
  user` and the provider's `hooks.json` path, and writes `hooks.state` with one
  `config/batchWrite` (`mergeStrategy: upsert`, `{ "<key>": { trusted_hash } }`) for
  the ones not yet `trusted`. Hook keys contain the file path and its dots, so trust is
  written as one table, not through a dotted key path. It then lists again.
- **Fail closed.** The query is refused, with no thread and no model turn, when the
  guard script is missing from the image, `hooks/list` fails or reports errors, an
  expected hook is not listed, is disabled, has an unknown trust status or hash, or is
  not `trusted` after the write. Hooks that are not ours are reported and left
  untrusted.
- **Opt-out.** `ARGUS_COMMAND_GUARD=0|off` still turns the guard off: the hook stays
  trusted and the script allows every command. `install.sh` adds the variable to the
  Codex environment allowlist so it reaches the hook.
- **Upstream drift.** The patcher is idempotent, converges from a pristine, an
  installed or a repeated run, writes nothing on failure, and fails the install with
  "upstream shape changed" when an anchor it needs is missing.
- **Rollback.** Revert the change and reinstall the engines (`install-engines.sh` or
  an image rebuild); the upstream provider then runs with its bypass again.

# 4. Open Questions

## Open items

- A live Codex 0.162.1 model turn under this provider: that the shell tool reaches
  the hook as `Bash` and the deny is honoured. `hooks/list` and `config/batchWrite`
  were probed against the real 0.162.1 app-server; no model turn was run.
- Activation on a pool: it takes effect only after the agent image is rebuilt or the
  engines are reinstalled; live provider access is unverified.

# 5. Constraints

- No network and no clock reads in the trust step; it uses the app-server only.
- The guard rules stay in `command_guard.py`; this spec only registers and trusts it.

# 6. Implementation Consequences

- `integrations/nanoclaw/overlays/codex-hook-trust.ts` (+ `.test.ts`, bun:test)
- `integrations/nanoclaw/patch-codex-hook-trust.py`,
  `integrations/nanoclaw/tests/test_patch_codex_hook_trust.py`
- `integrations/nanoclaw/engines/install-engines.sh` copies the overlay and runs the
  patcher after `patch-codex-cli-path.py`.
- `integrations/nanoclaw/install.sh` (env allowlist),
  `integrations/nanoclaw/security/scripts/verify-install.sh` (no-bypass check).

## Verification boundary

- Commands: `pytest integrations/nanoclaw/tests/test_patch_codex_hook_trust.py`;
  `bun test overlays/codex-hook-trust.test.ts` (from `integrations/nanoclaw`).
- Verified: the patcher on excerpts (pristine, after `patch-codex-cli-path.py`,
  repeated, each changed anchor fails without writing) and on a copy of the real
  installed runner, whose Codex provider tests then pass as before; trust selection
  and every refusal against a fake app-server; the `hooks.state` write turning a hook
  `trusted` on a real 0.162.1 app-server.
- Not verified: a model turn, the guard's deny inside a live Codex session, a rebuilt
  agent image.
