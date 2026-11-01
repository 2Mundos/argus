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
bypass. Background: [draft](../drafts/2026-10-29.command-guard.draft.md),
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
  the rules (`REASONS`, `check()`); the hook and the tests use the same module.
  It is standard library only and runs with the image's `python3`.
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
- **Shell forms.** Commands are split on `;`, `&&`, `||`, `|`, `&`, redirects and
  newlines. `sudo`/`doas`/`env`/`VAR=x` prefixes are skipped, and
  `bash|sh|zsh -c '...'` and `eval` are checked recursively (depth 4).
- **Block message.** `Blocked by Argus command guard: <reason>. Do not retry or
  work around this block.` Returned as a `deny` decision; the task continues and
  the agent decides what to do next.
- **Fail open.** A parse or runtime error in the guard allows the command.
- **Missing guard.** When the guard is enabled, an image without it is an
  environment block (`env_guard_missing`), not a silent unguarded run. **Not
  implemented**; see open items. The image build smoke does fail if the guard is
  absent or does not block `rm -rf /`.
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
  It is not a failure class and not an inbox item. **Not implemented**; the hook
  currently only returns the deny decision.
- **Opt-out (intended).** On by default; a `command_guard.enabled` setting
  disables it for new attempts. The hook already honours the delivery mechanism:
  the environment variable `ARGUS_COMMAND_GUARD=0|off` turns it off. The setting
  and its delivery at claim are **not implemented**.

# 4. Open Questions

- **Open item: settings and event plumbing.** `command_guard.enabled`
  ([settings](settings.spec.md)), passing it to the hook as
  `ARGUS_COMMAND_GUARD`, a worker route or channel to carry
  `task.command_blocked`, its feed/Fleet presentation, and `env_guard_missing`
  in [failure classes](failure-classes.spec.md).
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

- `integrations/nanoclaw/engines/command_guard.py`, `claude-managed-settings.json`
- `engines/Dockerfile.tools-layer` and `engines/apply-tools-layer.sh` install them.
- `integrations/nanoclaw/tests/test_command_guard.py`

## Verification boundary

- Commands: `pytest integrations/nanoclaw/tests/test_command_guard.py`.
- Verified: rule tables (block and allow, shell forms), the hook's stdin/stdout
  contract, fail-open and the env opt-out, at unit level.
- Not verified: the image build, that Claude Code loads the managed settings and
  honours the deny, a blocked `rm -rf ~` on a disposable worker, Codex.
