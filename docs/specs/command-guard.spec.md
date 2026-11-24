---
status: partial
created: 2026-11-01
updated: 2026-11-24
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
- Coverage of Cursor, opencode, the `local` executor, Copilot, or Codex run by
  `argus-delegate` (see Decisions). They run no verified native hook, so the guard must not be
  described as covering them. Claude Code run by `argus-delegate` is covered: it runs in
  the agent image and reads the same managed settings (verified, section 6).
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
  - `kubeconfig_secret`: any command other than `kubectl` (and `export`) that
    names a `task-integrations` path, and `kubectl config view --raw`/`--flatten`.
    `kubectl --kubeconfig <path> get pods` passes. It stops an accidental print
    or copy of the attempt's kubeconfig; it is not a boundary
    ([integrations](integrations.spec.md) 3.6).
  - Commit attribution, one reason id per trigger family (all in a Bash command,
    including a compound shell command). Editor-only commits are covered by the
    workspace commit-msg hook instead; publication is covered by the git wrapper on
    `git push` ([worker lifecycle](worker-lifecycle.spec.md) 3.8).
    - `commit_identity_env`: `GIT_AUTHOR_NAME`/`_EMAIL` or `GIT_COMMITTER_NAME`/`_EMAIL`
      set as an assignment, `export` or `env` argument. Element: the variable name.
    - `commit_identity_config`: `-c`/`--config` or `--config-env` with a `user.*`,
      `author.*` or `committer.*` `name`/`email` key, or a `git config` write of one.
      Element: the key.
    - `commit_author_flag`: `git commit --author`. Element: `--author`.
    - `hooks_path_override`: `-c core.hooksPath=…` on the command line. Element:
      `core.hooksPath`.
    - `commit_no_verify`: `--no-verify` or `-n` on `git commit`. Element: the flag.
    - `commit_coauthor_trailer`: a `-m`/`--message`/`-F`/`--file` text with a
      prohibited co-author or attribution trailer line (case-insensitive). Element:
      the trailer line, stripped and cut at 120 characters.
    The push-side refusals of the git wrapper (`push_wide`, `push_non_branch_refspec`,
    `push_follow_tags`, `git_dir_without_work_tree`, `push_outgoing_trailer`) have
    their own family ids in `commit_attribution.py`; the Bash guard cannot produce
    them, so they have no guard reason.
- **Shell forms.** Commands are split on `;`, `&&`, `||`, `|`, `&`, redirects and
  newlines. `sudo`/`doas`/`env`/`VAR=x` prefixes are skipped, and
  `bash|sh|zsh -c '...'` and `eval` are checked recursively (depth 4).
- **Block message.** `Blocked by Argus command guard: <reason>. Do not retry or
  work around this block.` Returned as a `deny` decision; the task continues and
  the agent decides what to do next. For the commit-attribution families the
  refusal names the trigger family (its reason) and the offending element:
  `<reason> (<element>)`. The decision is the first trigger in evaluation order:
  identity environment assignment, identity overrides over the git commands,
  then per commit the forbidden git config and the commit arguments (no-verify,
  then trailers). When other families were also present the message adds
  `; also present: <id>, <id>` after the element, each other family once.
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
  without a hook. The same read runs before a worker exists: pool discovery,
  launch and the reconciler's start check the pool's agent image and refuse to
  start a worker into it while the hook is missing ([worker
  launch](worker-launch.spec.md), `command_guard` check). That check is a
  preflight, not a replacement: the claim still checks the image the session
  will actually run. A test adapter without a session image reads the channel's
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
- **Codex: native app-server path.** The NanoClaw Codex provider writes the guard
  into its `hooks.json` as a `PreToolUse` hook on `Bash` and trusts it through
  Codex's own hook trust before the first thread; a guard that is missing, disabled
  or untrusted refuses the query ([Codex hook trust](codex-hook-trust.spec.md)).
  Codex run as a delegated executor (`argus-delegate`) is out of scope: `codex exec`
  does not read Claude's managed settings and the agent image has no Codex hook
  configuration (no `~/.codex/hooks.json`, no `/etc/codex`), so it runs unguarded.
  Live verification of a Codex turn is owned by [Codex hook trust](codex-hook-trust.spec.md)
  (its open items), not by this spec. Block events
  are not posted for Codex: the detection reads Claude transcripts only.
- **Visibility.** Each block is an informational, persistent
  `task.command_blocked` event (`tool`, `rule`, `reason`, `attempt`, `command`). It
  is not a failure class, not an inbox item, and changes no task state or progress.
  The hook stays side-effect free and only prints the deny decision. The NanoClaw
  channel tails the attempt's Claude transcript (`argus-command-blocked.ts`), pairs a
  Bash `tool_result` with `is_error` whose text is the block message (bare, or inside
  the `<tool_use_error>PreToolUse:Bash hook error: …</tool_use_error>` wrapper Claude
  Code writes) with its
  `tool_use` by id, maps the reason text to the closed rule id (a table a test keeps
  equal to `REASONS`) and posts `POST /api/tasks/{id}/command-blocked`
  `{attempt, tool_use_id, rule, command}` once per `tool_use_id`, with the host's
  token. The API validates the rule against the closed set (422 `unknown_rule`),
  takes the reason from its own table (`argus_core.events.COMMAND_GUARD_REASONS`, also
  kept equal to `REASONS`), and stores the command after control-character cleaning
  and `redact`, collapsed to one line and cut at 200 characters with `…`. It is fenced
  like progress (a stale attempt is 409 `stale_attempt`). The pair (task, attempt,
  `tool_use_id`) is unique (`task_command_blocks`), so a retry records one event
  (`status: duplicate`); an attempt records at most 20 events and later denials are
  `status: dropped`, not stored. A task retry clears the rows. A command can print the
  block message itself, so an event proves only that the transcript said so; it is
  informational. Fleet activity (Home, Events, the status bar and the task feed share
  one event list) shows the rule id and reason only, never command text. Readers see
  it through the existing event read model and its task read scope; no failure, retry
  or inbox count includes it (tested). Fleet health counts the events per task and
  per project for the selected window ([fleet health](fleet-health.spec.md) 3):
  read-only, from the events (never `task_command_blocks`), ids, projects and counts
  only, and no enforcement change.
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

- The Docker path: `integrations/nanoclaw/scripts/claude-guard-live-probe.sh --image
  <image>` (a throwaway `docker run --rm --pull=never --network=none` container) and
  `sessionImageCommandGuardBlock` with its default reader (`readImageFile`), each
  against the configured base image (NanoClaw's `CONTAINER_IMAGE`) and the project
  image, on a host with a Docker daemon. The live probe has only run inside the
  project image itself (section 6), where no Docker CLI or socket exists.

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
- `integrations/nanoclaw/argus-command-blocked.ts` (detection and posting),
  `apps/api/src/argus_api/tasks/command_blocked.py`, migration
  `0077_task_command_blocks.sql`
- `integrations/nanoclaw/tests/test_command_guard.py`
- `integrations/nanoclaw/scripts/claude-guard-live-probe.sh` and
  `fake-anthropic.mjs` (`FAKE_ANTHROPIC_BASH`): the live probe.
- Codex: `integrations/nanoclaw/overlays/codex-hook-trust.ts` and
  `patch-codex-hook-trust.py` ([Codex hook trust](codex-hook-trust.spec.md)).

## Verification boundary

- Live probe: `integrations/nanoclaw/scripts/claude-guard-live-probe.sh [--image IMAGE]
  [--delegate]`. Claude Code as installed in the image runs one turn against
  `fake-anthropic.mjs` (`FAKE_ANTHROPIC_BASH`: a benign marker write, then
  `rm --no-preserve-root --version > <denied marker>`) in a fresh HOME with a dummy
  key and the CLI's default permission mode, while user, project and local settings
  set `disableAllHooks` and an empty `PreToolUse`. It passes when `PreToolUse:Bash`
  hook events appear, the hook returns the Argus deny, the transcript carries an
  `is_error` tool_result with the block message, the benign marker holds its text,
  the denied marker is absent and the CLI exits 0. `--delegate` runs the command
  line `argus-delegate` builds for its Claude executor (`buildCommand`).
- Commands: `pytest integrations/nanoclaw/tests/test_command_guard.py`,
  `pytest integrations/nanoclaw/tests/test_commit_attribution.py`,
  `pytest integrations/nanoclaw/tests/test_tools_layer_permissions.py` (Docker
  probe when available).
- Verified: rule tables (block and allow, shell forms), the hook's stdin/stdout
  contract, fail-open and the env opt-out, at unit level; the event route and
  service (`apps/api/tests/test_task_command_blocked.py`: one event, dedupe, the
  per-attempt limit, stale attempt, rule validation, redaction and bounds, task state
  unchanged) and the transcript detection and posting
  (`integrations/nanoclaw/argus-command-blocked.test.ts`) on synthetic transcripts; the claim-time presence
  check against a stubbed session image (`argus-guard.test.ts`,
  `argus-single-layer.test.ts`); the pool-start check against a fake target
  (`apps/api/tests/test_launch.py`: missing, malformed, registered with the
  shipped settings, unverifiable, guard off).
- Verified live (2026-11-24) inside the project agent image
  `argus-project-runtime:argus-b88b194735b6`, Claude Code 2.1.296, no Docker: the
  probe passes plain (permission mode `auto`, the CLI default) and with `--delegate`
  (`acceptEdits`, `--allowed-tools Bash,Edit,Write,Read,Glob,Grep`). Claude Code
  reads `/etc/claude-code/managed-settings.json` (its settings watcher lists it after
  the user, project and local files), and their `disableAllHooks` does not remove the
  managed hook. The real denial is an `is_error` tool_result
  `<tool_use_error>PreToolUse:Bash hook error: Blocked by Argus command guard: …
  </tool_use_error>`; the detection reads that transcript line into one event
  (`argus-command-blocked.test.ts` keeps the wrapper). `sessionImageCommandGuardBlock`
  for that image returned null with an in-image file reader, and `env_guard_missing`
  with the default reader (`spawn docker ENOENT`).
- Not verified: the Docker path (Open items), the image build, a blocked `rm -rf ~`
  on a disposable worker, a live Codex turn ([Codex hook trust](codex-hook-trust.spec.md)).
