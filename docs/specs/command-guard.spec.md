---
status: partial
created: 2026-11-01
updated: 2026-11-06
---

# Command guard

# 1. Purpose and Scope

Workers run Claude Code without approval prompts. The container, the
[Codex sandbox profile](codex-sandbox-profile.spec.md) and OneCLI credential
injection bound what a bad command can reach, but nothing stops an agent from
destroying what it can reach inside that boundary (the mounted workspace, the
home directory, a disk device).

The command guard is a `PreToolUse` hook on Claude Code's Bash tool that refuses
a short list of catastrophic commands and tells the agent why. It is **accident
prevention, not a sandbox**: it catches the obvious mistake, not a determined
bypass. Background: draft (removed; in git history),
[research](../research/2026-10-29.command-guard.research.md).

# 2. Non-Goals

- Allow-lists, LLM-based command review, network egress rules.
- Containing a hostile or prompt-injected agent. Obfuscation (`eval` of a decoded
  string, scripts written then run, `find -delete`) is not detected.
- Coverage of Cursor, opencode, the `local` executor, Copilot, or Codex (see
  Decisions). They run no verified native hook, so the guard must not be
  described as covering them.
- Helper-deletion rules (`gh repo delete`, `argus-gitea-api` / `argus-github-api`
  deletes): the helpers expose no repository delete command, so there is nothing
  to guard.

# 3. Decisions Made

- **One source of truth.** `integrations/nanoclaw/engines/command_guard.py` holds
  the catastrophic-command rules (`REASONS`, `check()`); the hook and the tests
  use the same module. Co-author trailer detection lives in
  `commit_attribution.py`, imported by the guard for Bash `git commit` one-liners
  and by the worker git wrapper / commit-msg hook. Standard library only.
- **Rules.** Each id has a reason and a paired block/allow test; a new rule
  cannot ship without both.
  - `rm_root`: recursive `rm` of `/`, `~`, `$HOME`, `/home/*` or `/root`
    (trailing `/` and `/*` included). `rm -rf ./build` and `rm -rf /tmp/x` pass.
  - `no_preserve_root`: `rm --no-preserve-root`.
  - `mkfs`: `mkfs`, `mkfs.*`, `mke2fs`.
  - `dd_device`: `dd of=/dev/<disk>` (`sd`, `hd`, `vd`, `xvd`, `nvme`, `mmcblk`,
    `loop`, `md`, `dm-`).
  - `device_redirect`: `> /dev/<disk>`.
  - `fork_bomb`: the classic `:(){ :|:& };:`.
  - `commit_attribution`: a `git commit` (including in a compound shell command)
    whose `-m`/`--message`/`-F`/`--file` text contains a prohibited co-author
    or attribution trailer line (case-insensitive), uses `--no-verify`/`-n`, or
    passes `-c core.hooksPath=…` on the command line. Editor-only commits are covered by the workspace commit-msg hook instead;
    publication is covered by the git wrapper on `git push` ([worker lifecycle](worker-lifecycle.spec.md)
    3.8).
- **Shell forms.** Commands are split on `;`, `&&`, `||`, `|`, `&`, redirects and
  newlines. `sudo`/`doas`/`env`/`VAR=x` prefixes are skipped, and
  `bash|sh|zsh -c '...'` and `eval` are checked recursively (depth 4).
- **Block message.** `Blocked by Argus command guard: <reason>. Do not retry or
  work around this block.` Returned as a `deny` decision; the task continues and
  the agent decides what to do next.
- **Fail open.** A parse or runtime error in the guard allows the command.
- **Missing guard.** When the guard is enabled, an image without it is an
  environment block (`env_guard_missing`), not a silent unguarded run. At claim,
  before any lease, the worker reads `/etc/claude-code/managed-settings.json` from
  the image the agent session will run (the agent group's `image_tag`, else
  NanoClaw's `CONTAINER_IMAGE`, as NanoClaw's container runner picks it) for a
  `PreToolUse` `Bash` hook running `command_guard.py`. It reads it through a
  throwaway container (`docker run --rm --pull=never --network=none --entrypoint
  cat`), never from its own filesystem: the worker runs in the NanoClaw host image,
  which has no managed settings. An image that cannot be resolved or is not on the
  daemon, a missing, unreadable or malformed file, or a missing hook blocks the task.
  Skipped only when the guard is off (`ARGUS_COMMAND_GUARD=0|off`) and for engines
  without a hook. A test adapter without a session image reads the channel's
  `managedSettingsPath`, else `ARGUS_CLAUDE_MANAGED_SETTINGS`, else that path, on
  its own filesystem. The agent image sets `ARGUS_CLAUDE_MANAGED_SETTINGS` to that
  path, and its build smoke (`engines/probe-managed-settings.sh`) fails if the file
  does not register `command_guard.py` exactly once and `repeat_guard.py`, or if the
  guard does not block `rm -rf /`.
- **Registration.** Claude Code managed settings baked into the image
  (`/etc/claude-code/managed-settings.json`, source
  `engines/claude-managed-settings.json`), so a project's own `.claude` settings
  cannot remove the hook. Installed by `engines/Dockerfile.tools-layer`
  (`apply-tools-layer.sh`). The NanoClaw base image build is outside this repo.
- **Codex: not implemented.** Codex hook support and trusted-hash registration in
  the pinned version are unverified (research section 3), so no Codex hook is
  shipped. Do not claim Codex coverage until a probe confirms it.
- **Visibility (intended).** Each block is an informational, persistent
  `task.command_blocked` event with the reason and a redacted, truncated command.
  It is not a failure class and not an inbox item. The event type and its data
  shape (`tool`, `rule`, `reason`) exist in `argus_core` (`events.py`). Fleet
  activity (Home, Events, the status bar and the task feed share one event list)
  shows the rule id and reason only, never command text, even if the event data
  carries it. Readers see it through the existing event read model and its task
  read scope; no failure, retry or inbox count includes it (tested). The hook
  emission and the worker route are **not implemented**; the hook currently only
  returns the deny decision.
- **Opt-out (intended).** On by default; a `command_guard.enabled` setting
  disables it for new attempts. The hook already honours the delivery mechanism:
  the environment variable `ARGUS_COMMAND_GUARD=0|off` turns it off. The setting
  is registered ([settings](settings.spec.md) 3.9). The claim carries the
  effective value (project override over global) as `command_guard_enabled`;
  when false the NanoClaw channel sets `ARGUS_COMMAND_GUARD=off` for that attempt
  only and restores the pool's value afterwards. The `install.sh` container-runner
  patch forwards the variable to the agent container.

# 4. Open Questions

## Open items

Not built yet (section 3 marks each **not implemented**):

- `task.command_blocked` event emission: the event type, data shape and Fleet
  presentation exist; still missing are the hook emission and the worker route.
- Codex coverage (hook support and trusted-hash registration).

- **Open item: event plumbing.** A worker route or channel to carry
  `task.command_blocked`, in
  [failure classes](failure-classes.spec.md).
- Codex: hook support, trusted-hash registration, whether the hook runs inside
  Bubblewrap, and whether it survives `patch-codex-sandbox.py`.
- Claude Code managed-settings path and precedence in the pinned Claude Code
  version are not verified against a real image.
- Claude Code and Codex run as executors by `argus-delegate` are covered only if
  they pick up the managed settings (unverified).

# 5. Constraints

- English only; no network calls and no state in the hook.
- It must add no measurable latency: one short-lived `python3` process per Bash
  call.
- The block message wording is fixed so agents and tests can rely on it.

# 6. Implementation Consequences

- `integrations/nanoclaw/argus-guard.ts` (presence check),
  `integrations/nanoclaw/engines/command_guard.py`, `commit_attribution.py`,
  `claude-managed-settings.json`, `argus-git`, `git-hooks/commit-msg`
- `engines/Dockerfile.tools-layer` and `engines/apply-tools-layer.sh` install them.
- `integrations/nanoclaw/tests/test_command_guard.py`

## Verification boundary

- Commands: `pytest integrations/nanoclaw/tests/test_command_guard.py`,
  `pytest integrations/nanoclaw/tests/test_commit_attribution.py`,
  `pytest integrations/nanoclaw/tests/test_tools_layer_permissions.py` (Docker
  probe when available).
- Verified: rule tables (block and allow, shell forms), the hook's stdin/stdout
  contract, fail-open and the env opt-out, at unit level; the claim-time presence
  check against a stubbed session image (`argus-guard.test.ts`,
  `argus-single-layer.test.ts`).
- Not verified: the image build, that Claude Code loads the managed settings and
  honours the deny, a blocked `rm -rf ~` on a disposable worker, Codex.
