# Codex worker sandbox profile (candidate)

> **WIP / CANDIDATE. Nothing here is implemented, loaded, or runtime-verified.**
> No deploy or activation is approved by this spec or by the artifacts it
> describes. Every config field named below is a proposal.

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
- The shape of a future opt-in (decision 4), as a proposal.

# 2. Non-Goals

- Activating the profiles, loading them on any host, or changing the NanoClaw
  driver. The driver today fixes `--cap-drop=ALL` and `no-new-privileges` and
  exposes no AppArmor or seccomp option.
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
   without `CAP_SYS_ADMIN`. Only `rslave -> /` is demonstrated necessary; the
   rest are deduced from source and labelled candidates. The `/**` mount targets
   are deliberately broad until narrowed from audit logs (RUNBOOK step 4).
3. **If proof fails, the task stays blocked.** When the only route is
   `unconfined`, `--privileged`, `CAP_SYS_ADMIN`, or a global sysctl, the Codex
   task remains blocked and the blocker is reported to the owner. The internal
   sandbox is not turned off, and the profile is not relaxed to make it pass.
4. **Proposed opt-in (not implemented).** An optional per-worker field (for
   example `securityProfile: "codex-bwrap"`), default unset. When set, the driver
   would add the AppArmor and seccomp options, fail early if the profile is not
   loaded, and never relax `--cap-drop=ALL` or `no-new-privileges`. Name and
   shape are open (section 4).

# 4. Open Questions

- Does the probe reach `ALL-AS-EXPECTED` on the real image? Nobody has run it.
- Which mount destinations actually occur, so `/**` can be narrowed?
- Whether rebinding under the broad bind rule can undo Docker's masked paths
  (`mask-bypass` in the probe).
- Whether moby/profiles `main` equals the profile vendored in the installed
  Docker, and whether Bubblewrap 0.11.0 matches the `main` revision read.
- Whether a kernel or Ubuntu upgrade changes the unprivileged-userns policy.
- Who installs the profile on a node (operator tooling, never the worker).

# 5. Constraints

- Single-node and no new required service ([single-node](single-node.spec.md)):
  the profile is a file on the node plus Docker options; nothing new runs.
- `--cap-drop=ALL` and `no-new-privileges` stay; nothing privileged,
  `SYS_ADMIN`, `unconfined`, or global sysctl.
- Root is needed to load a profile, so loading is an operator action, never a
  worker action.
- Offline and probe scripts use `$TMPDIR` (scratch), not a shared temporary
  directory, and the probe pipeline must propagate failure (`pipefail`).
- Without the profile loaded, Docker refuses to create the container (fail
  closed).

# 6. Implementation Consequences

This PR is documentation and candidate artifacts only:

1. This spec and its link in the docs index.
2. The AppArmor and seccomp candidates, `PROVENANCE.md`, `RUNBOOK.md`, and three
   scripts under `integrations/nanoclaw/security/`.

A later change, each step its own PR after the owner reviews the probe result:

1. Operator runs the RUNBOOK and attaches the log as a dated report.
2. Narrow the mount rules to the observed destinations.
3. Driver option and provisioning, per decision 4 of section 3, with tests.
