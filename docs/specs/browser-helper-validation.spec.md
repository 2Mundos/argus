---
status: partial
created: 2026-11-09
updated: 2026-11-09
---

# Browser-helper validation assets (isolated Chromium)

# 1. Purpose and Scope

Opt-in assets that let an operator check, on their own node, whether Chromium's
namespace sandbox can run **ON** inside a dedicated browser-helper container that
keeps Argus's container protections. They validate a candidate seccomp profile and
the hardened run shape. They do not integrate a browser into workers, the fleet or
the runtime images.

In scope:

- `deploy/browser-helper/seccomp/moby-default.json`: the pinned moby default
  profile, copied exactly (baseline).
- `deploy/browser-helper/seccomp/chromium-userns.json`: the candidate, the baseline
  plus exactly one appended unconditional allow rule for `clone`, `unshare` and
  `chroot`. `setns` is not added.
- `deploy/browser-helper/provenance.json`: source URLs, pinned commit, sha256 of
  both profiles and of the original evidence scripts they were adapted from.
- `deploy/browser-helper/smoke.mjs`: the in-container check over raw CDP.
- `scripts/browser_helper_smoke.py`: `check` (assets against provenance and the
  one-rule rule), `derive` (reproduce the candidate byte for byte) and `run`.
- Tests: `scripts/test_browser_helper_smoke.py`.

Related: [codex-sandbox-profile](codex-sandbox-profile.spec.md),
[project-runtime](project-runtime.spec.md), [security-hardening](security-hardening.spec.md).

# 2. Non-Goals

- No security approval and no production readiness. The candidate enables
  unprivileged user namespaces (new-user-namespace kernel attack surface) for any
  container that uses it; never apply it to agent runtimes.
- No worker, fleet, orchestrator or deployment wiring, no host AppArmor, sysctl or
  other global change, no loaded profile.
- No claim that the baseline equals the profile embedded in a given Docker engine;
  that was not verified.
- No Playwright (`chromiumSandbox: true`), GPU or external-network coverage.

# 3. Decisions Made

1. **Container shape.** uid/gid 1000, `--cap-drop=ALL`, `no-new-privileges`, the
   enforced `docker-default` AppArmor profile plus the candidate seccomp profile,
   private IPC, `--network=none`, read-only root, tmpfs `/tmp` for HOME and config,
   and bounds: 2g memory (no extra swap), 512 pids, 2 cpus, 1g shm, 512m tmpfs,
   `--ulimit core=0`, 90 s inside the container and a finite outside timeout in
   (0, 600] s (default 150). `--pull=never`. No sandbox-disabling
   flag, no `unconfined`, no privileged mode, no `SYS_ADMIN`.
   `assert_hardened` refuses an argv that lacks any of these or adds a forbidden one.
2. **Image.** Required, never defaulted, and only an image ID or `name@sha256:digest`
   (tags move). It must already exist locally.
3. **Output.** A fresh absolute directory outside the repository, mode `0700`,
   owned by the caller; an existing directory must be empty and owned by the caller.
   The container writes as uid 1000, so `run` requires host uid 1000 and fails
   otherwise (no world-writable fallback). After the run the host re-verifies the
   artefacts independently of the script's own verdict: every required assertion
   present exactly once and `true`, a PNG whose hash matches, and a download whose
   hash equals the known served constant (not the hash the smoke reports).
   `receipt.json` in the output directory records the docker argv, requested and
   resolved image ID, return code, bounded stdout/stderr, timeout outcome and
   status, also on failure. Receipts hold machine-local IDs, so keep them in
   scratch, never in git.
4. **Fail closed.** `smoke.mjs` writes `all_pass=false` and exits 3 on any exception
   or after an 80 s deadline. Assertions: DOM and JS result, PNG screenshot and hash,
   download hash, `chrome://sandbox` "adequately sandboxed" with the Namespace layer,
   and for every Chromium process its full argv (no bypass flag), all four
   Uid/Gid fields 1000,
   `docker-default (enforce)`, NoNewPrivs 1, Seccomp 2, namespaces and capabilities
   (container-level processes hold none; the only non-zero set is `CAP_SYS_CHROOT`
   inside Chromium's own single-uid user namespace; renderers hold none). The
   outer user-namespace maps are recorded but no property of them is asserted. A live
   process whose `/proc` files cannot be read fails the run
   (`all_live_procs_inspected`); only a process that has exited is skipped.
5. **Lifecycle.** The container has a unique name and a label. On timeout, error or
   interrupt only that container is killed and `docker rm -f`ed; nothing is matched
   by name pattern.

# 4. Open Questions

## Open items

- Security owner approval of the candidate for a dedicated browser-helper container.
- Playwright with `chromiumSandbox: true`, GPU, external network, other kernels and
  Docker versions, and heavier or longer browsing workloads.
- Whether the baseline matches a given Docker engine's embedded default.
- Behaviour if the host AppArmor policy later restricts user namespaces for
  `docker-default` (a loaded profile with `userns,` would be needed).
- Wiring a render check and the profile into the helper launch path (separate spec).

# 5. Constraints

- Python standard library and Node 22 builtins only; no new dependency.
- The two seccomp JSON files are byte-exact copies and are excluded from Biome.
- Run only on a node where a throwaway container is acceptable; the check never
  touches the preview install.

# 6. Implementation Consequences

- `deploy/browser-helper/`, `scripts/browser_helper_smoke.py`,
  `scripts/test_browser_helper_smoke.py`, `scripts/check_changed.py` (maps the assets
  to the test), `biome.json`.

```bash
python3 scripts/browser_helper_smoke.py check
python3 scripts/browser_helper_smoke.py run --image <sha256:id> --out /abs/fresh/dir
```

## Verification boundary

- Commands: `uv run pytest -q -p no:cacheprovider scripts/test_browser_helper_smoke.py`;
  `python3 scripts/browser_helper_smoke.py run …` (needs Docker).
- Verified: derivation, one-rule check, hardening flags (including tmpfs, shm and
  core limits), image, timeout and output-directory rules, output rejection and
  timeout cleanup at unit level; real runs on Docker 29.5.2 with Chromium 156
  passing all 18 assertions with the candidate and failing closed with the
  baseline. `smoke.mjs` itself is exercised only by those real runs, not by unit
  tests. The source evidence's earlier 17/17 replay is historical, not proof.
- Not verified: everything listed under Open items.
