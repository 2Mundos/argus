/**
 * Per-worker opt-in to the Codex sandbox profile (docs/specs/codex-sandbox-profile.spec.md).
 *
 * Installed by integrations/nanoclaw/install.sh into src/drivers/. Everything here
 * runs in the NanoClaw HOST process, never in an agent container:
 *
 *   pool env ARGUS_CODEX_SANDBOX_PROFILE=codex-bwrap   (the operator, per pool)
 *     -> host process.env -> composeSessionSpec (only provider 'codex', only the
 *        agent container) -> ContainerSpec.securityProfile (a closed token)
 *     -> DockerSessionDriver -> `--security-opt apparmor=…` + `--security-opt seccomp=…`
 *
 * No flag string, profile name or path ever comes from the worker, the agent or the
 * spec: the driver maps the token to constants fixed below. Nothing falls back to the
 * Docker default or to `unconfined`; every failure is a non-retryable denial.
 */
import fs from "fs";
import { fileURLToPath } from "url";

import type { Cli } from "./cli.js";
import { deniedByPolicy, specInvalid } from "./types.js";

export const CODEX_SANDBOX_ENV = "ARGUS_CODEX_SANDBOX_PROFILE";
export const CODEX_SANDBOX_PROFILE = "codex-bwrap" as const;
export type SecurityProfileId = typeof CODEX_SANDBOX_PROFILE;

/** The name `apparmor_parser` loads from integrations/nanoclaw/security/argus-codex-worker.apparmor. */
export const APPARMOR_PROFILE_NAME = "argus-codex-worker";
export const SECCOMP_FILE_NAME = "argus-codex-worker.seccomp.json";

/** Only the native Codex provider; a Codex CLI delegated from another provider is not covered. */
const PROVIDER = "codex";

// A seccomp profile that does not deny by default is not the delta over Docker's default
// that the candidate documents; refuse it (SCMP_ACT_ALLOW, _LOG, _NOTIFY, _TRACE, …).
const DENYING_DEFAULT_ACTIONS = new Set([
  "SCMP_ACT_ERRNO",
  "SCMP_ACT_KILL",
  "SCMP_ACT_KILL_PROCESS",
  "SCMP_ACT_KILL_THREAD",
  "SCMP_ACT_TRAP",
]);

/**
 * The profile this worker opted into, or undefined when it did not (the default).
 * The only accepted value is the exact token; anything else refuses, so a typo can
 * never mean "off" or "on with something else".
 */
export function codexSandboxProfileFromEnv(env: NodeJS.ProcessEnv): SecurityProfileId | undefined {
  const raw = env[CODEX_SANDBOX_ENV]?.trim();
  if (!raw) return undefined;
  if (raw !== CODEX_SANDBOX_PROFILE) {
    throw specInvalid(
      `${CODEX_SANDBOX_ENV}='${raw}' is not a known profile (only '${CODEX_SANDBOX_PROFILE}'); unset it to keep the default`,
    );
  }
  if (env.ARGUS_NANOCLAW_INNER_DOCKER === "1") {
    // The profile lives in the kernel of the node's Docker daemon; a private daemon inside a sandbox runtime cannot load or enforce it.
    throw specInvalid(
      `${CODEX_SANDBOX_ENV} is not supported with a private inner Docker daemon (nanoclaw-sandbox)`,
    );
  }
  return raw;
}

/** The profile for a session: the opt-in applies to the native Codex provider only. */
export function securityProfileForProvider(
  provider: string,
  env: NodeJS.ProcessEnv,
): SecurityProfileId | undefined {
  const profile = codexSandboxProfileFromEnv(env); // validated for every provider: a bad value must not pass silently
  return provider === PROVIDER ? profile : undefined;
}

export interface SeccompSource {
  /** Path handed to the docker CLI, which reads the file on the CLIENT side and sends its content to the daemon. */
  path: string;
  read: (path: string) => string;
}

/** `<install>/security/…`, from either `src/drivers/` or `dist/drivers/`. Not configurable. */
export function defaultSeccompSource(): SeccompSource {
  return {
    path: fileURLToPath(new URL(`../../security/${SECCOMP_FILE_NAME}`, import.meta.url)),
    read: (p) => fs.readFileSync(p, "utf8"),
  };
}

export function validateSeccompProfile(text: string): void {
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch (error) {
    throw deniedByPolicy(`seccomp profile is not valid JSON (${(error as Error).message})`);
  }
  const profile = doc as { defaultAction?: unknown; syscalls?: unknown } | null;
  if (typeof profile !== "object" || profile === null || Array.isArray(profile)) {
    throw deniedByPolicy("seccomp profile must be a JSON object");
  }
  if (
    typeof profile.defaultAction !== "string" ||
    !DENYING_DEFAULT_ACTIONS.has(profile.defaultAction)
  ) {
    throw deniedByPolicy(
      `seccomp profile defaultAction must deny (one of ${[...DENYING_DEFAULT_ACTIONS].join(", ")})`,
    );
  }
  if (!Array.isArray(profile.syscalls) || profile.syscalls.length === 0) {
    throw deniedByPolicy("seccomp profile has no syscall rules");
  }
}

/**
 * `docker info` SecurityOptions from the daemon that will run the container. Docker
 * silently ignores `apparmor=` on a daemon without AppArmor, so absence must refuse.
 * Whether the named profile is LOADED in that kernel is not visible from the client:
 * runc fails the container at start in that case (never a silent default).
 */
export function assertDaemonSupportsProfile(infoSecurityOptions: string): void {
  let options: unknown;
  try {
    options = JSON.parse(infoSecurityOptions);
  } catch {
    throw deniedByPolicy("could not read the Docker daemon security options (docker info)");
  }
  const names = Array.isArray(options)
    ? options.filter((o): o is string => typeof o === "string")
    : [];
  if (!names.includes("name=apparmor")) {
    throw deniedByPolicy(
      `the Docker daemon does not report AppArmor enforcement; cannot apply '${APPARMOR_PROFILE_NAME}'`,
    );
  }
  if (!names.some((o) => o === "name=seccomp" || o.startsWith("name=seccomp,"))) {
    throw deniedByPolicy(
      "the Docker daemon does not report seccomp support; cannot apply the Codex seccomp profile",
    );
  }
}

/**
 * The only security flags a profile adds. `--cap-drop=ALL` and `no-new-privileges`
 * stay in `hardeningArgs`; this appends and never replaces.
 */
export function securityProfileArgs(
  profile: SecurityProfileId | undefined,
  source: SeccompSource,
): string[] {
  if (profile === undefined) return [];
  if (profile !== CODEX_SANDBOX_PROFILE)
    throw specInvalid(`unknown security profile '${String(profile)}'`);
  // docker splits --security-opt values on commas; a path with one (or a space) would change the option.
  if (/[,\s]/.test(source.path))
    throw deniedByPolicy(
      `seccomp profile path ${source.path} must not contain commas or whitespace`,
    );
  let text: string;
  try {
    text = source.read(source.path);
  } catch (error) {
    throw deniedByPolicy(
      `seccomp profile ${source.path} is not readable (${(error as Error).message}); run integrations/nanoclaw/install.sh`,
    );
  }
  validateSeccompProfile(text);
  return [
    "--security-opt",
    `apparmor=${APPARMOR_PROFILE_NAME}`,
    "--security-opt",
    `seccomp=${source.path}`,
  ];
}

/** A driver without the capability must refuse the spec: never validate-then-ignore a security profile. */
export function assertDriverSupportsSecurityProfiles(
  capabilities: { securityProfiles?: readonly string[] },
  spec: { containers: ReadonlyArray<{ role: string; securityProfile?: string }> },
  driverKind = "this driver",
): void {
  for (const container of spec.containers) {
    if (
      container.securityProfile !== undefined &&
      !capabilities.securityProfiles?.includes(container.securityProfile)
    ) {
      throw deniedByPolicy(
        `driver '${driverKind}' cannot realize security profile '${container.securityProfile}' (capabilities().securityProfiles); refusing instead of running without it`,
      );
    }
  }
}

const ENFORCE_TTL_MS = 30_000;
const SEP = " ARGUS-SEP ";
const enforcedUntil = new WeakMap<Cli, Map<string, number>>();

/**
 * The disposable container that proves the profile is ENFORCING on the daemon that will run the
 * task: same image and flags as the agent, but no mounts, no env, no credentials, no network,
 * a read-only root and an entrypoint that only reads /proc. Nothing privileged is added.
 */
export function enforcementProbeArgs(image: string, securityArgs: readonly string[]): string[] {
  return [
    "run",
    "--rm",
    "--pull=never",
    "--network",
    "none",
    "--cap-drop=ALL",
    "--security-opt",
    "no-new-privileges",
    ...securityArgs,
    "--read-only",
    "--pids-limit",
    "32",
    "--memory",
    "64m",
    "--entrypoint",
    "cat",
    image,
    "/proc/self/attr/current",
    "/proc/self/status",
  ];
}

/** `cat /proc/self/attr/current /proc/self/status` output must show enforce, not complain/unconfined/absent. */
export function assertEnforced(output: string): void {
  const [first = "", ...rest] = output.split("\n");
  const label = first.trim();
  if (
    label !== `${APPARMOR_PROFILE_NAME} (enforce)` &&
    !label.startsWith(`${APPARMOR_PROFILE_NAME} (enforce)Name:`)
  ) {
    throw deniedByPolicy(
      `AppArmor profile '${APPARMOR_PROFILE_NAME}' is not enforcing for containers on this daemon (process label: '${label.slice(0, 80) || "empty"}'); load it in enforce mode (RUNBOOK step 1)`,
    );
  }
  const status = rest.join("\n");
  if (
    !/^NoNewPrivs:\s+1$/m.test(status) ||
    !/^Seccomp:\s+2$/m.test(status) ||
    !/^CapEff:\s+0+$/m.test(status)
  ) {
    throw deniedByPolicy(
      "the profile probe did not show NoNewPrivs=1, Seccomp=2 and an empty CapEff",
    );
  }
}

export const ADOPTION_INSPECT_FORMAT = [
  "{{json .HostConfig.SecurityOpt}}",
  "{{json .HostConfig.CapDrop}}",
  "{{json .HostConfig.CapAdd}}",
  "{{json .HostConfig.Privileged}}",
  "{{json .AppArmorProfile}}",
  "{{json .HostConfig.Init}}",
].join(SEP);

const sameJson = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/**
 * An existing container may be adopted as an opted-in session only if its REAL host config matches what a
 * fresh create would have produced. Reads hardening fields only (never Config.Env). The seccomp option
 * is compared by content: the docker CLI inlines the file, so this ties it to the installed JSON.
 */
export function assertAdoptable(inspectOutput: string, expectedSeccompText: string): void {
  const parts = inspectOutput.trim().split(SEP);
  if (parts.length !== 6)
    throw deniedByPolicy("cannot read the host config of the existing session container");
  let secOpt: unknown,
    capDrop: unknown,
    capAdd: unknown,
    privileged: unknown,
    apparmor: unknown,
    init: unknown;
  try {
    [secOpt, capDrop, capAdd, privileged, apparmor, init] = parts.map((p) => JSON.parse(p));
  } catch {
    throw deniedByPolicy("cannot parse the host config of the existing session container");
  }
  const refuse = (what: string) =>
    deniedByPolicy(
      `existing session container was not created with the Codex sandbox profile (${what}); not adopting it`,
    );
  const opts = Array.isArray(secOpt)
    ? (secOpt as unknown[]).filter((o): o is string => typeof o === "string")
    : [];
  const seccomp = opts.filter((o) => o.startsWith("seccomp="));
  const rest = opts.filter((o) => !o.startsWith("seccomp="));
  const noNewPrivs = rest.filter(
    (o) => o === "no-new-privileges" || o === "no-new-privileges=true",
  );
  if (seccomp.length !== 1) throw refuse("seccomp");
  let actual: unknown, expected: unknown;
  try {
    actual = JSON.parse(seccomp[0].slice("seccomp=".length));
    expected = JSON.parse(expectedSeccompText);
  } catch {
    throw refuse("seccomp is not the installed JSON");
  }
  if (!sameJson(actual, expected)) throw refuse("seccomp content differs from the installed JSON");
  if (
    rest.length !== 2 ||
    noNewPrivs.length !== 1 ||
    !rest.includes(`apparmor=${APPARMOR_PROFILE_NAME}`)
  )
    throw refuse("security options");
  if (apparmor !== APPARMOR_PROFILE_NAME) throw refuse("apparmor");
  if (!sameJson(capDrop, ["ALL"])) throw refuse("cap-drop");
  if (!(capAdd === null || (Array.isArray(capAdd) && capAdd.length === 0))) throw refuse("cap-add");
  if (privileged !== false) throw refuse("privileged");
  if (init !== true) throw refuse("init");
}

export interface GuardOptions {
  seccompSource?: SeccompSource;
  now?: () => number;
}

/** Everything the Docker driver needs for an opted-in agent; the driver keeps only thin hooks. */
export class CodexSandboxGuard {
  constructor(
    private readonly cli: Cli,
    private readonly opts: GuardOptions,
    private readonly normalize: (error: unknown) => Error,
  ) {}

  private get source(): SeccompSource {
    return this.opts.seccompSource ?? defaultSeccompSource();
  }

  /** Flags for `docker create`, after the daemon is shown to ENFORCE the profile. Refusals create nothing. */
  args(agent: { image: string; securityProfile?: SecurityProfileId }): string[] {
    const args = securityProfileArgs(agent.securityProfile, this.source);
    if (args.length === 0) return args;
    let info: string;
    try {
      info = this.cli.run(["info", "--format", "{{json .SecurityOptions}}"], { timeoutMs: 10_000 });
    } catch (error) {
      throw this.normalize(error);
    }
    assertDaemonSupportsProfile(info);
    this.assertEnforcing(agent.image, args);
    return args;
  }

  /** Throws unless an existing container was created with the profile, and the profile enforces now. */
  assertAdoptable(
    name: string,
    agent: { image: string; securityProfile?: SecurityProfileId },
  ): void {
    const args = securityProfileArgs(agent.securityProfile, this.source);
    if (args.length === 0) return;
    let out: string;
    try {
      out = this.cli.run(["inspect", "--format", ADOPTION_INSPECT_FORMAT, name]);
    } catch (error) {
      throw this.normalize(error);
    }
    assertAdoptable(out, this.source.read(this.source.path));
    this.assertEnforcing(agent.image, args);
  }

  private assertEnforcing(image: string, securityArgs: readonly string[]): void {
    const now = (this.opts.now ?? Date.now)();
    const cache = enforcedUntil.get(this.cli) ?? new Map<string, number>();
    enforcedUntil.set(this.cli, cache);
    if ((cache.get(image) ?? 0) > now) return;
    let out: string;
    try {
      out = this.cli.run(enforcementProbeArgs(image, securityArgs), { timeoutMs: 30_000 });
    } catch (error) {
      const first = String((error as Error).message ?? error)
        .split("\n")[0]
        .slice(0, 200);
      throw deniedByPolicy(
        `profile enforcement probe could not run (${first}); the AppArmor profile may not be loaded`,
      );
    }
    assertEnforced(out);
    cache.set(image, now + ENFORCE_TTL_MS);
  }
}
