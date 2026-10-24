# Runbook — prove the Codex internal sandbox with the candidate policies

> **WIP / CANDIDATE.** Per-worker opt-in **is implemented** (step 7), but the policies **have not been
> loaded or tested at runtime** and **nothing here authorizes deploy or activation**. Turn the pool on only after
> `ALL-AS-EXPECTED` from step 3 on the image that will be used. **Current state:** the probe with the v1 (enforce) profile failed the
> positive check (`bwrap: Failed to make / slave`, kernel: `flags="rw, silent, rslave"`); the v2 profile fixes the rule but
> **has still not been loaded or tested**. Nothing has passed at runtime. Contract: [spec](../../../docs/specs/codex-sandbox-profile.spec.md).

The operator runs this (needs sudo; the agent session does not have it: `sudo -n` →
"interactive authentication is required"). **Nothing below was run by the agent**, except steps 0 and 7a
(offline). Steps 1, 3, 4 and 6 need root/Docker and remain pending.
Run from `integrations/nanoclaw/security/`. Image:
`argus-nanoclaw-agent:radar-74b160e-deadline7200`. No `--privileged`, no `SYS_ADMIN`, no global
sysctl, no `unconfined`, no Docker restart.

## 0. Offline validation (no root)
```bash
scripts/validate-offline.sh
scripts/verify-install.sh /path/to/nanoclaw-git-checkout   # disposable fixture: install.sh twice + tsc + driver tests
```

## 1. Install and load the profile (root)
```bash
sudo apparmor_parser -QK -S argus-codex-worker.apparmor >/dev/null   # re-validate as root
sudo install -m 0644 -o root -g root argus-codex-worker.apparmor /etc/apparmor.d/argus-codex-worker
sudo apparmor_parser -r -W /etc/apparmor.d/argus-codex-worker
sudo aa-status | grep -n argus-codex-worker      # must list under "profiles are in enforce mode"
```
Boot persistence: `/etc/apparmor.d/` is re-read by `apparmor.service`. Profile not loaded ⇒ the
container **does not start** (runc fails at `start`, not `create`; closed failure, no fallthrough to the default).

Reload from v1 to v2 (same commands; `-r` replaces the loaded profile without restarting Docker). Containers
already running with the profile keep the one they loaded until recreated. After reload confirm the mode:
```bash
sudo aa-status | grep -n argus-codex-worker       # "enforce"; if it shows under "complain", the code refuses the spawn
```

## 2. Prepare a disposable workspace and canaries (no credentials, no host root)
```bash
export PROBE=$(mktemp -d "${TMPDIR:-$HOME/.hermes/cache/scratch}/argus-codex-probe.XXXXXX")
mkdir -p "$PROBE/ws" "$PROBE/other" && chmod 0777 "$PROBE/ws" "$PROBE/other"
export IMG=argus-nanoclaw-agent:radar-74b160e-deadline7200
```

## 3. Run the probe (disposable container, no network, caps ALL dropped, no-new-privileges)
```bash
set -o pipefail   # without this, `| tee` masks the probe's exit code
docker run --rm --init --name argus-codex-probe \
  --network none --cap-drop ALL \
  --security-opt no-new-privileges \
  --security-opt apparmor=argus-codex-worker \
  --security-opt seccomp="$PWD/argus-codex-worker.seccomp.json" \
  --pids-limit 256 --memory 512m --tmpfs /tmp:rw,nosuid,nodev,size=64m \
  -v "$PROBE/ws:/workspace/group" -v "$PROBE/other:/workspace/other" \
  -v "$PWD/scripts/probe.sh:/probe.sh:ro" \
  --entrypoint /bin/bash "$IMG" /probe.sh 2>&1 | tee "$PROBE/probe.log"
```
Codex command shape: in 0.162.1 the source (`cli/src/main.rs`, parse tests) uses
`codex sandbox --permission-profile :workspace -- <cmd>`; the older form
`sandbox linux --full-auto` is not a subcommand in this version (host `codex 0.163.0` answered
`Failed to execvp linux`). The probe prints `sandbox --help` so you can confirm.

### Expected output
- Header: `argus-codex-worker (enforce)`, `Seccomp: 2`, `NoNewPrivs: 1`, `CapEff: 0000…`.
- **Controls** `control-write-*` = PASS (if they fail, the negatives prove nothing).
- `write-workspace` PASS and `write-other/home/etc-denied` PASS + `absent:*` PASS.
- `net-denied` with a printed code; `EPERM/EACCES` ⇒ sandbox block; `ENETUNREACH` ⇒ only
  `--network none` (Codex on `:workspace` may or may not block; do not claim more than that).
- "blast radius" block: `userns-tmpfs-mount` PASS (accepted by the candidate), other mounts PASS = denied.
- Final result `ALL-AS-EXPECTED` (exit 0). Exit 1 = some check failed; exit 2 = Codex binary missing;
  **exit 3 = `BOOTSTRAP-FAILED`**: the Codex sandbox did not start, negative checks stay `NOT_EXECUTED` and **nothing was proven**
  (a negative only counts if the command actually started; the script requires the marker). Any FAIL ⇒ do not activate; see step 4.
- Network: `PASS net-blocked-by-sandbox` only with EPERM/EACCES; `INFO net-blocked-only-by-docker-network-none` does not prove the sandbox.
- Blast radius uses `unshare -Urm --propagation unchanged` so `unshare` itself does not try to mount `/`; if it never starts, the
  checks stay `NOT_EXECUTED`.

If `write-workspace` fails, the profile is still not enough: the mount rules are candidates.

## 4. Diagnosis and tightening (root)
```bash
sudo journalctl -k --since "-10min" | grep 'apparmor="DENIED"'   # operation="mount"/"pivot_root", name=, flags=
```
Adjust only the rule cited in `DENIED`, reload (`apparmor_parser -r -W`) and repeat step 3. After it
passes, replace `/**` with the observed destinations and attach the log to a `YYYY-MM-DD.*.report.md`.
Seccomp failure (no DENIED in the kernel, `EPERM`/`ENOSYS`): `strace` would need `SYS_PTRACE`; isolate with
`unshare -Ur true` and `unshare -Urm sh -c 'mount -t tmpfs t /mnt'` inside the same `docker run` as step 3.

## 5. If it is NOT possible without relaxing
If the only path is `unconfined`, `--privileged`, `CAP_SYS_ADMIN` or a global sysctl: **stop** and
**keep the task blocked** (the Codex worker does not run in that configuration) and report the block to
the owner. **Do not** turn off Codex's internal sandbox as a workaround: that would need an owner decision in
another PR/spec. Do not relax anything here.

## 6. Cleanup and rollback (root; only with no dependents)
```bash
rm -rf "$PROBE"                                           # no sudo; it is yours
# dependents (running OR stopped: a start with a missing profile fails):
docker ps -a --format '{{.Names}}' | xargs -r docker inspect --format '{{.Name}} {{.HostConfig.SecurityOpt}}' | grep argus-codex-worker
# only if the output above is empty:
sudo apparmor_parser -R /etc/apparmor.d/argus-codex-worker && sudo rm /etc/apparmor.d/argus-codex-worker
```

## 7. Per-pool profile — only after step 3 (default on)

`ARGUS_CODEX_SANDBOX_PROFILE` in the pool `env` (nothing global). **Default on:** missing/blank =
`codex-bwrap`. Values: `codex-bwrap` | `off` (escape hatch). Applies only to the **agent** container of the
native **`codex`** provider; gateways/auxiliaries, other providers and delegated Codex (`argus-delegate`)
do not get it. `nanoclaw-sandbox` (internal daemon) **requires** `off` (on is refused).

**a) Install the artifacts in the checkout (no root, idempotent; what `provision.sh` already runs):**
```bash
integrations/nanoclaw/install.sh /path/to/nanoclaw-checkout    # copies the driver and seccomp JSON to <checkout>/security/
(cd /path/to/nanoclaw-checkout && pnpm run build)               # the host runs dist/
```

**b) Preflight (the code does this itself; these are the manual equivalents):**
```bash
sudo aa-status | grep -n argus-codex-worker                        # profile loaded, under "enforce"
docker info --format '{{json .SecurityOptions}}'                   # name=apparmor and name=seccomp… (necessary, NOT sufficient)
jq -e '.defaultAction=="SCMP_ACT_ERRNO"' /path/to/nanoclaw-checkout/security/argus-codex-worker.seccomp.json
```
`docker info` only says AppArmor exists; it does not prove the profile is loaded or in enforce. So before each
`docker create` of an opted-in agent (30 s cache per image), the driver runs **one disposable container on the same daemon** with the
agent image and flags, `--network none --read-only --pull=never`, no mounts, no env, no credentials, and reads
`/proc/self/attr/current` and `/proc/self/status`. It requires `argus-codex-worker (enforce)`, `NoNewPrivs: 1`, `Seccomp: 2` and zeroed
`CapEff`; `complain`, `unconfined`, another profile, unloaded profile (runc `change_onexec … label not found`) or probe failure
**refuse** the spawn (denied, no fallback). Limit: proves state at probe time, not across the session. An existing session
container is adopted only if `docker inspect` (only `SecurityOpt`, `CapDrop`, `CapAdd`, `Privileged`,
`AppArmorProfile`, `Init`) shows exactly the profile, seccomp identical to the installed JSON and `cap-drop ALL`; otherwise it is refused and
**not touched**. Without root the profile cannot be loaded; in that case **do not turn it on**.

**c) Set on explicitly (optional — already the default) or turn a pool off:**
```bash
# explicit on (redundant with the default):
curl -fsS -X PATCH "$ARGUS_URL/api/pools/<pool>" -H "Authorization: Bearer $ARGUS_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"expected_version":"<pool.version>","env":{"ARGUS_CODEX_SANDBOX_PROFILE":"codex-bwrap"}}'
# off (architectures that cannot enforce, e.g. inner Docker):
curl -fsS -X PATCH "$ARGUS_URL/api/pools/<pool>" -H "Authorization: Bearer $ARGUS_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"expected_version":"<pool.version>","env":{"ARGUS_CODEX_SANDBOX_PROFILE":"off"}}'
```
Unknown value (`true`, `unconfined`, `CODEX-BWRAP`…): the host refuses to start (`entrypoint.sh`, exit 78) and,
if spawn is reached, the spawn is denied. There is no fallback to `unconfined`.

**d) Remove the key from env:** `"env":{"ARGUS_CODEX_SANDBOX_PROFILE":null}` on the same `PATCH`; new hosts
return to **default on** (`codex-bwrap`). Already-created agent containers only change when recreated.
Unload AppArmor (step 6) only after no dependent container remains.

**Do not:** arbitrary `security_opt`, `unconfined`, `--privileged`, `SYS_ADMIN`, enable outside the Codex provider,
or recycle live workers as part of this.
