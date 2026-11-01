# Agent container resource hygiene: OOM priority and CPU count

# 1. Purpose and Scope

Agent session containers run NanoClaw's harness (Claude Code or Codex) and the
commands it starts inside one cgroup. Two failures follow when that container has
a memory or CPU quota:

1. **The wrong process dies on OOM.** With no swap, the kernel kills the largest
   process, often the harness rather than the build it started. The attempt then
   looks like `session_lost` instead of a tool failure.
2. **Tools oversubscribe the CPU.** `os.cpus()`, `os.cpu_count()` and
   `OMP`-style defaults report every host CPU, so a test runner starts one worker
   per host core inside a quota of two.

This spec defines the two container-side behaviours:

- **OOM priority.** Shell commands the harness runs get `oom_score_adj=500`, so a
  runaway command is the kernel's first victim. The harness and the NanoClaw agent
  runner keep their score.
- **CPU count.** When a session container has a CPU quota, CPU-count defaults
  report the quota instead of the host.

It also records how the OOM classification already on `main`
([failure classes](failure-classes.spec.md) 3.2.1) reads these kills. The
classification is unchanged.

Out of scope: autoscaling pools by memory pressure, per-task resource budgets,
and changing pool requests. Where the quota comes from is set by
[worker launch](worker-launch.spec.md) (`CONTAINER_CPU_LIMIT` and
`CONTAINER_MEMORY_LIMIT`), not here.

# 2. Non-Goals

- No change to the harness or the NanoClaw agent runner's own priority. They stay
  at `oom_score_adj=0`, so the agent survives a command it started and can report
  the kill.
- No capability is added to any container. Raising `oom_score_adj` needs none;
  lowering it would, and this spec never lowers it.
- No CPU limit is invented. Without a quota, nothing changes: no variable is set,
  no preload is loaded, and `os.cpus()` reports the host.
- No change to `nproc`. It reports the scheduler affinity, not the quota.
- No new `NODE_OPTIONS` or `PYTHONPATH` that overrides a value the project or the
  user set.

# 3. Decisions Made

## 3.1 OOM priority for harness child commands

- The shim `argus-oom-priority.sh` lives in the agent image (tools layer), not in
  the per-task mount. It is loaded through `BASH_ENV` (non-interactive bash) and
  `CLAUDE_ENV_FILE` (the file Claude Code's command shells source). Children
  inherit the score from their parent shell.
- **Raise-only.** The shim reads `/proc/self/oom_score_adj` and writes `500` only
  when the current value is lower. It never writes a lower value and never fails
  the command: a read or write error is a silent no-op.
- The harness process and the NanoClaw agent runner are not shell children of the
  shim and keep `0`.
- Whether Claude Code and Codex shells honour `BASH_ENV` and `CLAUDE_ENV_FILE` is
  **unverified** until a live session shows it (see section 4).

## 3.2 CPU count matches the quota

- The quota is read from the container's cgroup `cpu.max` (`<quota> <period>`;
  `max` means no quota). `CONTAINER_CPU_LIMIT` is a fallback when the file is
  missing. The count is `max(1, ceil(quota / period))`.
- When there is no quota, nothing is set (section 2).
- With a quota, the shim sets these variables **only if they are unset**, so a
  value the project or the user sets wins:
  - `OMP_NUM_THREADS` and `PYTHON_CPU_COUNT` to the count.
- **Node.** A preload module trims `os.cpus()` to the count. It is loaded with
  `--require` appended to `NODE_OPTIONS`. A project that replaces `NODE_OPTIONS`
  loses the preload; a later shell started under the shim sees it missing, logs one
  line to stderr so the loss is visible, restores it, and the command still runs.
- **Python.** `PYTHON_CPU_COUNT` is honoured only on Python 3.13 and later
  (unverified here: no 3.13 interpreter was available). For older Python a
  `sitecustomize` on `PYTHONPATH` overrides `os.cpu_count()`. `python -S` skips it;
  that is accepted.

## 3.3 Delivery

- The install-time patch `integrations/nanoclaw/patch-agent-resource-hygiene.py`
  edits NanoClaw's `container-runner.ts` so session containers get `BASH_ENV` and
  `CLAUDE_ENV_FILE`, both naming `argus-oom-priority.sh` in the tools layer
  (`/usr/local/lib/argus`). That shim sources `argus-cpu-count.sh`, which reads the
  quota inside the container and adds the preload and `sitecustomize` paths only
  when there is one; the patch sets neither `NODE_OPTIONS` nor `PYTHONPATH`. It follows the
  idempotent "ensure this exact text" pattern of the other `patch-*.py` files and
  stops the install if the upstream shape changed.
- The patch is wired into `install.sh` after `patch-no-session-ceiling.py`.

## 3.4 Classification (unchanged)

- A kernel OOM kill of a command is seen through the container's `memory.events`
  `oom_kill` count and a session's exit 137 with `OOMKilled`, as
  [failure classes](failure-classes.spec.md) 3.2.1 already defines. This spec adds
  no new fact and no new class. Host-wide kills are not classified.

# 4. Open Questions

- Do Claude Code and Codex shells read `BASH_ENV` and `CLAUDE_ENV_FILE`? Unverified.
  Until a live session confirms it, OOM priority may not apply; the failure mode
  is "no effect", not a broken command.
- Does `oom_score_adj` writes succeed under the AppArmor profile in production?
  Unverified (the profile is not loaded in the development environment).
- Live check: in a disposable container with `--memory 512m --cpus 2`, an unbounded
  allocation in a shell command is killed while the harness survives, and
  `os.cpus()`, `os.cpu_count()` and `OMP_NUM_THREADS` report 2. Not yet run.

# 5. Constraints

- Everything is shell-level and environment-level. No Argus process reads or
  writes another container's cgroup.
- The shim and preload must be safe to source twice and must not print to stdout
  (commands' output is parsed by the harness).
- The CPU count and score never depend on the host. A host with 24 CPUs and a
  quota of 2 reports 2.
- English only in the shim, preload and tests.

# 6. Implementation Consequences

- New files under `integrations/nanoclaw/`: `argus-oom-priority.sh`,
  `argus-cpu-count.sh`, `argus-cpu-count.cjs` (Node preload),
  `argus-cpu-count-sitecustomize.py` (installed as `pycpu/sitecustomize.py`),
  `patch-agent-resource-hygiene.py`, and their tests. The shims are copied into the
  agent image by the tools layer (`Dockerfile.tools-layer`), not into the NanoClaw
  checkout.
- `install.sh` runs the new patch after `patch-no-session-ceiling.py`.
- Tests: the score rule (raise only, never lower, no-op on error), the CPU count
  from `cpu.max` and from the fallback variable, no-quota no-op, set-only-if-unset,
  the `NODE_OPTIONS` append and its log line, the sitecustomize override, and the
  patch's idempotence and upstream-shape guard.
- [Docs README](../README.md) links this spec and the draft's status.
- The draft `docs/drafts/2026-10-29.agent-resource-hygiene.draft.md` records its
  resulting status.
