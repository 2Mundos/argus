// Pure checks for installed-provider-e2e.ts (docs/specs/codex-hook-trust.spec.md).
// Isolation is MEASURED from /proc and PATH through the injected probe, never read from a
// worker env flag: only ARGUS_IMAGE_DIGEST (provenance, not isolation) is taken from env.
import path from "path";

export const EXIT_PASSED = 0;
export const EXIT_FAILED = 1;
export const EXIT_USAGE = 2;
export const EXIT_BLOCKED = 3;

export const FORBIDDEN = ["--dangerously-bypass-hook-trust", "bypass_hook_trust"] as const;
export const TRUST_IMPORT = "from './codex-hook-trust.js';";
/** codex-bwrap runs with --cap-drop=ALL (argus-codex-sandbox.ts): no effective capability. */
export const EXPECTED_CAP_EFF = "0000000000000000";
const LIVE_RUNTIME = "/app";

export interface Probe {
  readFile(file: string): string | null;
  /** `command -v <name>` on PATH, or null. */
  which(name: string): string | null;
  /** stdout of a command, or null when it could not run. */
  run(file: string, args: string[]): string | null;
  env: Readonly<Record<string, string | undefined>>;
}

export interface Isolation {
  imageDigest: string | null;
  uid: number | null;
  capEff: string | null;
  noNewPrivs: number | null;
  seccomp: number | null;
  apparmor: string | null;
  bwrap: { path: string | null; version: string | null };
}

function statusField(status: string | null, name: string): string | null {
  const m = status?.match(new RegExp(`^${name}:\\s*(\\S+)`, "m"));
  return m ? m[1] : null;
}

function int(value: string | null): number | null {
  return value !== null && /^\d+$/.test(value) ? Number(value) : null;
}

export function measureIsolation(probe: Probe): Isolation {
  const status = probe.readFile("/proc/self/status");
  const digest = probe.env.ARGUS_IMAGE_DIGEST?.trim() ?? "";
  const bwrapPath = probe.which("bwrap");
  return {
    imageDigest: digest === "" ? null : digest,
    uid: int(statusField(status, "Uid")),
    capEff: statusField(status, "CapEff"),
    noNewPrivs: int(statusField(status, "NoNewPrivs")),
    seccomp: int(statusField(status, "Seccomp")),
    apparmor: probe.readFile("/proc/self/attr/current")?.replace(/\0/g, "").trim() || null,
    bwrap: {
      path: bwrapPath,
      version: bwrapPath ? (probe.run(bwrapPath, ["--version"])?.trim() ?? null) : null,
    },
  };
}

/** Every reason the run must not start; empty means the measured isolation is as required. */
export function isolationBlockers(iso: Isolation): string[] {
  const blockers: string[] = [];
  if (!iso.bwrap.path) blockers.push("bwrap is not on PATH");
  if (iso.capEff !== EXPECTED_CAP_EFF)
    blockers.push(`CapEff is ${iso.capEff ?? "unknown"}, expected ${EXPECTED_CAP_EFF}`);
  if (iso.noNewPrivs !== 1) blockers.push(`NoNewPrivs is ${iso.noNewPrivs ?? "unknown"}, not 1`);
  if (iso.seccomp !== 2) blockers.push(`no seccomp filter (Seccomp ${iso.seccomp ?? "unknown"})`);
  if (!iso.imageDigest || !/^sha256:[0-9a-f]{64}$/.test(iso.imageDigest))
    blockers.push("image digest unknown (ARGUS_IMAGE_DIGEST must be sha256:<64 hex>)");
  if (iso.uid === null) blockers.push("uid unknown");
  else if (iso.uid === 0) blockers.push("running as uid 0");
  return blockers;
}

/** The provider an installed runner root ships; null when the root is the live runtime. */
export function providerPath(realRunnerRoot: string): string | null {
  const root = path.resolve(realRunnerRoot);
  if (root === LIVE_RUNTIME || root.startsWith(`${LIVE_RUNTIME}/`)) return null;
  return path.join(root, "src", "providers", "codex-app-server.ts");
}

/** What is wrong with an installed provider's source; empty when it is the patched one. */
export function providerSourceProblems(text: string): string[] {
  const problems = FORBIDDEN.filter((s) => text.includes(s)).map((s) => `contains ${s}`);
  if (!text.includes(TRUST_IMPORT)) problems.push("does not import codex-hook-trust");
  if (!text.includes("await establishCodexHookTrust("))
    problems.push("does not call establishCodexHookTrust");
  return problems;
}

export interface CommandExit {
  command: string;
  exitCode: number | null;
  status: string | null;
}

export interface Receipt {
  status: "passed" | "failed" | "blocked";
  reasons: string[];
  isolation: Isolation;
  provider: { path: string; sha256: string | null; overlaySha256: string | null } | null;
  spawnArgs: string[] | null;
  sandbox: string | null;
  approvalPolicy: string | null;
  /** `codex` as resolved on the provider's PATH, and the process it actually spawned. */
  runtime: {
    selected: string | null;
    used: { exe: string; cmdline: string[] };
    version: string | null;
  } | null;
  trustStatuses: Record<string, string> | null;
  commandExits: CommandExit[] | null;
  hookEvents: string[] | null;
  okFile: boolean | null;
  negativeMarker: boolean | null;
}

/** A receipt with nothing beyond what was measured: every run-time field is null. */
export function baseReceipt(isolation: Isolation, provider: Receipt["provider"]): Receipt {
  return {
    status: "blocked",
    reasons: [],
    isolation,
    provider,
    spawnArgs: null,
    sandbox: null,
    approvalPolicy: null,
    runtime: null,
    trustStatuses: null,
    commandExits: null,
    hookEvents: null,
    okFile: null,
    negativeMarker: null,
  };
}

/** Why a completed run does not prove the installed provider; empty means it does. */
export function runFailures(r: Receipt): string[] {
  const failures: string[] = [];
  for (const arg of r.spawnArgs ?? []) {
    if (FORBIDDEN.some((s) => arg.includes(s))) failures.push(`spawned with ${arg}`);
  }
  if (!r.spawnArgs) failures.push("spawn args not observed");
  const statuses = Object.values(r.trustStatuses ?? {});
  if (statuses.length === 0 || statuses.some((s) => s !== "trusted"))
    failures.push("expected hooks not all trusted");
  if (r.okFile !== true) failures.push("benign command did not run");
  if (r.negativeMarker === true) failures.push("negative control ran: guard did not deny");
  else if (r.negativeMarker !== false) failures.push("negative control not observed");
  return failures;
}

export function exitCodeFor(status: Receipt["status"]): number {
  return status === "passed" ? EXIT_PASSED : status === "blocked" ? EXIT_BLOCKED : EXIT_FAILED;
}
