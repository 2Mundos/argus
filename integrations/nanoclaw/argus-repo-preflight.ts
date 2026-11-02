/**
 * Repository preflight in a bounded, model-free session container
 * (docs/specs/worker-lifecycle.spec.md, "Repository preflight session").
 *
 * The NanoClaw host has no OneCLI HTTPS proxy of its own: only a session container
 * is configured with one (and the injected CA), so a probe run in the host process
 * can never reach the forge. This module starts a short-lived session of the Argus
 * agent group through the same driver and the same gateway lease as a worker session,
 * replaces PID 1 with the credential-free helper's `repo-preflight`, and reads the
 * verdict the helper wrote to a mounted file. No model starts and no credential is
 * in the host's environment, argv or logs: OneCLI injects it at the proxy.
 */
import { randomBytes } from "node:crypto";
import { constants as fsc } from "node:fs";
import { chown, lstat, mkdir, open, readdir, rm } from "node:fs/promises";
import path from "node:path";
import {
  type AccountLease,
  assignmentConfirmed,
  normalizeRepositoryUrl,
  type RepositoryAssignmentCheck,
  type RepositorySecretCheck,
} from "./argus-accounts.js";

export const PREFLIGHT_TIMEOUT_MS = 90_000;
/** Each teardown step (stop, list, release, ...) is bounded on its own. */
export const PREFLIGHT_TEARDOWN_STEP_MS = 10_000;
/** The most a probe can take: the deadline, six bounded teardown steps and slack. */
export const PREFLIGHT_TOTAL_MS = PREFLIGHT_TIMEOUT_MS + 6 * PREFLIGHT_TEARDOWN_STEP_MS + 15_000;
const VERDICT_MAX_BYTES = 64 * 1024;
export const PREFLIGHT_RESULT_FILE = "result.json";
/** Where the container sees the result directory (the helper only writes here). */
export const PREFLIGHT_CONTAINER_DIR = "/preflight";

/** What the child did: the exit code of PID 1 and the verdict file it wrote, if any. */
export interface PreflightChildResult {
  exitCode: number;
  verdict: unknown;
}

export interface PreflightChildRequest {
  helper: "argus-github-api" | "argus-gitea-api";
  /** The one repository the child may touch (its whole allowlist). */
  repositoryUrl: string;
  timeoutMs: number;
  access?: "read" | "write";
  /** The OneCLI agent (agent group id) whose assignments were checked; the session must be its. */
  agentIdentifier?: string;
}

/**
 * Runs the helper in an ephemeral session. Rejects on: no proxy configured for the
 * session, start failure, timeout, an end without an exit code, or incomplete teardown.
 */
export type PreflightRunner = (request: PreflightChildRequest) => Promise<PreflightChildResult>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The helper's own error codes (gitea-api.mjs EXIT_CODES): the only text from the child that is reported. */
const HELPER_ERROR_CODES: ReadonlySet<string> = new Set([
  "internal_error",
  "usage",
  "unauthorized",
  "forbidden",
  "not_found",
  "conflict",
  "validation_failed",
  "rate_limited",
  "server_error",
  "network_error",
  "http_error",
  "limit_exceeded",
  "policy_denied",
  "ambiguous",
  "bad_response",
]);

/**
 * Why the child failed, as a closed reason. The verdict file is written by the container,
 * so none of its free text (messages, paths, provider names) reaches the task: only a code
 * from the helper's fixed set, or a fixed phrase.
 */
export function describeFailure(verdict: unknown): string {
  if (!isRecord(verdict) || !isRecord(verdict.error)) return "no verdict was written";
  const { code } = verdict.error;
  return typeof code === "string" && HELPER_ERROR_CODES.has(code)
    ? `helper error ${code}`
    : "helper error of an unknown kind";
}

/** Bounded, printable, credential-scrubbed text for errors whose origin is not ours (drivers, OneCLI). */
export function safeText(text: string, max = 160): string {
  return (
    text
      .replace(/[^\x20-\x7e]+/g, " ")
      // OneCLI's session proxy URL carries its agent token as userinfo, and a failed `docker`
      // call prints its whole argv: drop any URL's userinfo and any proxy variable's value.
      .replace(/\b([a-z][a-z0-9+.-]*:\/\/)[^\s/?#@]*@/gi, "$1[redacted]@")
      .replace(/\b((?:https?|all|no)_proxy\s*[=:]\s*)\S+/gi, "$1[redacted]")
      .replace(/\b(?:bearer\s+|sk-|eyJ|ghp_|github_pat_)[A-Za-z0-9._~+/=-]{8,}/gi, "[redacted]")
      .replace(
        /((?:token|secret|password|passwd|pwd|api[_-]?key|authorization)["']?\s*[:=]\s*["']?)[^\s"',;]+/gi,
        "$1[redacted]",
      )
      .slice(0, max)
  );
}

/** How a repository preflight ended (failure-classes spec 3.3.1, "Outcomes"). */
export type PreflightOutcome =
  | "ok"
  | "assignment_missing"
  | "assignment_unverified"
  | "forge_denied"
  | "network_failure"
  | "unknown"
  | "session_failure";

/** A failed preflight with its outcome; the message is already scrubbed and bounded. */
export class RepositoryPreflightError extends Error {
  constructor(
    readonly outcome: Exclude<PreflightOutcome, "ok">,
    message: string,
    readonly evidence: ProbeEvidence | null = null,
  ) {
    super(safeText(message, 300));
    this.name = "RepositoryPreflightError";
  }
}

/** What the session's verdict shows, re-read on the host into closed values only. */
export interface ProbeEvidence {
  helper_error: string | null;
  http_status: number | null;
  permissions: "present" | "absent" | "unknown";
  push: boolean | null;
  visibility: "private" | "public" | "unknown";
  authentication: "proven" | "unknown";
}

function oneOf<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  return allowed.includes(value as T) ? (value as T) : fallback;
}

/** The verdict's evidence, or null when there is no verdict. Nothing is copied as text. */
export function verdictEvidence(verdict: unknown): ProbeEvidence | null {
  if (!isRecord(verdict)) return null;
  const ev = isRecord(verdict.evidence) ? verdict.evidence : {};
  const code = isRecord(verdict.error) ? verdict.error.code : undefined;
  const status = ev.http_status;
  const push = typeof ev.push === "boolean" ? ev.push : null;
  const visibility = oneOf(ev.visibility, ["private", "public"] as const, "unknown");
  return {
    helper_error: typeof code === "string" && HELPER_ERROR_CODES.has(code) ? code : null,
    http_status:
      typeof status === "number" && Number.isInteger(status) && status >= 100 && status <= 599
        ? status
        : null,
    permissions: oneOf(ev.permissions, ["present", "absent"] as const, "unknown"),
    push,
    visibility,
    // Recomputed here rather than trusted: only push or a private answer proves a credential.
    authentication: push === true || visibility === "private" ? "proven" : "unknown",
  };
}

/**
 * The outcome of a failed child. A forge refusal (401/403/404) is a denial; a missing
 * permission is a denial only when the answer proves a credential was attached, since a
 * public repository answers without one. Anything that cannot prove it is `unknown`.
 */
export function failedOutcome(
  evidence: ProbeEvidence | null,
): "forge_denied" | "network_failure" | "unknown" | "session_failure" {
  if (!evidence) return "session_failure";
  switch (evidence.helper_error) {
    case "unauthorized":
    case "forbidden":
    case "not_found":
      return "forge_denied";
    case "network_error":
      return "network_failure";
    case "policy_denied":
      return evidence.http_status === 200 && evidence.authentication === "proven"
        ? "forge_denied"
        : "unknown";
    default:
      return "unknown";
  }
}

/**
 * The positive the preflight accepts. Anything else, however close, fails closed:
 * exit 0 and an `ok` verdict for exactly this provider and repository with the helper's
 * own allowlist refusals confirmed and, for write, authenticated write access
 * (forge-reported over the session proxy path); for read, read access. The refusals are a
 * local policy check: they say nothing about OneCLI's host/path scoping or token permissions.
 * Returns the evidence; throws a `RepositoryPreflightError` with the outcome.
 */
export function assertPreflightPositive(
  result: PreflightChildResult,
  expected: { provider: string; repository: string },
  access: "read" | "write" = "write",
): ProbeEvidence {
  const v = result.verdict;
  const evidence = verdictEvidence(v);
  const fail = (message: string, outcome = failedOutcome(evidence)): never => {
    throw new RepositoryPreflightError(outcome, message, evidence);
  };
  if (result.exitCode !== 0)
    fail(
      `the repository preflight session exited with code ${result.exitCode} (${describeFailure(v)})`,
    );
  if (!isRecord(v) || v.ok !== true)
    fail(`the repository preflight did not confirm access (${describeFailure(v)})`);
  const ok = v as Record<string, unknown>;
  if (ok.provider !== expected.provider)
    fail(`the repository preflight ran for another provider than ${expected.provider}`, "unknown");
  if (ok.repository !== expected.repository)
    fail("the repository preflight ran against a different repository", "unknown");
  if (access === "write") {
    if (ok.authenticated !== true || ok.write !== true)
      fail(
        "the repository preflight did not report authenticated write access in its verdict",
        "unknown",
      );
  } else if (ok.read !== true && ok.write !== true)
    fail("the repository preflight did not report read access in its verdict", "unknown");
  if (ok.allowlist_refuses_other_host !== true || ok.allowlist_refuses_sibling !== true)
    fail("the repository preflight did not confirm the helper allowlist refusals", "unknown");
  return evidence as ProbeEvidence;
}

/**
 * The `probeRepository` of the Argus channel, backed by an ephemeral session.
 * `provider` is the provider of the repository lease that was just applied: a lease
 * of the other provider (or a repository on the other host) is refused before any
 * container exists. `agentIdentifier` is the OneCLI agent whose assignments were checked:
 * the session must run for that agent group.
 */
export function sessionRepositoryProbe(
  run: PreflightRunner,
  timeoutMs = PREFLIGHT_TIMEOUT_MS,
): (
  repositoryUrl: string,
  allowlist: readonly string[],
  provider: string,
  access?: "read" | "write",
  agentIdentifier?: string,
) => Promise<ProbeEvidence> {
  return async (repositoryUrl, allowlist, provider, access = "write", agentIdentifier) => {
    const repository = normalizeRepositoryUrl(repositoryUrl);
    if (repository.provider !== provider)
      throw new RepositoryPreflightError(
        "assignment_missing",
        `the repository is on ${repository.provider} but the applied lease is ${provider}`,
      );
    const identity = (raw: string) => {
      const r = normalizeRepositoryUrl(raw);
      return `${r.origin}/${r.owner}/${r.repo}`;
    };
    const exact = identity(repositoryUrl);
    if (!allowlist.some((entry) => identity(entry) === exact))
      throw new RepositoryPreflightError(
        "assignment_missing",
        "the repository is not in this worker's allowlist",
      );
    const result = await run({
      helper: repository.provider === "github" ? "argus-github-api" : "argus-gitea-api",
      repositoryUrl: repository.cloneUrl,
      timeoutMs,
      access,
      agentIdentifier,
    });
    return assertPreflightPositive(
      result,
      { provider: repository.provider, repository: exact },
      access,
    );
  };
}

// ---------------------------------------------------------------------------
// The service: lease check, assignment check, probe, one evidence event.
// ---------------------------------------------------------------------------

/** The block reason the channel reports for an outcome; null for `ok`. */
export function preflightReason(outcome: PreflightOutcome): string | null {
  if (outcome === "ok") return null;
  return outcome === "assignment_missing" || outcome === "assignment_unverified"
    ? "env_repository_unavailable"
    : "env_repository_unreachable";
}

/** One preflight's non-secret evidence (failure-classes spec 3.3.1, "Evidence events"). */
export interface PreflightEvidenceEvent {
  event: "repository_preflight";
  preflight_id: string;
  task_id: string;
  attempt: number;
  provider: string;
  access: "read" | "write";
  outcome: PreflightOutcome;
  lease: { source: "host"; held: boolean; role: boolean; provider: boolean };
  assignment:
    | ({ source: "onecli_admin_read"; read: true } & Omit<RepositoryAssignmentCheck, "scopes">)
    | { source: "onecli_admin_read"; read: false }
    | null;
  target: {
    source: "host";
    api_host: string;
    api_path: string;
    git_host: string;
    git_path: string;
  };
  probe: ({ source: "preflight_session_verdict" } & ProbeEvidence) | null;
}

export interface RepositoryPreflightInput {
  taskId: string;
  attempt: number;
  repositoryUrl: string;
  access: "read" | "write";
  /** The task-scoped repository lease the channel holds for this attempt, if any. */
  lease: Pick<AccountLease, "lease_id" | "provider" | "role"> | null;
  /** Admin reads only: the lease agent's repository secrets. */
  verifyAssignment(): Promise<RepositoryAssignmentCheck>;
  /** The session probe for the same agent; throws a `RepositoryPreflightError` on failure. */
  probe(): Promise<ProbeEvidence | void>;
  /** Receives the event; a throw or rejection is ignored. */
  emit?(event: PreflightEvidenceEvent): unknown;
  preflightId?: string;
}

export interface RepositoryPreflightResult {
  outcome: PreflightOutcome;
  /** The block reason, null when the task may start. */
  reason: string | null;
  error: string | null;
  event: PreflightEvidenceEvent;
}

/**
 * The repository preflight after the lease is applied: confirm the lease and the OneCLI
 * assignments (no writes, so concurrent leases are untouched), then probe through a session
 * of that agent. Fails closed with a distinct outcome at each step and emits one bounded,
 * redacted evidence event.
 */
export async function runRepositoryPreflight(
  input: RepositoryPreflightInput,
): Promise<RepositoryPreflightResult> {
  const repository = normalizeRepositoryUrl(input.repositoryUrl);
  const host = (text: string) => safeText(text, 200);
  const event: PreflightEvidenceEvent = {
    event: "repository_preflight",
    preflight_id: input.preflightId ?? randomBytes(8).toString("hex"),
    task_id: safeText(input.taskId, 64),
    attempt: Number.isInteger(input.attempt) ? input.attempt : -1,
    provider: repository.provider,
    access: input.access,
    outcome: "unknown",
    lease: {
      source: "host",
      held: Boolean(input.lease?.lease_id),
      role: input.lease?.role === "repository",
      provider: input.lease?.provider === repository.provider,
    },
    assignment: null,
    target: {
      source: "host",
      api_host: host(repository.apiHost),
      api_path: host(`${repository.apiPath}/*`),
      git_host: host(repository.host),
      git_path: host(repository.gitPathPattern),
    },
    probe: null,
  };
  const finish = async (outcome: PreflightOutcome, detail?: string) => {
    event.outcome = outcome;
    try {
      await input.emit?.(event);
    } catch {
      // Evidence is best effort; it never changes the outcome.
    }
    return {
      outcome,
      reason: preflightReason(outcome),
      error:
        detail === undefined ? null : `repository preflight ${outcome}: ${safeText(detail, 300)}`,
      event,
    };
  };
  if (!event.lease.held || !event.lease.role || !event.lease.provider)
    return finish(
      "assignment_missing",
      "no task-scoped repository lease for this provider is held",
    );
  let check: RepositoryAssignmentCheck;
  try {
    check = await input.verifyAssignment();
  } catch {
    event.assignment = { source: "onecli_admin_read", read: false };
    return finish("assignment_unverified", "the OneCLI repository assignment could not be read");
  }
  const { scopes: _scopes, ...listed } = check;
  event.assignment = {
    source: "onecli_admin_read",
    read: true,
    agent: oneOf(listed.agent, ["found", "missing"] as const, "missing"),
    secretMode: oneOf(listed.secretMode, ["selective", "all"] as const, "unknown"),
    rest: closedSecret(listed.rest),
    git: closedSecret(listed.git),
  };
  if (!assignmentConfirmed(check))
    return finish(
      "assignment_missing",
      `the OneCLI agent ${event.assignment.agent === "found" ? "lacks a scoped, assigned repository secret" : "is missing"} (rest ${event.assignment.rest.state}/${event.assignment.rest.scope}, git ${event.assignment.git.state}/${event.assignment.git.scope})`,
    );
  try {
    const evidence = await input.probe();
    if (evidence) event.probe = { source: "preflight_session_verdict", ...evidence };
    return finish("ok");
  } catch (error) {
    if (error instanceof RepositoryPreflightError) {
      if (error.evidence) event.probe = { source: "preflight_session_verdict", ...error.evidence };
      return finish(error.outcome, error.message);
    }
    return finish("session_failure", message(error));
  }
}

function closedSecret(s: RepositorySecretCheck): RepositorySecretCheck {
  return {
    state: oneOf(s?.state, ["assigned", "unassigned", "missing"] as const, "missing"),
    scope: oneOf(s?.scope, ["match", "mismatch", "unlisted"] as const, "unlisted"),
  };
}

// ---------------------------------------------------------------------------
// The real runner: composes the session exactly as the host does for a worker.
// ---------------------------------------------------------------------------

/** The slice of a NanoClaw `SessionSpec` / container this module reads or replaces. */
interface ContainerLike {
  role: string;
  env: Record<string, string>;
  contributedEnv?: Record<string, string>;
  command?: string[];
  args?: string[];
  mounts: Array<{
    class: string;
    hostPath: string;
    containerPath: string;
    mode: "rw" | "ro";
    groupScope: string;
  }>;
  securityProfile?: string;
  labels?: Record<string, string>;
}
interface SpecLike {
  key: { installSlug: string; agentGroupId: string; sessionId: string };
  labels: Record<string, string>;
  containers: ContainerLike[];
  runAs?: { uid: number; gid: number };
}
type StatusLike =
  | { phase: "preparing" | "ready" | "running" | "stopped" }
  | { phase: "failed"; failure: { kind: string; exitCode?: number } };
interface HandleLike {
  start(): Promise<void>;
  status(): Promise<StatusLike>;
  stop(reason: string): Promise<void>;
}
interface LeaseLike {
  contribution: { env?: Record<string, string> };
  onUnavailable?(report: (reason: string) => void): void;
}
/** NanoClaw's `GatewaySessionControl`: the lease and the controller its release aborts. */
export interface GatewayControl {
  lease: LeaseLike;
  controller: AbortController;
}
interface ListedSession {
  handle: { key: { sessionId: string }; stop(reason: string): Promise<void> };
}

/** NanoClaw internals the runner needs; injected so tests run the real driver on a fake runtime. */
export interface EphemeralSessionDeps {
  installSlug: string;
  group(): Promise<{ id: string; name: string }>;
  /** The agent group's session directory root (data/v2-sessions/<group>/<session>). */
  sessionDir(groupId: string, sessionId: string): string;
  driver(): {
    capabilities(): unknown;
    prepare(spec: never): Promise<HandleLike>;
    listSessions(installSlug: string): Promise<ListedSession[]>;
  };
  /** NanoClaw's own `ensureGatewaySession`: the worker path's lease and admission gate. */
  ensureGateway(input: {
    key: SpecLike["key"];
    disposition: "create";
    runtimeIdentity: string;
    groupName: string;
    containerName: string;
    capabilities: unknown;
  }): Promise<GatewayControl>;
  /** NanoClaw's `releaseGatewaySession`: aborts this session's controller, nothing else. */
  releaseGateway(
    control: GatewayControl,
    event: { kind: "session-ended"; reason: string },
  ): Promise<void>;
  /** NanoClaw's `composeSessionSpec` for the group, with no mounts of its own. */
  compose(args: {
    groupId: string;
    sessionId: string;
    containerName: string;
    gateway: LeaseLike["contribution"];
  }): Promise<SpecLike>;
  /** The worker path's check that the driver realizes the spec's security profile (throws). */
  assertSpecAllowed?(capabilities: unknown, spec: SpecLike): void;
  /**
   * Settles when NanoClaw's startup adoption has finished. Adoption stops every listed session
   * without a database row, which a preflight session is, so none may exist before it settles.
   */
  startupReady(): Promise<void>;
  sleep?(ms: number): Promise<void>;
  pollMs?: number;
  teardownStepMs?: number;
}

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
const message = (error: unknown) =>
  safeText(error instanceof Error ? error.message : String(error));

/**
 * Race `work` against `ms`. On timeout the work keeps running, so `onLate` receives
 * what it eventually produces (a handle to stop, a lease to release) and the caller
 * awaits `sink` during teardown: nothing a hung operation creates is left behind.
 */
function guard<T>(
  label: string,
  work: Promise<T>,
  ms: number,
  sink: Promise<unknown>[],
  onLate?: (value: T) => Promise<unknown>,
): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let timedOut = false;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(
      () => {
        timedOut = true;
        if (onLate)
          sink.push(
            work.then(
              (value) => onLate(value),
              () => undefined,
            ),
          );
        reject(new Error(`${label} did not finish within ${ms} ms`));
      },
      Math.max(1, ms),
    );
  });
  return Promise.race([work, timeout]).finally(() => clearTimeout(timer));
}

/**
 * The verdict file, read without trusting the directory the container wrote to: the
 * directory and file must be plain (no symlink, one link, regular, owned by us), the
 * open refuses a symlink, and the size is bounded.
 */
export async function readVerdictFile(dir: string, name = PREFLIGHT_RESULT_FILE): Promise<unknown> {
  const self = process.getuid?.();
  const plain = (st: { uid: number }) => self === undefined || self === 0 || st.uid === self;
  const dirStat = await lstat(dir).catch(() => null);
  if (!dirStat?.isDirectory() || !plain(dirStat)) return null;
  const file = path.join(dir, name);
  const before = await lstat(file).catch(() => null);
  if (!before?.isFile() || before.nlink !== 1 || !plain(before)) return null;
  let handle: Awaited<ReturnType<typeof open>> | undefined;
  try {
    handle = await open(file, fsc.O_RDONLY | fsc.O_NOFOLLOW);
    const after = await handle.stat();
    if (
      !after.isFile() ||
      after.size > VERDICT_MAX_BYTES ||
      after.ino !== before.ino ||
      after.dev !== before.dev
    )
      return null;
    const parsed: unknown = JSON.parse(await handle.readFile("utf8"));
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  } finally {
    await handle?.close().catch(() => undefined);
  }
}

const RESIDUE_NAME = /^preflight-[0-9a-f]{12}$/;

/**
 * Remove the scratch directories of preflight sessions that a crashed host left behind.
 * Only direct children of the group's session directory whose name is exactly a preflight id,
 * that are real directories (a symlink is never followed or removed), that no listed session
 * carries and that are older than any live preflight can be. Every other session's directory
 * fails the name test. A crashed host's container is stopped by NanoClaw's startup adoption,
 * which stops any listed session without a database row.
 */
export async function sweepPreflightResidue(
  parent: string,
  live: ReadonlySet<string>,
  olderThanMs: number,
  nowMs = Date.now(),
): Promise<string[]> {
  const removed: string[] = [];
  const names = await readdir(parent).catch(() => [] as string[]);
  for (const name of names) {
    if (!RESIDUE_NAME.test(name) || live.has(name)) continue;
    const full = path.join(parent, name);
    const st = await lstat(full).catch(() => null);
    if (!st?.isDirectory() || nowMs - st.mtimeMs < olderThanMs) continue;
    await rm(full, { recursive: true, force: true }).catch(() => undefined);
    removed.push(name);
  }
  return removed;
}

/** The result directory must be writable by the container's user and by nobody else. */
export async function prepareResultDir(dir: string, runAs: SpecLike["runAs"]): Promise<void> {
  await mkdir(dir, { recursive: true, mode: 0o700 });
  const self = process.getuid?.();
  const uid = runAs?.uid ?? 1000; // the agent image's `node` user when no mapping is composed
  const gid = runAs?.gid ?? uid;
  if (self === uid) return;
  if (self !== 0)
    throw new Error(`the container user ${uid} cannot write the preflight result directory`);
  await chown(dir, uid, gid);
}

export function ephemeralSessionRunner(deps: EphemeralSessionDeps): PreflightRunner {
  const sleep = deps.sleep ?? defaultSleep;
  const pollMs = deps.pollMs ?? 250;
  const stepMs = deps.teardownStepMs ?? PREFLIGHT_TEARDOWN_STEP_MS;
  return async (request) => {
    const deadlineAt = Date.now() + request.timeoutMs;
    const remaining = () => Math.max(1, deadlineAt - Date.now());
    const sink: Promise<unknown>[] = [];
    let sessionId: string | undefined;
    let dir: string | undefined;
    let control: GatewayControl | undefined;
    let handle: HandleLike | undefined;
    let unavailable: string | undefined;
    let outcome: { result?: PreflightChildResult; error?: unknown } = {};
    const release = (c: GatewayControl) =>
      deps.releaseGateway(c, { kind: "session-ended", reason: "repository-preflight-done" });
    try {
      await guard("NanoClaw's startup adoption", deps.startupReady(), remaining(), sink);
      const group = await guard("the agent group lookup", deps.group(), remaining(), sink);
      if (request.agentIdentifier !== undefined && group.id !== request.agentIdentifier)
        throw new RepositoryPreflightError(
          "assignment_missing",
          "the preflight session's agent group is not the OneCLI agent whose assignments were checked",
        );
      // Best effort, inside the deadline: scratch left by a crashed host. Never fails the probe.
      try {
        const listed = await guard(
          "the residue listing",
          deps.driver().listSessions(deps.installSlug),
          Math.min(stepMs, remaining()),
          sink,
        );
        await guard(
          "the residue sweep",
          sweepPreflightResidue(
            path.dirname(deps.sessionDir(group.id, "preflight-000000000000")),
            new Set(listed.map((l) => l.handle.key.sessionId)),
            2 * PREFLIGHT_TOTAL_MS,
          ),
          Math.min(stepMs, remaining()),
          sink,
        );
      } catch {
        // Nothing to sweep, or the driver cannot list: the teardown listing below fails closed.
      }
      sessionId = `preflight-${randomBytes(6).toString("hex")}`;
      const containerName = `nanoclaw-v2-preflight-${sessionId.slice(10)}`;
      dir = deps.sessionDir(group.id, sessionId);
      const resultDir = path.join(dir, "preflight");
      const key = { installSlug: deps.installSlug, agentGroupId: group.id, sessionId };
      const driver = deps.driver();
      const capabilities = driver.capabilities();
      control = await guard(
        "the gateway lease",
        deps.ensureGateway({
          key,
          disposition: "create",
          runtimeIdentity: `${key.installSlug}/${key.agentGroupId}/${sessionId}`,
          groupName: group.name,
          containerName,
          capabilities,
        }),
        remaining(),
        sink,
        release,
      );
      control.lease.onUnavailable?.((reason) => {
        unavailable = reason;
      });
      const env = control.lease.contribution.env;
      if (!(env?.HTTPS_PROXY || env?.https_proxy))
        throw new Error("OneCLI did not configure an HTTPS proxy for the preflight session");
      const spec = await guard(
        "the session composition",
        deps.compose({
          groupId: group.id,
          sessionId,
          containerName,
          gateway: control.lease.contribution,
        }),
        remaining(),
        sink,
      );
      const agent = spec.containers.find((c) => c.role === "agent");
      if (!agent) throw new Error("the composed preflight session has no agent container");
      // PID 1 is the helper: no model runner, no mailbox. Only the exact repository is allowed.
      // The composed security profile, user mapping, limits, network and gateway mounts stay
      // as the worker path composed them; the driver realizes them or refuses the spec.
      agent.command = [`/usr/local/bin/${request.helper}`];
      agent.args = [
        "repo-preflight",
        "--repository",
        request.repositoryUrl,
        "--access",
        request.access === "read" ? "read" : "write",
        "--result-file",
        `${PREFLIGHT_CONTAINER_DIR}/${PREFLIGHT_RESULT_FILE}`,
        "--timeout-ms",
        String(Math.min(30_000, request.timeoutMs)),
      ];
      agent.contributedEnv = {
        ...agent.contributedEnv,
        ARGUS_REPOSITORIES_JSON: JSON.stringify({ repository: request.repositoryUrl }),
      };
      agent.labels = { ...agent.labels, "argus-preflight": "1" };
      if (agent.mounts.some((m) => m.containerPath === PREFLIGHT_CONTAINER_DIR))
        throw new Error(`the composed session already mounts ${PREFLIGHT_CONTAINER_DIR}`);
      await guard(
        "the result directory",
        prepareResultDir(resultDir, spec.runAs),
        remaining(),
        sink,
      );
      agent.mounts = [
        ...agent.mounts,
        {
          class: "group-state",
          hostPath: resultDir,
          containerPath: PREFLIGHT_CONTAINER_DIR,
          mode: "rw",
          groupScope: group.id,
        },
      ];
      deps.assertSpecAllowed?.(capabilities, spec);
      handle = await guard(
        "the session prepare",
        driver.prepare(spec as never),
        remaining(),
        sink,
        (late) => late.stop("repository-preflight-late-prepare"),
      );
      await guard("the session start", handle.start(), remaining(), sink);
      let status: StatusLike = await guard(
        "the session status",
        handle.status(),
        remaining(),
        sink,
      );
      const live = (st: StatusLike) =>
        st.phase === "preparing" || st.phase === "ready" || st.phase === "running";
      for (;;) {
        while (live(status)) {
          if (unavailable) throw new Error(`the gateway became unavailable: ${unavailable}`);
          if (Date.now() >= deadlineAt)
            throw new Error(
              `the repository preflight session did not finish within ${request.timeoutMs} ms`,
            );
          await sleep(Math.min(pollMs, remaining()));
          status = await guard("the session status", handle.status(), remaining(), sink);
        }
        if (status.phase !== "stopped") break;
        // The driver reports exit 0 as `stopped` and any other exit code as `failed`, but a
        // container in Docker's transient `removing` state also reads as `stopped` before the
        // attach process has recorded how it ended. Read again after a poll: only a repeated
        // `stopped` is taken as exit 0, and a session that is running again is not finished.
        await sleep(Math.min(pollMs, remaining()));
        const again = await guard("the session status", handle.status(), remaining(), sink);
        status = again;
        if (!live(again)) break;
      }
      if (unavailable) throw new Error(`the gateway became unavailable: ${unavailable}`);
      let exitCode: number;
      if (status.phase === "stopped") exitCode = 0;
      else if (status.phase === "failed" && typeof status.failure.exitCode === "number")
        exitCode = status.failure.exitCode;
      else
        throw new Error(
          `the repository preflight session ended unexpectedly (${status.phase === "failed" ? status.failure.kind : status.phase})`,
        );
      const verdict = await guard(
        "the verdict read",
        readVerdictFile(resultDir),
        remaining(),
        sink,
      );
      if (unavailable) throw new Error(`the gateway became unavailable: ${unavailable}`);
      outcome = { result: { exitCode, verdict } };
    } catch (error) {
      outcome = { error };
    }
    // Teardown is part of the preflight, and every step is bounded on its own so a hung
    // operation cannot keep the container, the lease or this call alive.
    const teardown: string[] = [];
    const step = async (label: string, work: () => Promise<unknown>) => {
      try {
        await guard(label, work(), stepMs, sink);
      } catch (error) {
        teardown.push(`${label}: ${message(error)}`);
      }
    };
    await step("stop", async () => handle?.stop("repository-preflight-done"));
    // A prepare that hung or failed after `docker create` leaves no handle here: find it by key.
    await step("reap", async () => {
      if (handle || !sessionId) return;
      for (const s of await deps.driver().listSessions(deps.installSlug))
        if (s.handle.key.sessionId === sessionId) await s.handle.stop("repository-preflight-reap");
    });
    await step("gateway release", async () => control && release(control));
    // Late results of timed-out operations (a handle, a lease) are cleaned up before the check.
    try {
      const cleanupUntil = Date.now() + stepMs;
      let batch = sink.splice(0);
      while (batch.length > 0 && Date.now() < cleanupUntil) {
        const sliceMs = Math.max(1, Math.min(25, cleanupUntil - Date.now()));
        const outcome = await Promise.race([
          Promise.allSettled(batch).then((results) => ({ kind: "done" as const, results })),
          sleep(sliceMs).then(() => ({ kind: "wait" as const })),
        ]);
        if (outcome.kind === "wait") continue;
        const failed = outcome.results.filter(
          (r) => r.status === "rejected",
        ) as PromiseRejectedResult[];
        if (failed.length > 0) throw new Error(failed.map((r) => message(r.reason)).join("; "));
        batch = sink.splice(0);
      }
      if (batch.length > 0) sink.push(...batch);
    } catch (error) {
      teardown.push(`late cleanup: ${message(error)}`);
    }
    await step("listing", async () => {
      if (!sessionId) return;
      const left = (await deps.driver().listSessions(deps.installSlug)).some(
        (s) => s.handle.key.sessionId === sessionId,
      );
      if (left) throw new Error("the preflight container is still listed after stop");
    });
    // Scratch holds no credential and the next preflight sweeps it: bounded, never fatal.
    if (dir)
      await guard("scratch removal", rm(dir, { recursive: true, force: true }), stepMs, sink).catch(
        () => undefined,
      );
    for (const pending of sink.splice(0)) {
      try {
        await Promise.race([pending, sleep(stepMs)]);
      } catch (error) {
        teardown.push(`late cleanup: ${message(error)}`);
      }
    }
    if (teardown.length > 0)
      throw new RepositoryPreflightError(
        "session_failure",
        `the repository preflight session was not torn down (${teardown.join("; ")})`,
      );
    // The error may carry text from a driver or OneCLI (a failed docker call prints its argv,
    // which holds the proxy URL): only scrubbed, bounded text leaves this module.
    if (outcome.error)
      throw new RepositoryPreflightError(
        outcome.error instanceof RepositoryPreflightError
          ? outcome.error.outcome
          : "session_failure",
        message(outcome.error),
      );
    return outcome.result as PreflightChildResult;
  };
}

/**
 * Resolves when NanoClaw's startup adoption succeeded (`startupAdoptionDone`, added by
 * patch-startup-adoption-gate.py). Rejects when adoption threw or the installed
 * container-runner lacks the export.
 */
export async function nanoclawStartupReady(): Promise<void> {
  const runner = await import("../container-runner.js");
  const gate = (runner as { startupAdoptionDone?: Promise<void> }).startupAdoptionDone;
  if (!(gate instanceof Promise))
    throw new Error(
      "container-runner does not export startupAdoptionDone: run install.sh and rebuild NanoClaw",
    );
  await gate;
}

/** The production runner: the installed NanoClaw's own modules, resolved when first used. */
export function nanoclawPreflightRunner(
  resolveGroup: () => Promise<{ id: string; name: string } | null>,
): PreflightRunner {
  return async (request) => {
    const [runner, lifecycle, config, sessions, drivers, containerConfig, sandbox] =
      await Promise.all([
        import("../container-runner.js"),
        import("../gateway-session-lifecycle.js"),
        import("../config.js"),
        import("../session-manager.js"),
        import("../drivers/index.js"),
        import("../container-config.js"),
        import("../drivers/argus-codex-sandbox.js"),
      ]);
    const { getAgentGroup } = await import("../db/agent-groups.js");
    // The hook is added by install.sh (patch-gateway-session-export.py) and needs a rebuild.
    if (typeof runner.ensureGatewaySession !== "function")
      throw new Error(
        "container-runner does not export ensureGatewaySession: run install.sh and rebuild NanoClaw",
      );
    return ephemeralSessionRunner({
      installSlug: config.INSTALL_SLUG,
      startupReady: nanoclawStartupReady,
      group: async () => {
        const group = await resolveGroup();
        if (!group) throw new Error("no Argus agent group for the repository preflight");
        return group;
      },
      sessionDir: sessions.sessionDir,
      driver: () => drivers.getSessionDriver() as never,
      ensureGateway: (input) => runner.ensureGatewaySession(input as never) as never,
      releaseGateway: (control, event) =>
        lifecycle.releaseGatewaySession(control as never, event as never),
      assertSpecAllowed: (capabilities, spec) =>
        sandbox.assertDriverSupportsSecurityProfiles(
          capabilities as never,
          spec as never,
          drivers.getSessionDriver().kind,
        ),
      compose: async ({ groupId, sessionId, containerName, gateway }) => {
        const agentGroup = await getAgentGroup(groupId);
        if (!agentGroup) throw new Error(`agent group ${groupId} no longer exists`);
        return runner.composeSessionSpec({
          agentGroup,
          session: { id: sessionId, agent_group_id: groupId, agent_provider: null } as never,
          containerName,
          mounts: [],
          containerConfig: await containerConfig.materializeContainerJson(groupId),
          contribution: {} as never,
          gateway: gateway as never,
          mailboxEnvironment: {},
        }) as never;
      },
    })(request);
  };
}
