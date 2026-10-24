# Provenance and rationale — candidate Codex worker policies

Status: **WIP — CANDIDATES, not loaded, not verified at runtime; no deploy approval** (no sudo, no `docker run`
in this stage). They compile offline; nothing here proves Bubblewrap works. The proof is the
[RUNBOOK](RUNBOOK.md). Behaviour change needs a spec before activation (AGENTS.md):
[codex-sandbox-profile.spec.md](../../../docs/specs/codex-sandbox-profile.spec.md). The profile
(`codex-bwrap` by default; `off` to disable) that applies these artifacts is implemented, but the
policies themselves remain candidates.

## Primary sources (consulted 2026-10-19)

| Source | Revision | File sha256 |
|---|---|---|
| moby/profiles `apparmor/template.go` (`docker-default` profile) | `4b8214fc4971e06172938a1f1554ccff6647ee80` (main) | `7da15d81aa5c507f5002b844fdc49a969904681447a2cc29222bd6cbe797163b` |
| moby/profiles `seccomp/default.json` | same | `6416b47770785a41ac59073cdc77d9fe98517df2799dc83ef207e622de3053f6` |
| openai/codex `codex-rs/linux-sandbox/src/{bwrap,launcher,linux_run_main,landlock}.rs`, `cli/src/main.rs` | tag `rust-v0.162.1` → commit `092d3acd6bec3e3a14bdc7e7a2810ab628ab759d` | bwrap.rs `a6b97db5acaec475066f3a5338776f875f7f2033f008ee2532907a1e5e50ffc0`; linux_run_main.rs `3109246473ef2f8132c2d6555bec5b8116612ce58f28927608f322ac5c3fd8b1`; landlock.rs `4f92e49f44694d2c946ab2cb96fa4293daf52023b674a63332a863bb00c1370d` |
| containers/bubblewrap `bubblewrap.c`, `bind-mount.c` | `1fb0aa26d3eb66254f7742e704add7f82f41a582` (main; host has 0.11.0) | bubblewrap.c `de141c805cb88f8faff9af1b3bb49098191fc67833b51b51fea84510ea4426ce`; bind-mount.c `13a01b3d13ee14ed305c6b09debd3784b52c8470a2ecbb4a9e3568792eb9cde6` |

Caveats: installed Docker 29.5.2 (`568f755`) uses the built-in default (`seccomp,profile=builtin`);
it was not proven that `moby/profiles@main` is byte-for-byte what is vendored there. Bubblewrap was read on
`main`, not 0.11.0. Reconfirm both before activating.

## Artifacts (sha256 at time of writing)

- `argus-codex-worker.apparmor` — `0aceddf7817d148537f124fe333782431eb8e90f1af6157d10ddf531179c5106` (v2; v1 `240001ea…` was the one loaded on the host and denied `rslave`, see below)
- `argus-codex-worker.seccomp.json` — `d199b4c8cbe4106a1e5fadb078a37a15a4b56c49d1d28ca6922d94767c81d846` (formatted with Biome; the earlier hash, `f9739dde…`, was raw `jq` output, semantically identical: same parsed JSON)
- `scripts/derive-seccomp.sh` (derivation via `jq`; raw output must go through `biome format --stdin-file-path=argus-codex-worker.seccomp.json` to match the hash above byte-for-byte, required by the repo's `biome check .`), `scripts/validate-offline.sh`

## What Codex 0.162.1 actually asks for (source reading)

- `bwrap.rs`: `--unshare-user --unshare-ipc [--unshare-pid] [--unshare-net] --proc /proc --cap-drop ALL`,
  `--dev /dev`, `--ro-bind`, `--bind`, `--tmpfs`, `--remount-ro`.
- `linux_run_main.rs:422-445,1516`: if mounting `/proc` fails (`Can't mount proc` … `Operation not
  permitted`/`Permission denied`), Codex retries without `--proc`. **That is why the profile does NOT allow `mount
  proc`**: Docker's `maskedPaths` would block it anyway and there is a fallback.
- `landlock.rs`: Codex applies its own seccomp (network) + `PR_SET_NO_NEW_PRIVS`; `seccomp`/`prctl`
  are already in the Docker default.
- `bubblewrap.c`: `clone(SIGCHLD|NEWNS|NEWUSER|NEWPID|NEWNET|NEWIPC[|NEWUTS|NEWCGROUP])` (l.3057-3115),
  `mount(NULL,"/",MS_SLAVE|MS_REC)` (l.3280 — the observed `Failed to make / slave`), `mount tmpfs`
  (l.3284), bind `MS_BIND|MS_REC` (l.3303), `pivot_root` (l.3322,3366), `MS_REC|MS_PRIVATE` (l.3339),
  `umount2(MNT_DETACH)` (l.3342,3370); `bind-mount.c:513`: `MS_BIND|MS_REMOUNT`.

## Deltas vs. Docker default

### AppArmor (`docker-default` rendered with abi 3.0, `tunables/global`, `abstractions/base`)
1. `deny mount,` removed; allow-list added (unlisted mounts stay denied by default). **`options=(...)` matches the
   EXACT flag set** (apparmor.d(5)): `rw` and `silent` (MS_SILENT) must be listed. v2 rules, each with the flags
   bubblewrap actually passes (`bubblewrap.c`/`bind-mount.c` at the hashes above):
   - `options=(rw, silent, rslave) -> /` (l.3280) — **shown necessary**: real probe, v1 denied with
     `flags="rw, silent, rslave"`, `info="failed flags match"` (v1 had `options=(rslave)`).
   - `fstype=tmpfs options=(rw, nosuid, nodev) tmpfs -> /**` (l.3284, 980; no `silent`). Removed the optionless tmpfs rule (it accepted any flags).
   - `options=(rw, silent, bind|rbind) /** -> /**` (l.3303, 3331; bind-mount.c:458).
   - `options=(rw, silent, rprivate) -> /**` (l.3339).
   - `fstype=devpts options=(rw, nosuid, noexec) devpts -> /**` (l.1478).
   - **Removed** the v1 `remount,bind` rule: `MS_BIND|MS_REMOUNT` (bind-mount.c:514,534) is only the fallback without `mount_setattr(2)`;
     if a probe shows `DENIED`, add exactly the logged set. All other v1 rules were wrong for the same reason
     (missing `rw`/`silent`), but only `rslave` was observed.
2. `pivot_root,` added.
3. Nothing else changed: denies of socket families, `/proc` and `/sys` write, `sysrq-trigger`, `kcore`,
   signals and ptrace by peer. No `userns` rule and no ABI 4: the local proof (docker-default AppArmor +
   open seccomp → `unshare -Ur` PASS) shows it is not needed, and abi 3.0 keeps the
   already-measured behaviour.

Per-rule status: **`(rw, silent, rslave) -> /` is shown necessary** (observed error; v2 has still not been loaded).
The others are **candidates deduced from bwrap source, not verified**; the `/**` path patterns are
deliberately wide because bwrap's destination is dynamic (`/tmp/newroot/...`) and was not measured.
RUNBOOK step 6 covers narrowing them via `dmesg`/audit (`apparmor="DENIED" operation="mount"`).

### seccomp (default.json + 4 rules, `defaultAction` stays `SCMP_ACT_ERRNO`)
All with `excludes.caps: CAP_SYS_ADMIN` (anyone with the cap was already allowed upstream):
- `unshare`: only if `flags & ~0x7e020000 == 0` (namespace flags only).
- `clone` (and s390 variant): only if `CLONE_NEWUSER` (0x10000000) is set on the same call.
- `mount`, `umount2`, `pivot_root`: no usable argument condition (target and type are not
  BPF-filterable); real control is the kernel (cap only inside the userns) + AppArmor.
- `clone3` stays `ENOSYS` (upstream); glibc/bwrap fall back to `clone`. `setns`, `bpf`, `fsopen`,
  `move_mount`, `open_tree`, `sethostname` stay `CAP_SYS_ADMIN` only.

## Implications and limits (no claim of equivalence to the default)

- Any process in the worker, not only bwrap, can create user+mount namespaces and mount
  tmpfs/bind/devpts inside them. That grows the kernel surface (userns is historically a source of
  LPE CVEs). The Docker default denied this entirely.
- Inside the userns the process has caps *in that* userns; `no-new-privileges` and the container's empty
  bounding set remain. It does not become root on the host, and has no network (`--network none` on the probe).
- `tmpfs -> /**` allows tmpfs over any path **in its own mount ns**; bind `/** -> /**` allows
  rebinding anything already visible to the container. Open: whether that undoes Docker's
  maskedPaths (e.g. binding an empty directory is harmless, but exposing `/proc/kcore` should
  not be possible because AppArmor still denies `kcore`). **Not verified**: see `mask-bypass` in the RUNBOOK.
- Host `restrict_unprivileged_userns=1`: non-`unconfined` profiles (like this one) are not the target of the
  restriction, consistent with the local proof; an Ubuntu upgrade could change that.
- Named profile `argus-codex-worker` must be loaded on the host before any `docker run`
  that uses it; without it runc fails at start (`change_onexec … label not found`, observed), never at create (fail closed).
