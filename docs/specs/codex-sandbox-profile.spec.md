# Codex worker sandbox profile (opt-in implemented; profiles candidate)

> **Status.** The per-worker opt-in is **implemented in code and tested offline**. The
> AppArmor and seccomp profiles it applies are **candidates and have NOT passed a runtime
> test**: the first real probe (profile v1, enforce mode, Docker) failed on the positive
> check, `bwrap: Failed to make / slave: Permission denied`, and no task ever ran under the
> profile. Profile v2 corrects the cause but is not loaded yet. Nothing here approves a
> deploy or an activation, and the contract as a whole is **not met** until the probe passes.

# 1. Purpose and Scope

The Codex CLI runs commands inside its own Bubblewrap sandbox. Inside a NanoClaw
worker container, Docker's default AppArmor and seccomp profiles deny the user
and mount namespaces Bubblewrap needs (`Failed to make / slave`), so Codex tasks
that need that sandbox are blocked.

This spec describes an **opt-in, per-worker** security profile for Codex workers
that would allow only what Bubblewrap needs, and the evidence required before
anyone enables it.

In scope:

- The candidate artifacts in
  [`integrations/nanoclaw/security/`](../../integrations/nanoclaw/security/):
  an AppArmor profile and a seccomp profile, each a small delta over the Docker
  defaults, with sources and hashes in
  [`PROVENANCE.md`](../../integrations/nanoclaw/security/PROVENANCE.md).
- The operator procedure that would prove them, in
  [`RUNBOOK.md`](../../integrations/nanoclaw/security/RUNBOOK.md), and the
  offline checks (`scripts/validate-offline.sh`).
- The per-worker opt-in that applies them (decision 4): where it is configured, how
  it reaches the container, and what it refuses.

# 2. Non-Goals

- Loading the AppArmor profile on any host. That needs root and is an explicit
  operator action (RUNBOOK step 1); provisioning never does it.
- Applying the profile to gateways, auxiliary containers, other providers, a Codex
  CLI delegated from another provider (`argus-delegate`), or workers that did not opt
  in.
- `--privileged`, `CAP_SYS_ADMIN`, `unconfined`, global sysctls, or any
  credential handling.
- Disabling the Codex internal sandbox as a workaround.
- A default change: workers without the opt-in keep the Docker defaults.

# 3. Decisions Made

1. **Candidates only.** The artifacts compile offline (`apparmor_parser -QK -S`,
   JSON validity, `defaultAction` of `SCMP_ACT_ERRNO`). Compiling proves syntax,
   not that Bubblewrap works.
2. **Smallest delta.** AppArmor: `deny mount,` becomes an allow-list of the mount
   kinds Bubblewrap uses, plus `pivot_root`. seccomp: four conditional allow
   rules (`unshare`, `clone`, and `mount`/`umount2`/`pivot_root`) for processes
   without `CAP_SYS_ADMIN`. Only `rslave -> /` is demonstrated necessary (kernel log, below); the
   rest are deduced from source and labelled candidates. AppArmor `options=(...)` matches
   the **exact** flag set, so each rule lists `rw` and, where bubblewrap passes it,
   `silent` (MS_SILENT); v1 omitted both and was denied
   (`flags="rw, silent, rslave"`, `info="failed flags match"`). v2 lists them and drops the
   unobserved `remount,bind` rule and the option-less tmpfs rule. The `/**` mount targets
   are deliberately broad until narrowed from audit logs (RUNBOOK step 4).
3. **If proof fails, the task stays blocked.** When the only route is
   `unconfined`, `--privileged`, `CAP_SYS_ADMIN`, or a global sysctl, the Codex
   task remains blocked and the blocker is reported to the owner. The internal
   sandbox is not turned off, and the profile is not relaxed to make it pass.
4. **Opt-in: one pool, one token, fixed profile.** Implemented.
   - *Where.* The pool's existing `env` (`PUT/PATCH /api/pools/{name}`): the variable
     `ARGUS_CODEX_SANDBOX_PROFILE`, only value `codex-bwrap`, default unset (= exactly
     today's behaviour). Argus copies pool `env` into the NanoClaw **host** container
     (`fleet/runtimes.py`), which is the host process's `process.env`. A pool is the
     unit; instances of one pool share it, other pools are untouched. Example:
     `PATCH /api/pools/<pool>` with
     `{"expected_version": "<pool.version>", "env": {"ARGUS_CODEX_SANDBOX_PROFILE": "codex-bwrap"}}`
     (value `null` removes it). It takes effect for NanoClaw host containers created
     afterwards; this change recycles nothing. Argus does not mark a node or template
     ready because of it.
   - *Path.* `composeSessionSpec` resolves the session's provider exactly as spawn does
     (session, then group, then `claude`). Only provider `codex` gets
     `ContainerSpec.securityProfile = "codex-bwrap"` on the **agent** container, a
     closed token, not a flag. `validateSpec` refuses the token on any other role or
     any other value. The Docker driver maps the token to fixed options:
     `--security-opt apparmor=argus-codex-worker` and
     `--security-opt seccomp=<install>/security/argus-codex-worker.seccomp.json`,
     appended after the unchanged `--cap-drop=ALL` / `no-new-privileges` / `--init`.
   - *Not configurable by the agent.* The variable lives in the host process; it is
     not forwarded into the agent container's environment. No profile name, path or
     flag comes from the worker, the agent, or the session spec.
   - *Installed by tracked artifacts.* `install.sh` copies the driver module, tests
     and the seccomp JSON into the checkout and patches `drivers/types.ts`,
     `drivers/docker-driver.ts` and `container-runner.ts` idempotently (a second run
     changes nothing). `deploy/nanoclaw-host/entrypoint.sh` refuses to boot a host
     with a bad value.
5. **Fail closed, no fallback.** Each of these refuses (non-retryable) and nothing is
   created: a value other than `codex-bwrap` (checked for every provider, so a typo is
   never "off"); a private inner Docker daemon (`nanoclaw-sandbox`); a seccomp file that
   is missing, unreadable, not JSON, not an object, without syscall rules, or whose
   `defaultAction` does not deny; a daemon whose `docker info` does not report both
   AppArmor and seccomp; a seccomp path containing a comma or whitespace; and any
   failure of the enforcement probe (decision 6).
6. **Enforcement is proven on the daemon, per spawn, before the task.** `docker info`
   only says AppArmor exists; it does not say the profile is loaded or enforcing. So
   before `docker create` for an opted-in agent the driver runs one disposable container
   on the same daemon (so a remote daemon is the one asked) with the agent's image and
   hardening flags plus the two profile options, `--network none`, `--read-only`,
   `--pull=never`, small pids/memory limits, **no mounts, no env, no credentials**, and
   entrypoint `cat /proc/self/attr/current /proc/self/status`. It requires the label
   `argus-codex-worker (enforce)`, `NoNewPrivs: 1`, `Seccomp: 2` and an empty `CapEff`.
   `complain`, `unconfined`, another profile, an unreadable label, a profile that is not
   loaded (runc `change_onexec ... label not found`, observed) or a probe that cannot run
   all refuse. A pass is cached per client and image for 30 s. Cost: one short container per
   opted-in spawn at most every 30 s. Limit: it proves the state at probe time, not for the
   life of the session, and the container that actually runs is not re-read after start.
   The seccomp JSON is read by the `docker` CLI on the host process's side and inlined
   to the daemon; the AppArmor profile lives in the daemon's kernel and is only visible
   through this probe.
7. **Adoption never downgrades.** `prepare` returns an existing session container
   (idempotency on key). With the opt-in on, the driver first inspects its real host
   config (only `SecurityOpt`, `CapDrop`, `CapAdd`, `Privileged`, `AppArmorProfile`,
   `Init`; never `Config.Env`) and adopts it only if it carries exactly
   `no-new-privileges`, `apparmor=argus-codex-worker`, a `seccomp=` whose content equals the
   installed JSON, `CapDrop=[ALL]`, no `CapAdd`, not privileged, init, and the profile
   still enforces. Anything else is refused and the container is left untouched (not
   stopped, not removed: it may belong to another lease). Without the opt-in adoption is
   unchanged and reads nothing. Labels alone are never trusted.
8. **A backend that cannot realize the profile refuses it.** `DriverCapabilities` gains
   an optional `securityProfiles`; the Docker driver lists `codex-bwrap`. `validateSpec`
   denies a profile the driver does not list (or any when no capabilities handle is given),
   and the spawn path checks `capabilities().securityProfiles` right after composition,
   before claiming the session. The inner-daemon refusal (`ARGUS_NANOCLAW_INNER_DOCKER`)
   stays as an extra guard.

# 4. Open Questions

- Does the probe reach `ALL-AS-EXPECTED` on the real image? One run: **no**. v1 failed to bootstrap
  (`Failed to make / slave`); v2 is untested. Evidence: `2026-10-20` probe log in the evidence folder.
- Which mount destinations actually occur, so `/**` can be narrowed?
- Whether rebinding under the broad bind rule can undo Docker's masked paths
  (`mask-bypass` in the probe).
- Whether moby/profiles `main` equals the profile vendored in the installed
  Docker, and whether Bubblewrap 0.11.0 matches the `main` revision read.
- Whether a kernel or Ubuntu upgrade changes the unprivileged-userns policy.
- Who installs the profile on a node (operator tooling, never the worker). Still open:
  there is deliberately no automation.
- Whether `remount,bind` (bubblewrap's fallback without `mount_setattr(2)`) is ever needed;
  v2 does not allow it. A `DENIED` line in the kernel log would name the exact flags.
- Whether `nanoclaw-sandbox` (private inner daemon) can ever be supported; refused now.
- Whether a Codex CLI delegated from another provider should be covered; excluded now.

# 5. Constraints

- Single-node and no new required service ([single-node](single-node.spec.md)):
  the profile is a file on the node plus Docker options; nothing new runs.
- `--cap-drop=ALL` and `no-new-privileges` stay; nothing privileged,
  `SYS_ADMIN`, `unconfined`, or global sysctl.
- Root is needed to load a profile, so loading is an operator action, never a
  worker action.
- Offline and probe scripts use `$TMPDIR` (scratch), not a shared temporary
  directory, and the probe pipeline must propagate failure (`pipefail`).
- Without the profile loaded, the container does not start (runc error at start, not
  at create); see decision 6. Never a silent fallback.

# 6. Implementation Consequences

Ready in code (verified offline by `integrations/nanoclaw/security/scripts/verify-install.sh`:
install twice into a disposable pristine checkout, `tsc --noEmit`, the real driver and
composition tests; and `UPGRADE_FROM=` an already-installed v1 checkout, which must end
byte-identical to the pristine install):

1. `integrations/nanoclaw/argus-codex-sandbox.ts` (+ two test files),
   `patch-codex-sandbox.py` (the idempotent, upgrading patches `install.sh` runs),
   the `entrypoint.sh` guard, `provision.sh` running the tests.
2. The candidate profiles (AppArmor v2), `PROVENANCE.md`, `RUNBOOK.md` and its scripts;
   `probe.sh` now treats a sandbox that does not bootstrap as `BOOTSTRAP-FAILED` (exit 3,
   every dependent check `NOT_EXECUTED`) and cannot pass a negative check that failed
   before its command started.

**Not proven, and not claimed:**

1. Bubblewrap under the profiles: v1 failed at the first mount; v2 has not been loaded or run.
2. Whether a real Docker daemon accepts every generated option and reports the output the
   probe parser expects: tests use a recording CLI (the label/status layout is taken from
   the real probe log).
3. The spawn-path capability gate in `container-runner.ts` is covered through its helper and
   `validateSpec`, not by running `spawnContainer`.
4. The first real spawn with the opt-in on is the first time a task runs under the profile.

Next, each its own PR: the operator reloads AppArmor v2 and attaches a passing probe log as a
dated report; narrow the `/**` mount rules; only then enable one pool.
